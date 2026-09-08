"""Folder tree operations (Schema.md §5, Architecture.md §4.1 and §6a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import File, FileVersion, Folder
from app.models.identity import User, Workspace
from app.services import permissions, quota


class FolderError(Exception):
    """Client-safe folder operation failure."""


class NameConflict(FolderError):
    """A sibling of that name already exists.

    Rejected rather than silently auto-renamed: a user who asked for "Reports" and got
    "Reports (1)" has been given something they did not ask for.
    """


async def get_folder(
    db: AsyncSession, workspace_id: uuid.UUID, folder_id: uuid.UUID
) -> Folder | None:
    result = await db.execute(
        select(Folder).where(
            Folder.id == folder_id,
            Folder.workspace_id == workspace_id,
            Folder.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def depth_of(db: AsyncSession, folder_id: uuid.UUID | None) -> int:
    if folder_id is None:
        return 0
    return len(await permissions.ancestor_chain(db, folder_id))


async def create(
    db: AsyncSession,
    *,
    workspace: Workspace,
    name: str,
    parent_folder_id: uuid.UUID | None,
    created_by: User,
) -> Folder:
    if parent_folder_id is not None:
        parent = await get_folder(db, workspace.id, parent_folder_id)
        if parent is None:
            raise FolderError("Parent folder not found.")
        if await depth_of(db, parent_folder_id) >= permissions.MAX_FOLDER_DEPTH:
            raise permissions.DepthLimitExceeded(
                f"Folders may not nest deeper than {permissions.MAX_FOLDER_DEPTH} levels."
            )

    folder = Folder(
        workspace_id=workspace.id,
        parent_folder_id=parent_folder_id,
        name=name,
        created_by=created_by.id,
    )
    db.add(folder)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise NameConflict("A folder with that name already exists here.") from exc
    return folder


async def rename_or_move(
    db: AsyncSession,
    *,
    folder: Folder,
    name: str | None = None,
    new_parent_id: uuid.UUID | None = None,
    move: bool = False,
) -> Folder:
    if move:
        # Guard the cycle BEFORE writing: a cycle makes the ancestor walk that every
        # permission check depends on non-terminating.
        await permissions.assert_move_is_acyclic(db, folder.id, new_parent_id)
        if new_parent_id is not None:
            parent = await get_folder(db, folder.workspace_id, new_parent_id)
            if parent is None:
                raise FolderError("Target folder not found.")
        # The subtree moves with it, so the deepest descendant must still fit.
        subtree_depth = await _subtree_height(db, folder.id)
        target_depth = await depth_of(db, new_parent_id)
        if target_depth + subtree_depth >= permissions.MAX_FOLDER_DEPTH:
            raise permissions.DepthLimitExceeded(
                "That move would nest folders deeper than "
                f"{permissions.MAX_FOLDER_DEPTH} levels."
            )
        folder.parent_folder_id = new_parent_id

    if name is not None:
        folder.name = name

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise NameConflict("A folder with that name already exists there.") from exc
    return folder


async def _subtree_height(db: AsyncSession, folder_id: uuid.UUID) -> int:
    """How many levels exist below this folder (0 if it has no children)."""
    result = await db.execute(
        text(
            """
            WITH RECURSIVE sub AS (
                SELECT id, 0 AS depth FROM folders
                WHERE id = :root AND deleted_at IS NULL
                UNION ALL
                SELECT f.id, sub.depth + 1
                FROM folders f JOIN sub ON f.parent_folder_id = sub.id
                WHERE f.deleted_at IS NULL AND sub.depth < :max_depth
            )
            SELECT COALESCE(MAX(depth), 0) FROM sub
            """
        ),
        {"root": folder_id, "max_depth": permissions.MAX_FOLDER_DEPTH},
    )
    return int(result.scalar_one())


async def descendant_ids(db: AsyncSession, folder_id: uuid.UUID) -> list[uuid.UUID]:
    """The folder plus every folder beneath it, live rows only."""
    result = await db.execute(
        text(
            """
            WITH RECURSIVE sub AS (
                SELECT id, 0 AS depth FROM folders
                WHERE id = :root AND deleted_at IS NULL
                UNION ALL
                SELECT f.id, sub.depth + 1
                FROM folders f JOIN sub ON f.parent_folder_id = sub.id
                WHERE f.deleted_at IS NULL AND sub.depth < :max_depth
            )
            SELECT id FROM sub
            """
        ),
        {"root": folder_id, "max_depth": permissions.MAX_FOLDER_DEPTH},
    )
    return [row.id for row in result.all()]


async def breadcrumb(db: AsyncSession, folder_id: uuid.UUID) -> list[Folder]:
    """Root-first path to the folder, for navigation."""
    chain = await permissions.ancestor_chain(db, folder_id)
    if not chain:
        return []
    result = await db.execute(select(Folder).where(Folder.id.in_(chain)))
    by_id = {f.id: f for f in result.scalars().all()}
    return [by_id[fid] for fid in reversed(chain) if fid in by_id]


async def soft_delete(
    db: AsyncSession, folder: Folder
) -> tuple[uuid.UUID, int, int]:
    """Soft-delete a folder and everything under it under one batch id.

    Returns (batch_id, folders_deleted, bytes_released). Architecture.md §6a: restore
    revives exactly this batch, so items the user deleted earlier stay deleted.
    """
    batch_id = uuid.uuid4()
    now = datetime.now(UTC)
    folder_ids = await descendant_ids(db, folder.id)

    files_result = await db.execute(
        select(File).where(File.folder_id.in_(folder_ids), File.deleted_at.is_(None))
    )
    files = list(files_result.scalars().all())
    file_ids = [f.id for f in files]

    released = 0
    if file_ids:
        versions_result = await db.execute(
            select(FileVersion).where(
                FileVersion.file_id.in_(file_ids), FileVersion.deleted_at.is_(None)
            )
        )
        for version in versions_result.scalars().all():
            version.deleted_at = now
            version.deletion_batch_id = batch_id
            if version.upload_id is None:
                released += version.size_bytes

    for file in files:
        file.deleted_at = now
        file.deletion_batch_id = batch_id

    subtree_result = await db.execute(select(Folder).where(Folder.id.in_(folder_ids)))
    folders = list(subtree_result.scalars().all())
    for row in folders:
        row.deleted_at = now
        row.deletion_batch_id = batch_id

    await db.flush()
    if released:
        await quota.release(db, folder.workspace_id, released)
    return batch_id, len(folders), released


async def restore_batch(
    db: AsyncSession, workspace: Workspace, batch_id: uuid.UUID
) -> tuple[int, int]:
    """Revive exactly one deletion batch. Returns (rows_restored, bytes_reclaimed)."""
    folders_result = await db.execute(
        select(Folder).where(
            Folder.workspace_id == workspace.id, Folder.deletion_batch_id == batch_id
        )
    )
    folders = list(folders_result.scalars().all())
    files_result = await db.execute(
        select(File).where(
            File.workspace_id == workspace.id, File.deletion_batch_id == batch_id
        )
    )
    files = list(files_result.scalars().all())
    if not folders and not files:
        raise FolderError("Nothing to restore for that deletion.")

    file_ids = [f.id for f in files]
    versions: list[FileVersion] = []
    if file_ids:
        versions_result = await db.execute(
            select(FileVersion).where(
                FileVersion.file_id.in_(file_ids),
                FileVersion.deletion_batch_id == batch_id,
            )
        )
        versions = list(versions_result.scalars().all())

    reclaim = sum(v.size_bytes for v in versions if v.upload_id is None)
    if reclaim:
        # Refuse rather than silently push the workspace over its limit.
        await quota.reserve(db, workspace.id, reclaim)

    # A parent that is still deleted would leave the restored rows unreachable.
    live_ids = {f.id for f in folders}
    for folder in folders:
        parent_id = folder.parent_folder_id
        if parent_id is not None and parent_id not in live_ids:
            parent = await db.get(Folder, parent_id)
            if parent is None or parent.deleted_at is not None:
                raise FolderError(
                    "The folder this was in has also been deleted. Restore that first."
                )

    for row in (*folders, *files, *versions):
        row.deleted_at = None
        row.deletion_batch_id = None

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise NameConflict(
            "Something with that name now exists where this was deleted from."
        ) from exc
    return len(folders) + len(files), reclaim
