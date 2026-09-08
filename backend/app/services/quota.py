"""Storage quota accounting (Architecture.md §6).

**What the counter counts:** the sum of `size_bytes` over all non-deleted
`file_versions` rows in the workspace — every retained version, not just current ones.
The storage prefix physically holds every version, so this is the only definition under
which the reconciliation task can ever agree with reality. Counting current versions
only would make reconciliation report drift permanently.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.content import File, FileVersion
from app.models.identity import Workspace


class QuotaExceeded(Exception):
    """The workspace does not have room for this upload."""

    def __init__(self, requested: int, available: int) -> None:
        super().__init__(
            f"Not enough space: {requested} bytes requested, {available} available."
        )
        self.requested = requested
        self.available = available


class FileTooLarge(Exception):
    """Above the storage provider's own per-file ceiling."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"File is {size} bytes; the limit is {limit} bytes.")
        self.size = size
        self.limit = limit


async def lock_workspace(db: AsyncSession, workspace_id: uuid.UUID) -> Workspace | None:
    """Take a row lock before reading the counter.

    Without this, two concurrent uploads both read the old total, both pass the check,
    and the workspace ends up over quota. A check that is not atomic with the thing it
    guards is not a check.
    """
    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def reserve(
    db: AsyncSession, workspace_id: uuid.UUID, size_bytes: int
) -> Workspace:
    """Check the provider ceiling and the workspace quota, then reserve the space.

    Reserving up front (rather than at upload-complete) is deliberate: the bytes are
    already being written to storage during the upload, so the accounting has to
    assume they will land.
    """
    if size_bytes > settings.max_file_size_bytes:
        raise FileTooLarge(size_bytes, settings.max_file_size_bytes)

    workspace = await lock_workspace(db, workspace_id)
    if workspace is None:
        raise QuotaExceeded(size_bytes, 0)

    available = workspace.storage_quota_bytes - workspace.storage_used_bytes
    if size_bytes > available:
        raise QuotaExceeded(size_bytes, max(available, 0))

    workspace.storage_used_bytes += size_bytes
    await db.flush()
    return workspace


async def release(db: AsyncSession, workspace_id: uuid.UUID, size_bytes: int) -> None:
    """Return space to the workspace: a deletion, a pruned version, or a failed upload."""
    if size_bytes <= 0:
        return
    workspace = await lock_workspace(db, workspace_id)
    if workspace is None:
        return
    # Never let the counter go negative — a drifted counter that reads below zero
    # would make the reconciliation report meaningless.
    workspace.storage_used_bytes = max(0, workspace.storage_used_bytes - size_bytes)
    await db.flush()


async def actual_usage(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    """Recompute usage from the version rows themselves."""
    result = await db.execute(
        select(func.coalesce(func.sum(FileVersion.size_bytes), 0))
        .select_from(FileVersion)
        .join(File, File.id == FileVersion.file_id)
        .where(
            File.workspace_id == workspace_id,
            FileVersion.deleted_at.is_(None),
            # An upload that never completed holds no bytes worth counting.
            FileVersion.upload_id.is_(None),
        )
    )
    return int(result.scalar_one())


async def reconcile(db: AsyncSession, workspace_id: uuid.UUID) -> dict[str, int]:
    """Compare the stored counter against the real total and correct any drift."""
    workspace = await lock_workspace(db, workspace_id)
    if workspace is None:
        return {"stored": 0, "actual": 0, "drift": 0}
    actual = await actual_usage(db, workspace_id)
    drift = workspace.storage_used_bytes - actual
    if drift:
        workspace.storage_used_bytes = actual
        await db.flush()
    return {"stored": workspace.storage_used_bytes, "actual": actual, "drift": drift}
