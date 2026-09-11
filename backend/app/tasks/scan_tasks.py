"""Malware scanning (Architecture.md §3).

The shape of this task is set by one constraint: roughly 500 VirusTotal requests a day.
Everything here is arranged to spend as few as possible, and to refuse rather than
guess when there are none left.

Order of operations, cheapest first:

1. A cached verdict for this content — zero requests.
2. A budget reservation. No call is made without one.
3. A hash lookup — one request, no file content transmitted.
4. Only if submission is explicitly enabled, an upload plus polls.

The task never sleeps waiting for budget. A worker blocking on an exhausted daily window
would hold a slot for hours, and at `concurrency=2` two of them stall every scan in the
system; it requeues instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core import async_bridge
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.content import File, FileVersion
from app.models.enums import AuditAction, AuditTargetType, ScanStatus
from app.models.identity import Workspace
from app.services import audit, notifications, scan, storage, virustotal, vt_budget
from app.tasks import notification_tasks

logger = logging.getLogger(__name__)


class BudgetUnavailable(Exception):
    """No request budget right now. Not a failure — a reason to come back later."""

    def __init__(self, retry_after_seconds: int, reason: str) -> None:
        super().__init__(reason)
        self.retry_after_seconds = retry_after_seconds


def _run(coro):
    """One loop per worker process, not one per task (app.core.async_bridge)."""
    return async_bridge.run(coro)


async def _reserve_or_raise() -> None:
    reservation = await vt_budget.reserve()
    if reservation.granted:
        return
    raise BudgetUnavailable(
        reservation.retry_after_seconds,
        f"VirusTotal budget exhausted for the {reservation.blocked_by.value} window",
    )


async def _flag_audit(db, version: FileVersion, detail: dict[str, Any]) -> list[uuid.UUID]:
    """A flagged file is a security event, not a status change (Architecture.md §3 step 7).

    Returns the ids of any notifications queued, so the caller can dispatch them after
    its own commit rather than handing a worker a row a rollback might remove.
    """
    file = await db.get(File, version.file_id)
    if file is None:
        return []
    await audit.record(
        db,
        workspace_id=file.workspace_id,
        # No actor: the scanner flagged this, not a person. Recording the uploader as
        # the actor would read as though they did something, which is a different
        # allegation entirely. Who uploaded it goes in the metadata instead.
        actor_id=None,
        action=AuditAction.SCAN_FLAGGED,
        target_type=AuditTargetType.FILE,
        target_id=file.id,
        metadata={
            "label": file.name,
            "file_version_id": str(version.id),
            "file_name": file.name,
            "uploaded_by": str(version.uploaded_by),
            "stats": detail.get("stats"),
        },
    )

    # The uploader AND every workspace owner (Architecture.md §5a). The uploader needs
    # to know their file is withheld; the owners need to know because §3d makes them the
    # only people who can release it.
    workspace = await db.get(Workspace, file.workspace_id)
    if workspace is None:
        return []
    queued = await notifications.notify_scan_flagged(
        db, file=file, version=version, workspace=workspace
    )
    await db.flush()
    return [row.id for row in queued]


async def _settle(version_id: uuid.UUID, verdict: virustotal.Verdict, checksum: str) -> str:
    """Write a verdict, cache it, and audit it if it flagged."""
    async with SessionLocal() as db:
        version = await db.get(FileVersion, version_id)
        if version is None or version.deleted_at is not None:
            return "gone"
        await scan.apply_verdict(db, version, verdict)
        if not verdict.detail.get("from_cache"):
            await scan.remember_verdict(db, checksum, verdict)
        queued: list[uuid.UUID] = []
        if verdict.is_flagged:
            queued = await _flag_audit(db, version, verdict.detail)
        await db.commit()
    # After the commit, never inside it.
    notification_tasks.dispatch(queued)
    return verdict.status.value


async def _skip(version_id: uuid.UUID, reason: str) -> str:
    async with SessionLocal() as db:
        version = await db.get(FileVersion, version_id)
        if version is None or version.deleted_at is not None:
            return "gone"
        await scan.mark(
            db,
            version,
            ScanStatus.SCAN_SKIPPED,
            detail={
                "source": "virustotal",
                "skipped": True,
                "reason": reason,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
        await db.commit()
    return ScanStatus.SCAN_SKIPPED.value


async def _submit_and_poll(
    version_id: uuid.UUID, storage_key: str, filename: str, size_bytes: int
) -> virustotal.Verdict:
    """The expensive path: one upload plus a poll per attempt, each a reservation.

    Only reached when `virustotal_submit_unknown_files` is on. On the free tier the
    submitted file is shared with the VirusTotal community (§3b), so arriving here is
    always an explicit choice made in configuration.
    """
    handle, temp_path = tempfile.mkstemp(prefix="fileshare-scan-", suffix=".bin")
    os.close(handle)
    try:
        written = storage.download_to_path(storage_key, temp_path)
        if written != size_bytes:
            raise ValueError(
                f"read {written} bytes from storage but the version records {size_bytes}; "
                "refusing to submit a partial object for analysis"
            )

        await _reserve_or_raise()
        analysis_id = await virustotal.submit_file(
            temp_path, filename=filename, size_bytes=size_bytes
        )
        logger.info("submitted version %s for analysis %s", version_id, analysis_id[:16])
        # Persisted immediately: it is what tells a later requeue that an analysis is
        # already running, so the file is not submitted a second time.
        async with SessionLocal() as db:
            version = await db.get(FileVersion, version_id)
            if version is not None:
                version.scan_detail = {
                    **(version.scan_detail or {}),
                    "analysis_id": analysis_id,
                    "submitted_at": datetime.now(UTC).isoformat(),
                }
                await db.commit()

        for attempt in range(settings.virustotal_analysis_poll_attempts):
            await asyncio.sleep(settings.virustotal_analysis_poll_interval_seconds)
            await _reserve_or_raise()
            verdict = await virustotal.poll_analysis(analysis_id)
            if verdict is not None:
                return verdict
            logger.info(
                "analysis %s still running (poll %d)", analysis_id[:16], attempt + 1
            )
        raise virustotal.TransientError(
            f"analysis {analysis_id[:16]} did not complete within "
            f"{settings.virustotal_analysis_poll_attempts} polls"
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:  # pragma: no cover - best effort cleanup
            pass


async def _scan(version_id: uuid.UUID) -> dict[str, Any]:
    # --- load what the scan needs, then release the session while calling out ------
    async with SessionLocal() as db:
        version = await db.get(FileVersion, version_id)
        if version is None or version.deleted_at is not None:
            return {"status": "skipped", "reason": "version is gone"}
        if version.upload_id is not None:
            return {"status": "skipped", "reason": "upload still in flight"}
        if version.checksum_sha256 is None:
            # compute_checksum is this task's predecessor; without it there is nothing
            # to look up, and hashing here would duplicate that task's job.
            raise ValueError(
                f"version {version_id} has no checksum yet; compute_checksum must run first"
            )
        checksum = version.checksum_sha256
        storage_key = version.storage_key
        size_bytes = version.size_bytes
        file = await db.get(File, version.file_id)
        filename = file.name if file else "upload.bin"

        # 1. Cheapest possible answer: content we have already asked about.
        cached = await scan.cached_verdict(db, checksum)

    if cached is not None:
        settled = await _settle(version_id, cached, checksum)
        return {"status": settled, "source": "cache", "requests_spent": 0}

    # 2. Over the submission ceiling there is nothing to try, whatever the settings.
    if size_bytes > settings.virustotal_max_upload_limit_bytes:
        await _skip(
            version_id,
            f"file is {size_bytes} bytes, above VirusTotal's "
            f"{settings.virustotal_max_upload_limit_bytes} byte submission ceiling",
        )
        return {"status": ScanStatus.SCAN_SKIPPED.value, "requests_spent": 0}

    # 3. Hash lookup: one request, no content transmitted.
    #
    # The reservation comes BEFORE the version is marked `scanning`, and that order is
    # load-bearing. Marking first meant a file waiting for budget displayed "Scanning"
    # — a spinner that would not move for hours, which is exactly the dishonesty
    # Design.md §5 forbids. Found by M4 manual validation, not by the suite.
    await _reserve_or_raise()
    await _mark_scanning(version_id)
    verdict = await virustotal.lookup_hash(checksum)
    if verdict is not None:
        settled = await _settle(version_id, verdict, checksum)
        return {"status": settled, "source": "hash_lookup", "requests_spent": 1}

    # 4. A miss. By default the content stays here and the badge says so (§3b).
    if not settings.virustotal_submit_unknown_files:
        await _skip(
            version_id,
            "not previously seen by the scanner; content was not submitted because "
            "free-tier submissions are shared publicly",
        )
        return {"status": ScanStatus.SCAN_SKIPPED.value, "requests_spent": 1}

    verdict = await _submit_and_poll(version_id, storage_key, filename, size_bytes)
    settled = await _settle(version_id, verdict, checksum)
    return {"status": settled, "source": "submission"}


@celery_app.task(
    name="app.tasks.scan_tasks.scan_file_version",
    bind=True,
    max_retries=4,
    autoretry_for=(),
)
def scan_file_version(self, file_version_id: str) -> dict[str, Any]:
    """Resolve one version's scan status.

    Three outcomes are handled differently on purpose:

    - **No budget** — requeued with a countdown and *without* consuming a retry. Waiting
      for a window to reopen is not a failure, and counting it as one would exhaust the
      retry budget of a file uploaded on a busy day and leave it `scan_failed` when
      nothing had gone wrong.
    - **Misconfiguration** (missing or rejected API key) — no retry at all. Retrying a
      wrong key four times just delays the same answer; the version is left `pending` so
      that fixing the key and re-driving it works, rather than marked failed as though
      the file were at fault.
    - **Anything else** — `scan_failed` with exponential backoff, and after the retries
      are exhausted the file stays withheld until an admin override or a later scan.
    """
    version_id = uuid.UUID(file_version_id)
    try:
        return _run(_scan(version_id))

    except BudgetUnavailable as exc:
        # Jitter: without it, every task queued during an exhausted window wakes at the
        # same instant and immediately exhausts the reopened one.
        jitter = int.from_bytes(os.urandom(1), "big") % 30
        countdown = exc.retry_after_seconds + jitter
        # Put the badge back to "queued" while it waits. Skipped when an analysis is
        # genuinely in flight at VirusTotal, because then `scanning` is the truth and
        # resetting would also lose the analysis id and pay to submit the file again.
        _run(_release_scanning_claim(version_id))
        logger.info(
            "scan for version %s deferred %ds: %s", file_version_id, countdown, exc
        )
        raise self.retry(countdown=countdown, max_retries=None) from exc

    except virustotal.ConfigurationError as exc:
        # Left `pending`, not `scan_failed`: the file is fine, our configuration is not,
        # and `pending` is the state the stuck-scan sweep will pick back up once it is
        # fixed.
        logger.error("scanning is misconfigured, not retrying: %s", exc)
        return {"status": "misconfigured", "detail": str(exc)}

    except Exception as exc:
        logger.warning(
            "scan failed for version %s (%s)", file_version_id, type(exc).__name__
        )
        try:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
        except self.MaxRetriesExceededError:
            _run(
                _mark_failed(
                    version_id,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            return {"status": ScanStatus.SCAN_FAILED.value, "detail": str(exc)}


async def _mark_scanning(version_id: uuid.UUID) -> None:
    """Claim the scan, once there is budget to actually do it."""
    async with SessionLocal() as db:
        version = await db.get(FileVersion, version_id)
        if version is None or version.deleted_at is not None:
            return
        await scan.mark(db, version, ScanStatus.SCANNING)
        await db.commit()


async def _release_scanning_claim(version_id: uuid.UUID) -> None:
    """Return a starved version to `pending` so its badge says "queued", not "scanning"."""
    async with SessionLocal() as db:
        version = await db.get(FileVersion, version_id)
        if version is None or version.deleted_at is not None:
            return
        if version.scan_status is not ScanStatus.SCANNING:
            return
        if (version.scan_detail or {}).get("analysis_id"):
            # An analysis really is running at VirusTotal; `scanning` is accurate.
            return
        await scan.mark(db, version, ScanStatus.PENDING)
        await db.commit()


async def _mark_failed(version_id: uuid.UUID, reason: str) -> None:
    async with SessionLocal() as db:
        version = await db.get(FileVersion, version_id)
        if version is None or version.deleted_at is not None:
            return
        await scan.mark(
            db,
            version,
            ScanStatus.SCAN_FAILED,
            detail={
                "source": "virustotal",
                "error": reason,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
        await db.commit()


async def _stalled_version_ids() -> list[uuid.UUID]:
    """Versions whose scan never resolved (Architecture.md §3a).

    Two situations produce these, and both are invisible to the user as anything other
    than a file that stays undownloadable:

    - a worker killed mid-scan, leaving `scanning` with nothing scheduled;
    - a version left `pending` because the API key was wrong, which deliberately does
      not retry, so it needs something to restart it once the key is fixed.

    Only completed uploads are eligible: a version whose multipart session is still open
    has no object to scan, and re-driving it would hash a partial upload.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.scan_stuck_after_minutes)
    async with SessionLocal() as db:
        result = await db.execute(
            select(FileVersion.id).where(
                FileVersion.scan_status.in_([ScanStatus.PENDING, ScanStatus.SCANNING]),
                FileVersion.deleted_at.is_(None),
                FileVersion.upload_id.is_(None),
                FileVersion.checksum_sha256.is_not(None),
                FileVersion.created_at < cutoff,
            )
        )
        return [row[0] for row in result.all()]


@celery_app.task(name="app.tasks.scan_tasks.redrive_stalled_scans")
def redrive_stalled_scans() -> dict[str, Any]:
    """Re-queue scans that stopped without reaching a terminal state.

    Does nothing when scanning is disabled: every version would look stalled, and
    queueing them all would be a self-inflicted flood the moment the flag was turned on.
    """
    if not settings.scanning_enabled:
        return {"status": "skipped", "reason": "scanning is disabled"}

    version_ids = _run(_stalled_version_ids())
    for version_id in version_ids:
        scan_file_version.delay(str(version_id))
    if version_ids:
        logger.info("re-driving %d stalled scan(s)", len(version_ids))
    return {"status": "ok", "requeued": len(version_ids)}
