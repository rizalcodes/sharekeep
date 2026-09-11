"""The trash (Architecture.md §6a, §6b).

§6a promised a user-visible trash and a restore that revives exactly one deletion batch.
The restore existed from M2; a way to *see* what could be restored did not, so restore
needed a batch id the user had no way to find. This module lists deletions — one entry
per delete operation, because that is the unit restore works in — and decides whether
each can still come back.

Who deleted something is read from the audit log, not from a column: every delete has
written an audit row carrying its batch id since M2, and the append-only log is the one
place that already records it truthfully.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.content import File, FileVersion, Folder
from app.models.enums import AuditAction
from app.models.identity import AuditLog, User

# More than anyone will scroll through; the retention window keeps the real number low.
MAX_ENTRIES = 200


class GoneForGood(Exception):
    """The batch's bytes have been purged, or are due to be; restore must refuse."""


@dataclass(frozen=True)
class TrashEntry:
    deletion_batch_id: uuid.UUID
    kind: str  # "folder" or "file" — also which restore endpoint applies
    name: str
    folders: int
    files: int
    size_bytes: int
    deleted_at: datetime
    deleted_by_email: str | None
    purge_after: datetime
    restorable: bool
    blocked_reason: str | None


def retention_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=settings.soft_delete_retention_days)


async def assert_restorable(db: AsyncSession, batch_id: uuid.UUID) -> None:
    """Refuse a batch whose bytes are gone, or due to go before the restore could finish.

    Both restore paths call this. Without it, restoring after the sweep would revive rows
    that point at objects no longer in storage — files that list, and then fail to
    download, with nothing telling anyone why.
    """
    row = (
        await db.execute(
            select(
                func.bool_or(FileVersion.purged_at.is_not(None)),
                func.min(FileVersion.deleted_at),
            ).where(FileVersion.deletion_batch_id == batch_id)
        )
    ).one()
    any_purged, earliest = row
    if any_purged or (earliest is not None and earliest < retention_cutoff()):
        raise GoneForGood(
            f"This was deleted more than {settings.soft_delete_retention_days} days ago "
            "and its contents have been permanently removed."
        )


async def list_entries(db: AsyncSession, workspace_id: uuid.UUID) -> list[TrashEntry]:
    cutoff = retention_cutoff()

    folder_rows = (
        await db.execute(
            select(
                Folder.deletion_batch_id,
                Folder.id,
                Folder.name,
                Folder.parent_folder_id,
                Folder.deleted_at,
            ).where(
                Folder.workspace_id == workspace_id,
                Folder.deletion_batch_id.is_not(None),
                Folder.deleted_at >= cutoff,
            )
        )
    ).all()
    file_rows = (
        await db.execute(
            select(
                File.deletion_batch_id,
                File.id,
                File.name,
                File.folder_id,
                File.deleted_at,
            ).where(
                File.workspace_id == workspace_id,
                File.deletion_batch_id.is_not(None),
                File.deleted_at >= cutoff,
            )
        )
    ).all()

    batches: dict[uuid.UUID, dict] = {}
    for batch, folder_id, name, parent_id, deleted_at in folder_rows:
        entry = batches.setdefault(batch, {"folders": {}, "files": [], "deleted_at": deleted_at})
        entry["folders"][folder_id] = (name, parent_id)
    for batch, _file_id, name, folder_id, deleted_at in file_rows:
        entry = batches.setdefault(batch, {"folders": {}, "files": [], "deleted_at": deleted_at})
        entry["files"].append((name, folder_id))

    if not batches:
        return []
    ids = list(batches)

    sizes = dict(
        (
            await db.execute(
                select(
                    FileVersion.deletion_batch_id,
                    func.coalesce(func.sum(FileVersion.size_bytes), 0),
                )
                .where(
                    FileVersion.deletion_batch_id.in_(ids),
                    FileVersion.upload_id.is_(None),
                    FileVersion.purged_at.is_(None),
                )
                .group_by(FileVersion.deletion_batch_id)
            )
        ).all()
    )
    purged = {
        batch
        for (batch,) in (
            await db.execute(
                select(FileVersion.deletion_batch_id)
                .where(
                    FileVersion.deletion_batch_id.in_(ids),
                    FileVersion.purged_at.is_not(None),
                )
                .distinct()
            )
        ).all()
    }

    actors: dict[str, str | None] = {}
    audit_rows = await db.execute(
        select(AuditLog.audit_metadata["batch"].astext, User.email)
        .outerjoin(User, User.id == AuditLog.actor_id)
        .where(
            AuditLog.workspace_id == workspace_id,
            AuditLog.action.in_([AuditAction.DELETE, AuditAction.FOLDER_DELETED]),
            AuditLog.audit_metadata["batch"].astext.in_([str(b) for b in ids]),
        )
    )
    for batch_text, email in audit_rows.all():
        actors[batch_text] = email

    # Which parents are themselves deleted, so an entry can say it is blocked and why.
    parent_ids = {
        parent
        for data in batches.values()
        for parent in (
            [p for _, p in data["folders"].values()] + [f for _, f in data["files"]]
        )
        if parent is not None
    }
    deleted_parents = set()
    if parent_ids:
        deleted_parents = {
            fid
            for (fid,) in (
                await db.execute(
                    select(Folder.id).where(
                        Folder.id.in_(parent_ids), Folder.deleted_at.is_not(None)
                    )
                )
            ).all()
        }

    entries: list[TrashEntry] = []
    for batch, data in batches.items():
        if batch in purged:
            continue  # nothing left to act on; restore refuses it anyway
        folders, files = data["folders"], data["files"]
        if folders:
            # The folder the user actually chose: the one whose parent is outside it.
            roots = [(n, p) for n, p in folders.values() if p not in folders]
            name, parent = roots[0] if roots else next(iter(folders.values()))
            kind = "folder"
        else:
            name, parent = files[0]
            kind = "file"
        blocked = (
            "The folder it was in has also been deleted. Restore that first."
            if parent is not None and parent in deleted_parents
            else None
        )
        deleted_at = data["deleted_at"]
        entries.append(
            TrashEntry(
                deletion_batch_id=batch,
                kind=kind,
                name=name,
                folders=len(folders),
                files=len(files),
                size_bytes=int(sizes.get(batch, 0)),
                deleted_at=deleted_at,
                deleted_by_email=actors.get(str(batch)),
                purge_after=deleted_at + timedelta(days=settings.soft_delete_retention_days),
                restorable=blocked is None,
                blocked_reason=blocked,
            )
        )

    entries.sort(key=lambda e: e.deleted_at, reverse=True)
    return entries[:MAX_ENTRIES]
