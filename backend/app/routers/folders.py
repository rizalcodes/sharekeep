"""Folder endpoints (Schema.md §5, Architecture.md §4.1, §4.6, §6a).

Every handler resolves access through `services/permissions.py`. No handler queries
membership or grants for itself.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession, ViewerAccess
from app.models.content import File, Folder
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
from app.services import audit, permissions
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
    return FolderListing(
        folder=FolderOut.model_validate(folder) if folder else None,
        breadcrumb=[BreadcrumbEntry(id=f.id, name=f.name) for f in trail],
        folders=[FolderOut.model_validate(f) for f in child_folders.scalars().all()],
        files=[FileOut.model_validate(f) for f in child_files.scalars().all()],
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
        metadata={"name": folder.name, "parent": str(payload.parent_folder_id or "")},
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
        metadata={"renamed": payload.name is not None, "moved": payload.move},
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
        metadata={"batch": str(batch_id), "folders": count, "bytes_released": released},
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
        metadata={"batch": str(payload.deletion_batch_id), "restored": restored},
    )
    await db.commit()
    return {"restored": restored, "bytes_reclaimed": reclaimed}
