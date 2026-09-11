"""Folder endpoints (Schema.md §5, Architecture.md §4.1, §4.6, §6a).

Every handler resolves access through `services/permissions.py`. No handler queries
membership or grants for itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, or_, select

from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession, ViewerAccess
from app.models.content import File, FilePermission, Folder, ShareLink
from app.models.enums import AuditAction, AuditTargetType, WorkspaceRole
from app.schemas.content import (
    BreadcrumbEntry,
    DeletionReceipt,
    FileOut,
    FolderCreate,
    FolderListing,
    FolderOut,
    FolderUpdate,
    RestoreRequest,
)
from app.services import audit, permissions, scan, trash
from app.services import folders as folder_service

router = APIRouter(
    prefix=f"{settings.api_v1_prefix}/workspaces/{{workspace_id}}/folders",
    tags=["folders"],
)


def _translate(exc: Exception) -> HTTPException:
    """One place that maps service failures to status codes.

    NotAMember becomes 404 rather than 403 so a caller cannot probe which ids exist.
    """
    if isinstance(exc, permissions.NotAMember):
        return HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    if isinstance(exc, permissions.InsufficientRole):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, permissions.CycleError | permissions.DepthLimitExceeded):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, folder_service.NameConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, folder_service.FolderError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


async def _load(db, workspace_id: uuid.UUID, folder_id: uuid.UUID) -> Folder:
    folder = await folder_service.get_folder(db, workspace_id, folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found.")
    return folder


@router.get("", response_model=FolderListing)
async def list_root(access: ViewerAccess, user: CurrentUser, db: DbSession) -> FolderListing:
    """Contents of the workspace root."""
    workspace, _ = access
    return await _listing(db, workspace.id, None, user)


@router.get("/{folder_id}", response_model=FolderListing)
async def list_folder(
    folder_id: uuid.UUID, access: ViewerAccess, user: CurrentUser, db: DbSession
) -> FolderListing:
    workspace, _ = access
    folder = await _load(db, workspace.id, folder_id)
    try:
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=WorkspaceRole.VIEWER,
            folder_id=folder.id,
        )
    except permissions.PermissionError_ as exc:
        raise _translate(exc) from exc
    return await _listing(db, workspace.id, folder, user)


async def _listing(db, workspace_id: uuid.UUID, folder: Folder | None, user) -> FolderListing:
    folder_id = folder.id if folder else None
    child_folders = await db.execute(
        select(Folder)
        .where(
            Folder.workspace_id == workspace_id,
            Folder.parent_folder_id.is_(None)
            if folder_id is None
            else Folder.parent_folder_id == folder_id,
            Folder.deleted_at.is_(None),
        )
        .order_by(Folder.name)
    )
    child_files = await db.execute(
        select(File)
        .where(
            File.workspace_id == workspace_id,
            File.folder_id.is_(None) if folder_id is None else File.folder_id == folder_id,
            File.deleted_at.is_(None),
        )
        .order_by(File.name)
    )
    trail = await folder_service.breadcrumb(db, folder_id) if folder_id else []
    files = list(child_files.scalars().all())
    folders = list(child_folders.scalars().all())
    # One query for every file's current version, not one per file: the override and
    # skip reason live on the version, and this is the main browsing path.
    facts = await scan.presentation_for(db, files)
    marks = await _indicator_facts(db, workspace_id, folder_id, user, files, folders)
    return FolderListing(
        folder=FolderOut.model_validate(folder) if folder else None,
        breadcrumb=[BreadcrumbEntry(id=f.id, name=f.name) for f in trail],
        my_role=marks["here"],
        folders=[
            FolderOut(**{**FolderOut.model_validate(f).model_dump(), **marks["folders"][f.id]})
            for f in folders
        ],
        files=[
            FileOut(
                **{
                    **FileOut.model_validate(f).model_dump(),
                    **facts[f.id],
                    **marks["files"][f.id],
                }
            )
            for f in files
        ],
    )


@router.post("", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
async def create_folder(
    payload: FolderCreate,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> FolderOut:
    workspace, _ = access
    try:
        # Editor+ on the PARENT, not on the workspace: a narrowing grant on a subtree
        # has to be able to stop a viewer creating folders inside it.
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=WorkspaceRole.EDITOR,
            folder_id=payload.parent_folder_id,
        )
        folder = await folder_service.create(
            db,
            workspace=workspace,
            name=payload.name,
            parent_folder_id=payload.parent_folder_id,
            created_by=user,
        )
    except (permissions.PermissionError_, folder_service.FolderError) as exc:
        raise _translate(exc) from exc

    await audit.record(
        db,
        action=AuditAction.FOLDER_CREATED,
        target_type=AuditTargetType.FOLDER,
        target_id=folder.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={
            "label": folder.name,
            "name": folder.name,
            "parent": str(payload.parent_folder_id or ""),
        },
    )
    await db.commit()
    await db.refresh(folder)
    return FolderOut.model_validate(folder)


@router.patch("/{folder_id}", response_model=FolderOut)
async def update_folder(
    folder_id: uuid.UUID,
    payload: FolderUpdate,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> FolderOut:
    workspace, _ = access
    folder = await _load(db, workspace.id, folder_id)
    try:
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=WorkspaceRole.EDITOR,
            folder_id=folder.id,
        )
        if payload.move:
            # Editor on the destination too, or a move becomes a way to write into a
            # folder you have no rights to.
            await permissions.require_object_role(
                db,
                workspace_id=workspace.id,
                user=user,
                required=WorkspaceRole.EDITOR,
                folder_id=payload.parent_folder_id,
            )
        folder = await folder_service.rename_or_move(
            db,
            folder=folder,
            name=payload.name,
            new_parent_id=payload.parent_folder_id,
            move=payload.move,
        )
    except (permissions.PermissionError_, folder_service.FolderError) as exc:
        raise _translate(exc) from exc

    await audit.record(
        db,
        action=AuditAction.PERMISSION_CHANGED,
        target_type=AuditTargetType.FOLDER,
        target_id=folder.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={
            "label": folder.name,
            "renamed": payload.name is not None,
            "moved": payload.move,
        },
    )
    await db.commit()
    await db.refresh(folder)
    return FolderOut.model_validate(folder)


@router.delete("/{folder_id}", response_model=DeletionReceipt)
async def delete_folder(
    folder_id: uuid.UUID,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> DeletionReceipt:
    workspace, _ = access
    folder = await _load(db, workspace.id, folder_id)
    try:
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=WorkspaceRole.EDITOR,
            folder_id=folder.id,
        )
    except permissions.PermissionError_ as exc:
        raise _translate(exc) from exc

    batch_id, count, released = await folder_service.soft_delete(db, folder)
    await audit.record(
        db,
        action=AuditAction.FOLDER_DELETED,
        target_type=AuditTargetType.FOLDER,
        target_id=folder.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={
            "label": folder.name,
            "batch": str(batch_id),
            "folders": count,
            "bytes_released": released,
        },
    )
    await db.commit()
    return DeletionReceipt(
        deletion_batch_id=batch_id, items_deleted=count, bytes_released=released
    )


@router.post("/restore", status_code=status.HTTP_200_OK)
async def restore_folder(
    payload: RestoreRequest,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Revive exactly one deletion batch (Architecture.md §6a)."""
    workspace, role = access
    if not role.can_act_as(WorkspaceRole.EDITOR):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requires editor.")
    # Gone for good once the retention sweep purged the bytes (Architecture §6b).
    try:
        await trash.assert_restorable(db, payload.deletion_batch_id)
    except trash.GoneForGood as exc:
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc
    try:
        restored, reclaimed = await folder_service.restore_batch(
            db, workspace, payload.deletion_batch_id
        )
    except folder_service.FolderError as exc:
        raise _translate(exc) from exc
    except Exception as exc:  # quota refusal
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await audit.record(
        db,
        action=AuditAction.VERSION_RESTORED,
        target_type=AuditTargetType.FOLDER,
        target_id=None,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={
            "label": f"{restored} folder(s)",
            "batch": str(payload.deletion_batch_id),
            "restored": restored,
        },
    )
    await db.commit()
    return {"restored": restored, "bytes_reclaimed": reclaimed}


