"""Scan bookkeeping: the verdict cache, the access rule, and the admin override.

The VirusTotal conversation lives in `virustotal.py` and the request budget in
`vt_budget.py`. This module owns what the application does with an answer once it has
one, and — more importantly — who may read a file given what we know about it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.content import File, FileVersion, ScanResult
from app.models.enums import ScanStatus
from app.services.virustotal import Verdict

logger = logging.getLogger(__name__)


class ScanError(Exception):
    """Raised for refusals the caller should turn into an HTTP status."""


class OverrideNotPermitted(ScanError):
    """The version is not in a state an override can act on."""


# --------------------------------------------------------------------------- access --
#
# Architecture.md §3 is the single source of truth for whether bytes may be served, and
# since M4 the answer depends on two things, not one: the scan status *and* whether an
# admin recorded an override on that specific version. `ScanStatus` alone cannot answer
# it, which is why the enum property is named `allows_download_without_override`.


def version_is_downloadable(version: FileVersion) -> bool:
    """The whole access rule, in one place.

    An override releases only the states it is *permitted* to release. `record_override`
    already refuses to create one on `pending` or `scanning`, so an override sitting on
    an unresolved scan should be impossible — but the access rule must not depend on
    that being true. A stray override row from a migration, a manual edit, or some later
    code path would otherwise hand out an unscanned file, and this is the one function
    standing between a request and the bytes.
    """
    if version.scan_status.allows_download_without_override:
        return True
    return version.scan_status.is_overridable and has_override(version)


def has_override(version: FileVersion) -> bool:
    """An override is all three fields or none — a DB CHECK enforces that — so testing
    the timestamp is sufficient and cannot be half-true."""
    return version.scan_override_at is not None


def download_refusal_reason(version: FileVersion) -> str:
    """Why a download was refused, in words a user can act on.

    Deliberately specific per state. "Not available" would leave someone unable to tell
    a scan still running from a file the scanner rejected — a distinction that decides
    whether waiting will help.
    """
    status = version.scan_status
    if status is ScanStatus.PENDING:
        return (
            "This file is queued for a malware scan and cannot be downloaded yet. "
            "Scanner capacity is limited, so this may take a while."
        )
    if status is ScanStatus.SCANNING:
        return "This file is being scanned for malware and cannot be downloaded yet."
    if status is ScanStatus.FLAGGED:
        return (
            "This file was flagged as malicious by the scanner and cannot be "
            "downloaded. A workspace owner or admin can review and release it."
        )
    if status is ScanStatus.SCAN_FAILED:
        return (
            "This file could not be scanned successfully, so it is withheld. A "
            "workspace owner or admin can review and release it."
        )
    return f"This file cannot be downloaded while its scan status is '{status.value}'."


# ---------------------------------------------------------------------- verdict cache --


async def cached_verdict(db: AsyncSession, checksum: str) -> Verdict | None:
    """A verdict already known for this content, if it is still fresh.

    Costs zero VirusTotal requests, which against ~500/day is the difference between
    a workspace of duplicate files being scannable and not (Architecture.md §3c).
    """
    cutoff = datetime.now(UTC) - timedelta(days=settings.scan_cache_ttl_days)
    result = await db.execute(
        select(ScanResult).where(
            ScanResult.checksum_sha256 == checksum,
            ScanResult.checked_at >= cutoff,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    detail = dict(row.detail or {})
    # Marked so a version's scan_detail says plainly that no request was spent, rather
    # than looking like a fresh answer from VirusTotal.
    detail["from_cache"] = True
    detail["cached_at"] = row.checked_at.isoformat()
    return Verdict(status=row.verdict, detail=detail)


async def remember_verdict(db: AsyncSession, checksum: str, verdict: Verdict) -> None:
    """Upsert one row per hash.

    Only settled verdicts reach here; a DB CHECK rejects anything else, because caching
    `pending` or `scan_failed` would serve a non-answer back as an answer. Re-lookups
    replace the row in place: this is a cache, not a history, and a growing pile of
    stale verdicts per hash would need its own pruning.
    """
    if verdict.status not in (ScanStatus.CLEAN, ScanStatus.FLAGGED):
        raise ValueError(
            f"Only settled verdicts are cacheable, refusing to cache {verdict.status!r}."
        )
    now = datetime.now(UTC)
    detail = {k: v for k, v in verdict.detail.items() if k not in ("from_cache", "cached_at")}
    statement = (
        pg_insert(ScanResult)
        .values(
            id=uuid.uuid4(),
            checksum_sha256=checksum,
            verdict=verdict.status,
            detail=detail,
            checked_at=now,
        )
        .on_conflict_do_update(
            index_elements=[ScanResult.checksum_sha256],
            set_={
                "verdict": verdict.status,
                "detail": detail,
                "checked_at": now,
                "updated_at": now,
            },
        )
    )
    await db.execute(statement)


# ------------------------------------------------------------------- applying results --


async def apply_verdict(
    db: AsyncSession, version: FileVersion, verdict: Verdict
) -> FileVersion:
    """Write a settled verdict onto the version and, if current, onto the file."""
    version.scan_status = verdict.status
    version.scan_detail = verdict.detail
    await _mirror_onto_file(db, version)
    return version


async def mark(
    db: AsyncSession,
    version: FileVersion,
    status: ScanStatus,
    *,
    detail: dict[str, Any] | None = None,
) -> FileVersion:
    """Set a transient or terminal state that did not come from an engine verdict."""
    version.scan_status = status
    if detail is not None:
        version.scan_detail = detail
    await _mirror_onto_file(db, version)
    return version


async def _mirror_onto_file(db: AsyncSession, version: FileVersion) -> None:
    """`files.scan_status` mirrors the *current* version only (Schema.md §6).

    A scan finishing on a superseded version must not change what the file reports:
    another upload has already moved on, and overwriting the mirror would make the file
    claim the wrong version's result.
    """
    file = await db.get(File, version.file_id)
    if file is not None and file.current_version_id == version.id:
        file.scan_status = version.scan_status
        file.updated_at = datetime.now(UTC)


# ------------------------------------------------------------------------- override ---


async def record_override(
    db: AsyncSession,
    version: FileVersion,
    *,
    user_id: uuid.UUID,
    reason: str,
) -> FileVersion:
    """Release one version despite its scan status (Architecture.md §3d).

    `scan_status` is deliberately left alone. Setting it to `clean` would assert a
    re-scan that never happened, and the record has to keep saying "this was flagged,
    and a named person chose to release it anyway".

    Scoped to a single version: a later upload of the same file starts unreleased,
    because the override was a judgement about specific content, not a standing
    exemption for a filename.
    """
    if not version.scan_status.is_overridable:
        raise OverrideNotPermitted(
            f"A version with scan status '{version.scan_status.value}' cannot be "
            "overridden. Only flagged or failed scans can be released."
        )
    reason = (reason or "").strip()
    if not reason:
        # The DB CHECK would reject a null reason anyway; refusing here gives a usable
        # message instead of an integrity error.
        raise OverrideNotPermitted("An override requires a stated reason.")

    version.scan_override_at = datetime.now(UTC)
    version.scan_override_by = user_id
    version.scan_override_reason = reason
    return version


# ------------------------------------------------------------------ presentation ----


def _reason_from_detail(detail: dict[str, Any] | None) -> str | None:
    """The "why" behind a `scan_skipped` badge (Design.md §5).

    The badge is shared by three different situations — scanning switched off, a file
    above the scanner's size ceiling, and a file the scanner had never seen whose
    content was deliberately not uploaded. "Not scanned" with no reason invites the
    reader to assume the most reassuring one.
    """
    if not detail:
        return None
    reason = detail.get("reason")
    return reason if isinstance(reason, str) else None


async def presentation_for(
    db: AsyncSession, files: list[File]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Scan facts for a set of files, resolved from their current versions.

    One query for every file in the listing rather than one per file: the override and
    the skip reason live on the version (Schema.md §7 keeps them off `files` so there is
    a single source of truth), and a listing that fetched them individually would be
    N+1 on the main browsing path.
    """
    version_ids = [f.current_version_id for f in files if f.current_version_id]
    versions: dict[uuid.UUID, FileVersion] = {}
    if version_ids:
        result = await db.execute(
            select(FileVersion).where(FileVersion.id.in_(version_ids))
        )
        versions = {v.id: v for v in result.scalars().all()}

    presentation: dict[uuid.UUID, dict[str, Any]] = {}
    for file in files:
        version = versions.get(file.current_version_id) if file.current_version_id else None
        if version is None:
            presentation[file.id] = {"is_downloadable": False}
            continue
        presentation[file.id] = {
            "is_downloadable": version_is_downloadable(version),
            "scan_reason": _reason_from_detail(version.scan_detail),
            "scan_override_at": version.scan_override_at,
            "scan_override_by": version.scan_override_by,
            "scan_override_reason": version.scan_override_reason,
        }
    return presentation
