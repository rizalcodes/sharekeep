"""File endpoints: upload, download, versions, move, delete, restore, grants.

Architecture.md §2 (chunked upload), §3 (what may be served), §4.1 (who may see it),
§6 (quota), §6a (deletion and restore).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession, ViewerAccess
from app.models.content import File, FilePermission, FileVersion
from app.models.enums import AuditAction, AuditTargetType, WorkspaceRole
from app.models.identity import User
from app.schemas.content import (
    DeletionReceipt,
    DownloadResponse,
    FileOut,
    FileUpdate,
    GrantCreate,
    GrantOut,
    InFlightUpload,
    QuotaOut,
    RestoreRequest,
    ResumeState,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResponse,
    UploadPart,
    VersionOut,
)
from app.services import audit, permissions, quota, storage
from app.services import files as file_service

router = APIRouter(
    prefix=f"{settings.api_v1_prefix}/workspaces/{{workspace_id}}",
    tags=["files"],
)


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, permissions.NotAMember):
        return HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    if isinstance(exc, permissions.InsufficientRole):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, quota.QuotaExceeded):
        return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc))
    if isinstance(exc, quota.FileTooLarge):
        return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc))
    if isinstance(exc, file_service.NameConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, file_service.UploadStateError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, storage.StorageError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "Object storage is unavailable.")
    if isinstance(exc, file_service.FileError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


async def _load(db, workspace_id: uuid.UUID, file_id: uuid.UUID) -> File:
    file = await file_service.get_file(db, workspace_id, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")
    return file


async def _require(db, workspace, user, file: File, role: WorkspaceRole) -> None:
    try:
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=role,
            file_id=file.id,
            folder_id=file.folder_id,
        )
    except permissions.PermissionError_ as exc:
        raise _translate(exc) from exc


# --- Quota -----------------------------------------------------------------------

@router.get("/quota", response_model=QuotaOut)
async def get_quota(access: ViewerAccess) -> QuotaOut:
    workspace, _ = access
    return QuotaOut(
        storage_quota_bytes=workspace.storage_quota_bytes,
        storage_used_bytes=workspace.storage_used_bytes,
        storage_available_bytes=max(
            0, workspace.storage_quota_bytes - workspace.storage_used_bytes
        ),
        max_file_size_bytes=settings.max_file_size_bytes,
    )


# --- Upload ----------------------------------------------------------------------

@router.post("/uploads/init", response_model=UploadInitResponse, status_code=201)
async def upload_init(
    payload: UploadInitRequest,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> UploadInitResponse:
    """Architecture.md §2 steps 1-3.

    Quota and the provider's per-file ceiling are both checked here, before any
    storage call, so an oversized upload fails cleanly instead of part-way through.
    """
    workspace, _ = access
    try:
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=WorkspaceRole.EDITOR,
            folder_id=payload.folder_id,
        )
        existing = await db.execute(
            select(File.id).where(
                File.workspace_id == workspace.id,
                File.folder_id.is_(payload.folder_id)
                if payload.folder_id is None
                else File.folder_id == payload.folder_id,
                File.name == payload.name,
                File.deleted_at.is_(None),
            )
        )
        is_new = existing.scalar_one_or_none() is None

        file, version, parts = await file_service.start_upload(
            db,
            workspace=workspace,
            folder_id=payload.folder_id,
            name=payload.name,
            mime_type=payload.mime_type,
            size_bytes=payload.size_bytes,
            user=user,
        )
    except Exception as exc:
        raise _translate(exc) from exc

    await db.commit()
    return UploadInitResponse(
        file_id=file.id,
        version_id=version.id,
        version_number=version.version_number,
        parts=[UploadPart(**p) for p in parts],
        is_new_file=is_new,
    )


@router.post("/uploads/complete", response_model=FileOut)
async def upload_complete(
    payload: UploadCompleteRequest,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> FileOut:
    """Architecture.md §2 steps 5-6, then version pruning."""
    workspace, _ = access
    version = await db.get(FileVersion, payload.version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload session not found.")
    file = await _load(db, workspace.id, version.file_id)
    await _require(db, workspace, user, file, WorkspaceRole.EDITOR)

    try:
        file, pruned = await file_service.complete_upload(
            db,
            workspace=workspace,
            file=file,
            version=version,
            parts=[p.model_dump() for p in payload.parts],
        )
    except Exception as exc:
        raise _translate(exc) from exc

    await audit.record(
        db,
        action=AuditAction.UPLOAD,
        target_type=AuditTargetType.FILE,
        target_id=file.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={
            "version": version.version_number,
            "size_bytes": version.size_bytes,
            "pruned_versions": len(pruned),
        },
    )
    await db.commit()

    # Objects for pruned versions are removed only after the transaction succeeds;
    # deleting them first would destroy data a rollback was going to keep.
    for old in pruned:
        storage.delete_object(old.storage_key)

    await db.refresh(file)
    return FileOut.model_validate(file)


# --- Read ------------------------------------------------------------------------

@router.get("/files/{file_id}", response_model=FileOut)
async def get_file(
    file_id: uuid.UUID, access: ViewerAccess, user: CurrentUser, db: DbSession
) -> FileOut:
    workspace, _ = access
    file = await _load(db, workspace.id, file_id)
    await _require(db, workspace, user, file, WorkspaceRole.VIEWER)
    return FileOut.model_validate(file)


@router.get("/files/{file_id}/download", response_model=DownloadResponse)
async def download_file(
    file_id: uuid.UUID,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> DownloadResponse:
    """Returns a short-lived signed URL — the API never proxies the bytes."""
    workspace, _ = access
    file = await _load(db, workspace.id, file_id)
    await _require(db, workspace, user, file, WorkspaceRole.VIEWER)
    try:
        url = await file_service.download_url(db, file)
    except file_service.FileError as exc:
        # A file blocked by scan status is a 403, not a 404: it exists, and the caller
        # is allowed to know that it is being withheld.
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except storage.StorageError as exc:
        raise _translate(exc) from exc

    await audit.record(
        db,
        action=AuditAction.DOWNLOAD,
        target_type=AuditTargetType.FILE,
        target_id=file.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
    )
    await db.commit()
    return DownloadResponse(
        url=url,
        expires_in_seconds=settings.signed_download_url_ttl_seconds,
        scan_status=file.scan_status,
    )


@router.get("/files/{file_id}/versions", response_model=list[VersionOut])
async def list_versions(
    file_id: uuid.UUID, access: ViewerAccess, user: CurrentUser, db: DbSession
) -> list[VersionOut]:
    workspace, _ = access
    file = await _load(db, workspace.id, file_id)
    await _require(db, workspace, user, file, WorkspaceRole.VIEWER)
    result = await db.execute(
        select(FileVersion)
        .where(
            FileVersion.file_id == file.id,
            FileVersion.deleted_at.is_(None),
            FileVersion.upload_id.is_(None),
        )
        .order_by(FileVersion.version_number.desc())
    )
    return [
        VersionOut(
            **{
                **VersionOut.model_validate(v).model_dump(),
                "is_current": v.id == file.current_version_id,
            }
        )
        for v in result.scalars().all()
    ]


# --- Mutate ----------------------------------------------------------------------

@router.patch("/files/{file_id}", response_model=FileOut)
async def update_file(
    file_id: uuid.UUID,
    payload: FileUpdate,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> FileOut:
    workspace, _ = access
    file = await _load(db, workspace.id, file_id)
    await _require(db, workspace, user, file, WorkspaceRole.EDITOR)

    if payload.move:
        try:
            # Editor on the destination as well, or a move writes into a folder the
            # caller has no rights to.
            await permissions.require_object_role(
                db,
                workspace_id=workspace.id,
                user=user,
                required=WorkspaceRole.EDITOR,
                folder_id=payload.folder_id,
            )
        except permissions.PermissionError_ as exc:
            raise _translate(exc) from exc

    try:
        file = await file_service.rename_or_move(
            db,
            file=file,
            name=payload.name,
            new_folder_id=payload.folder_id,
            move=payload.move,
        )
        if payload.tags is not None:
            file.tags = payload.tags
            await db.flush()
    except file_service.FileError as exc:
        raise _translate(exc) from exc

    await db.commit()
    await db.refresh(file)
    return FileOut.model_validate(file)


@router.delete("/files/{file_id}", response_model=DeletionReceipt)
async def delete_file(
    file_id: uuid.UUID,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> DeletionReceipt:
    workspace, _ = access
    file = await _load(db, workspace.id, file_id)
    await _require(db, workspace, user, file, WorkspaceRole.EDITOR)

    size = file.size_bytes
    batch_id = await file_service.soft_delete(db, workspace=workspace, file=file)
    await audit.record(
        db,
        action=AuditAction.DELETE,
        target_type=AuditTargetType.FILE,
        target_id=file.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={"batch": str(batch_id), "name": file.name},
    )
    await db.commit()
    return DeletionReceipt(
        deletion_batch_id=batch_id, items_deleted=1, bytes_released=size
    )


@router.post("/files/restore")
async def restore_file(
    payload: RestoreRequest,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    workspace, role = access
    if not role.can_act_as(WorkspaceRole.EDITOR):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requires editor.")
    try:
        restored = await file_service.restore(
            db, workspace=workspace, batch_id=payload.deletion_batch_id
        )
    except Exception as exc:
        raise _translate(exc) from exc

    await audit.record(
        db,
        action=AuditAction.VERSION_RESTORED,
        target_type=AuditTargetType.FILE,
        target_id=None,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={"batch": str(payload.deletion_batch_id), "restored": restored},
    )
    await db.commit()
    return {"restored": restored}


# --- Per-object grants (Schema.md §8) --------------------------------------------

@router.post("/files/{file_id}/grants", response_model=GrantOut, status_code=201)
async def grant_on_file(
    file_id: uuid.UUID,
    payload: GrantCreate,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> GrantOut:
    """Grant one user an explicit role on one file.

    Requires owner: handing out access is an administrative act, not an edit.
    """
    workspace, _ = access
    file = await _load(db, workspace.id, file_id)
    await _require(db, workspace, user, file, WorkspaceRole.OWNER)

    target = await db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    existing = await db.execute(
        select(FilePermission).where(
            FilePermission.file_id == file.id, FilePermission.user_id == payload.user_id
        )
    )
    grant = existing.scalar_one_or_none()
    if grant is None:
        grant = FilePermission(
            file_id=file.id,
            user_id=payload.user_id,
            role=payload.role,
            granted_by=user.id,
        )
        db.add(grant)
    else:
        grant.role = payload.role
        grant.granted_by = user.id
    await db.flush()

    await audit.record(
        db,
        action=AuditAction.PERMISSION_CHANGED,
        target_type=AuditTargetType.FILE,
        target_id=file.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={"user_id": str(payload.user_id), "role": payload.role.value},
    )
    await db.commit()
    await db.refresh(grant)
    return GrantOut(
        **GrantOut.model_validate(grant).model_dump(exclude={"email"}),
        email=target.email,
    )


@router.delete("/files/{file_id}/grants/{user_id}", status_code=204)
async def revoke_file_grant(
    file_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> None:
    workspace, _ = access
    file = await _load(db, workspace.id, file_id)
    await _require(db, workspace, user, file, WorkspaceRole.OWNER)

    result = await db.execute(
        select(FilePermission).where(
            FilePermission.file_id == file.id, FilePermission.user_id == user_id
        )
    )
    grant = result.scalar_one_or_none()
    if grant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grant not found.")
    await db.delete(grant)
    await audit.record(
        db,
        action=AuditAction.PERMISSION_CHANGED,
        target_type=AuditTargetType.FILE,
        target_id=file.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={"user_id": str(user_id), "role": None, "revoked": True},
    )
    await db.commit()


@router.post("/folders/{folder_id}/grants", response_model=GrantOut, status_code=201)
async def grant_on_folder(
    folder_id: uuid.UUID,
    payload: GrantCreate,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> GrantOut:
    """Grant on a folder. Inherited by everything beneath it until a nearer grant
    overrides — including a nearer grant that is LESS permissive (Architecture §4.1)."""
    from app.services import folders as folder_service

    workspace, _ = access
    folder = await folder_service.get_folder(db, workspace.id, folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found.")
    try:
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=WorkspaceRole.OWNER,
            folder_id=folder.id,
        )
    except permissions.PermissionError_ as exc:
        raise _translate(exc) from exc

    target = await db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    existing = await db.execute(
        select(FilePermission).where(
            FilePermission.folder_id == folder.id,
            FilePermission.user_id == payload.user_id,
        )
    )
    grant = existing.scalar_one_or_none()
    if grant is None:
        grant = FilePermission(
            folder_id=folder.id,
            user_id=payload.user_id,
            role=payload.role,
            granted_by=user.id,
        )
        db.add(grant)
    else:
        grant.role = payload.role
        grant.granted_by = user.id
    await db.flush()

    await audit.record(
        db,
        action=AuditAction.PERMISSION_CHANGED,
        target_type=AuditTargetType.FOLDER,
        target_id=folder.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={"user_id": str(payload.user_id), "role": payload.role.value},
    )
    await db.commit()
    await db.refresh(grant)
    return GrantOut(
        **GrantOut.model_validate(grant).model_dump(exclude={"email"}),
        email=target.email,
    )


# --- Resume (Architecture.md §2a) -------------------------------------------------

@router.get("/uploads", response_model=list[InFlightUpload])
async def list_in_flight_uploads(
    access: ViewerAccess, user: CurrentUser, db: DbSession
) -> list[InFlightUpload]:
    """Uploads left unfinished — after a refresh, a closed tab, or a lost connection.

    Scoped to the workspace like everything else: an interrupted upload belonging to a
    workspace the caller is not a member of is not listed, and the workspace itself
    reads as not found.
    """
    workspace, _ = access
    rows = await file_service.list_in_flight(db, workspace.id)

    out: list[InFlightUpload] = []
    for version, file, uploaded, parts_done in rows:
        # Only sessions the caller may actually write to.
        role = await permissions.resolve_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            file_id=file.id,
            folder_id=file.folder_id,
        )
        if role is None or not role.can_act_as(WorkspaceRole.EDITOR):
            continue
        out.append(
            InFlightUpload(
                version_id=version.id,
                file_id=file.id,
                file_name=file.name,
                folder_id=file.folder_id,
                total_bytes=version.size_bytes,
                uploaded_bytes=uploaded,
                parts_completed=parts_done,
                parts_total=len(storage.plan_parts(version.size_bytes)),
                started_at=version.upload_started_at,
                version_number=version.version_number,
            )
        )
    return out


@router.get("/uploads/{version_id}/parts", response_model=ResumeState)
async def get_resume_state(
    version_id: uuid.UUID, access: ViewerAccess, user: CurrentUser, db: DbSession
) -> ResumeState:
    """Which parts already landed, and fresh URLs for the ones that did not."""
    workspace, _ = access
    version = await db.get(FileVersion, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload session not found.")
    file = await _load(db, workspace.id, version.file_id)
    await _require(db, workspace, user, file, WorkspaceRole.EDITOR)

    try:
        state = await file_service.resume_state(
            db, workspace=workspace, version=version, file=file
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return ResumeState(**state)


@router.delete("/uploads/{version_id}", status_code=204)
async def cancel_upload(
    version_id: uuid.UUID, access: ViewerAccess, user: CurrentUser, db: DbSession
) -> None:
    """Abandon an interrupted upload and return its reserved quota immediately."""
    workspace, _ = access
    version = await db.get(FileVersion, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload session not found.")
    file = await _load(db, workspace.id, version.file_id)
    await _require(db, workspace, user, file, WorkspaceRole.EDITOR)

    await file_service.abort_upload(db, workspace=workspace, version=version)
    await db.commit()