async def _indicator_facts(db, workspace_id, folder_id, user, files, folders) -> dict:
    """The permission indicator's data (Design.md §5): the viewer's role on each item and
    whether it is shared. A fixed number of grouped queries per listing, not per row."""
    file_ids = [f.id for f in files]
    folder_ids = [f.id for f in folders]
    file_roles, folder_roles, here = await permissions.listing_roles(
        db,
        workspace_id=workspace_id,
        user=user,
        folder_id=folder_id,
        file_ids=file_ids,
        child_folder_ids=folder_ids,
    )

    grants_per_file: dict = {}
    grants_per_folder: dict = {}
    links_per_file: dict = {}
    if file_ids:
        grants_per_file = dict(
            (
                await db.execute(
                    select(FilePermission.file_id, func.count())
                    .where(FilePermission.file_id.in_(file_ids))
                    .group_by(FilePermission.file_id)
                )
            ).all()
        )
        now = datetime.now(UTC)
        # "Active" means a visitor could redeem it right now — the same three conditions
        # resolution checks, so the mark cannot claim a link works when it would refuse.
        links_per_file = dict(
            (
                await db.execute(
                    select(ShareLink.file_id, func.count())
                    .where(
                        ShareLink.file_id.in_(file_ids),
                        ShareLink.revoked_at.is_(None),
                        or_(ShareLink.expires_at.is_(None), ShareLink.expires_at > now),
                        or_(
                            ShareLink.max_downloads.is_(None),
                            ShareLink.download_count < ShareLink.max_downloads,
                        ),
                    )
                    .group_by(ShareLink.file_id)
                )
            ).all()
        )
    if folder_ids:
        grants_per_folder = dict(
            (
                await db.execute(
                    select(FilePermission.folder_id, func.count())
                    .where(FilePermission.folder_id.in_(folder_ids))
                    .group_by(FilePermission.folder_id)
                )
            ).all()
        )

    return {
        "here": here,
        "files": {
            fid: {
                "my_role": file_roles.get(fid),
                "explicit_grants": grants_per_file.get(fid, 0),
                "active_share_links": links_per_file.get(fid, 0),
            }
            for fid in file_ids
        },
        "folders": {
            fid: {
                "my_role": folder_roles.get(fid),
                "explicit_grants": grants_per_folder.get(fid, 0),
            }
            for fid in folder_ids
        },
    }
