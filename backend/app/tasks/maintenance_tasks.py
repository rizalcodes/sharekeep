"""Scheduled maintenance tasks."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core import async_bridge
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.content import File, FileVersion
from app.models.identity import Workspace
from app.services import quota, storage

logger = logging.getLogger(__name__)


def _run(coro):
    """Celery tasks are synchronous; the data layer is async.

    Shares the per-process loop (app.core.async_bridge). These tasks are rare enough
    that `asyncio.run` never visibly broke here, but it was the same latent fault.
    """
    return async_bridge.run(coro)


@celery_app.task(name="app.tasks.maintenance_tasks.ping")
def ping() -> dict[str, str]:
    """Wiring check: proves the API can enqueue and a worker can execute."""
    return {"status": "pong", "at": datetime.now(UTC).isoformat()}


async def _reconcile_all() -> dict[str, Any]:
    """Architecture.md §6 — verify the artifact, don't just trust the counter."""
    corrected: list[dict[str, Any]] = []
    async with SessionLocal() as db:
        result = await db.execute(select(Workspace.id))
        workspace_ids = [row[0] for row in result.all()]
        for workspace_id in workspace_ids:
            report = await quota.reconcile(db, workspace_id)
            if report["drift"]:
                corrected.append({"workspace_id": str(workspace_id), **report})
                logger.warning(
                    "quota drift corrected for workspace %s: %s bytes",
                    workspace_id,
                    report["drift"],
                )
        await db.commit()
    return {"workspaces_checked": len(workspace_ids), "corrected": corrected}


@celery_app.task(name="app.tasks.maintenance_tasks.reconcile_storage_usage")
def reconcile_storage_usage() -> dict[str, Any]:
    return _run(_reconcile_all())


async def _sweep_uploads() -> dict[str, Any]:
    """Abort multipart sessions that were never completed.

    On AWS S3 a bucket lifecycle rule would catch these as a second line of defence.
    **Supabase Storage has no lifecycle rules, so this task is the only thing standing
    between an abandoned browser upload and orphaned parts accumulating forever**
    (Architecture.md §2).
    """
    cutoff = datetime.now(UTC) - timedelta(hours=settings.abandoned_multipart_ttl_hours)
    aborted = 0
    reclaimed = 0

    async with SessionLocal() as db:
        # Join the file so the owning workspace comes back with the version — the
        # reserved quota has to be returned to the right workspace.
        result = await db.execute(
            select(FileVersion, File.workspace_id)
            .join(File, File.id == FileVersion.file_id)
            .where(
                FileVersion.upload_id.is_not(None),
                FileVersion.upload_started_at < cutoff,
            )
        )
        for version, workspace_id in result.all():
            storage.abort_multipart_upload(version.storage_key, version.upload_id)
            version.upload_id = None
            version.deleted_at = datetime.now(UTC)
            await quota.release(db, workspace_id, version.size_bytes)
            aborted += 1
            reclaimed += version.size_bytes
        await db.commit()

    if aborted:
        logger.info("swept %s abandoned uploads, reclaimed %s bytes", aborted, reclaimed)
    return {"aborted": aborted, "bytes_reclaimed": reclaimed}


@celery_app.task(name="app.tasks.maintenance_tasks.sweep_abandoned_multipart_uploads")
def sweep_abandoned_multipart_uploads() -> dict[str, Any]:
    return _run(_sweep_uploads())


@celery_app.task(name="app.tasks.maintenance_tasks.storage_keepalive")
def storage_keepalive() -> dict[str, Any]:
    """Keep the Supabase project from being auto-paused for inactivity.

    Free-tier projects pause after 7 consecutive days without a request, which would
    take a portfolio demo offline for anyone visiting weeks later. Temporary: removed
    when the project returns to AWS S3 (PRD §10).
    """
    result = storage.check_connectivity()
    if not result["ok"]:
        logger.warning("storage_keepalive: storage unreachable (%s)", result.get("detail"))
    return result
