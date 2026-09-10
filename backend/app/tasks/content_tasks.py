"""Post-upload content processing (Architecture.md §3).

The checksum is computed here rather than inside the scan task, and that separation was
a correction, not a preference: while §3 had it as step 1 of scanning, a deployment with
`scanning_enabled=false` computed no checksum at all — so the integrity guarantee
Schema.md §7 describes silently did not exist. Integrity is not a scanning feature.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from app.core import async_bridge
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.content import FileVersion
from app.services import storage

logger = logging.getLogger(__name__)


def _run(coro):
    """Celery tasks are synchronous; the data layer is async.

    Goes through `async_bridge` rather than `asyncio.run`: a fresh loop per task
    invalidates the pooled connections held by the engine and the Redis clients, which
    breaks the *second* task in any worker process.
    """
    return async_bridge.run(coro)


def _hash_object(storage_key: str) -> tuple[str, int]:
    """SHA-256 over the stored object, streamed."""
    digest = hashlib.sha256()
    total = 0
    for chunk in storage.stream_object(storage_key):
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


async def _compute(file_version_id: uuid.UUID) -> dict[str, object]:
    async with SessionLocal() as db:
        version = await db.get(FileVersion, file_version_id)
        if version is None or version.deleted_at is not None:
            return {"status": "skipped", "reason": "version is gone"}
        if version.upload_id is not None:
            # The multipart session is still open, so there is no complete object to
            # hash yet. The upload-complete path enqueues this task; a stray call
            # arriving early must not hash a partial object.
            return {"status": "skipped", "reason": "upload still in flight"}
        storage_key = version.storage_key
        expected_size = version.size_bytes
        already = version.checksum_sha256

    if already:
        # Content at a storage key never changes: a new version writes a new key. So a
        # checksum that exists is still correct, and re-hashing would only spend egress.
        checksum = already
        logger.info("checksum already present for version %s", file_version_id)
    else:
        checksum, read_bytes = _hash_object(storage_key)
        if read_bytes != expected_size:
            # Hashing a truncated read and storing the result would produce an integrity
            # value that certifies the wrong bytes - worse than having none.
            raise ValueError(
                f"read {read_bytes} bytes from storage but the version records "
                f"{expected_size}; refusing to store a checksum over a partial object"
            )
        async with SessionLocal() as db:
            version = await db.get(FileVersion, file_version_id)
            if version is None or version.deleted_at is not None:
                return {"status": "skipped", "reason": "version is gone"}
            version.checksum_sha256 = checksum
            await db.commit()

    queued = False
    if settings.scanning_enabled:
        from app.tasks import scan_tasks

        scan_tasks.scan_file_version.delay(str(file_version_id))
        queued = True

    return {
        "status": "ok",
        "checksum_sha256": checksum,
        "scan_queued": queued,
    }


@celery_app.task(
    name="app.tasks.content_tasks.compute_checksum",
    bind=True,
    max_retries=5,
    default_retry_delay=30,
)
def compute_checksum(self, file_version_id: str) -> dict[str, object]:
    """Hash a completed upload, then hand off to scanning if it is enabled.

    Retries on failure: the object exists and storage being briefly unreachable is not
    a reason to leave a version permanently without an integrity checksum.
    """
    try:
        return _run(_compute(uuid.UUID(file_version_id)))
    except Exception as exc:
        logger.warning(
            "checksum failed for version %s (%s), retrying", file_version_id, type(exc).__name__
        )
        raise self.retry(exc=exc) from exc
