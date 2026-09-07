"""Scheduled maintenance tasks.

M0 wires the schedule and proves the worker/beat round trip. The real implementations
land with the milestones that give them something to reconcile:
  - reconcile_storage_usage        -> M2 (quota enforcement)
  - sweep_abandoned_multipart_uploads -> M3 (chunked upload)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.core.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.maintenance_tasks.ping")
def ping() -> dict[str, str]:
    """M0 wiring check: proves the API can enqueue and a worker can execute."""
    return {"status": "pong", "at": datetime.now(UTC).isoformat()}


@celery_app.task(name="app.tasks.maintenance_tasks.reconcile_storage_usage")
def reconcile_storage_usage() -> dict[str, str]:
    """Recompute real S3 usage per workspace prefix against workspaces.storage_used_bytes.

    Architecture.md §6 — verify the artifact, don't just trust the counter.
    """
    logger.info("reconcile_storage_usage: not implemented until M2")
    return {"status": "not_implemented", "milestone": "M2"}


@celery_app.task(name="app.tasks.maintenance_tasks.sweep_abandoned_multipart_uploads")
def sweep_abandoned_multipart_uploads() -> dict[str, str]:
    """Abort S3 multipart uploads older than the TTL and reap their pending rows.

    Architecture.md §2 — orphaned parts are a real cost and consistency leak.
    """
    logger.info("sweep_abandoned_multipart_uploads: not implemented until M3")
    return {"status": "not_implemented", "milestone": "M3"}
