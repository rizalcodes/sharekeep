"""Workspace and membership endpoints (Architecture.md §4.6)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession, OwnerAccess, ViewerAccess
from app.models.enums import AuditAction, AuditTargetType, WorkspaceRole
from app.models.identity import User, Workspace, WorkspaceMember
from app.schemas.workspace import (
    MemberOut,
    MemberRoleUpdate,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from app.services import audit, auth, permissions

router = APIRouter(prefix=f"{settings.api_v1_prefix}/workspaces", tags=["workspaces"])


def _out(workspace: Workspace, role: WorkspaceRole) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        owner_id=workspace.owner_id,
        is_personal=workspace.is_personal,
        storage_quota_bytes=workspace.storage_quota_bytes,
        storage_used_bytes=workspace.storage_used_bytes,
        created_at=workspace.created_at,
        role=role,
    )


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(user: CurrentUser, db: DbSession) -> list[WorkspaceOut]:
    """Only workspaces the caller is a member of — never a global list."""
    rows = await auth.list_workspaces_for_user(db, user.id)
    return [_out(workspace, role) for workspace, role in rows]


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate, request: Request, user: CurrentUser, db: DbSession
) -> WorkspaceOut:
    workspace = Workspace(name=payload.name, owner_id=user.id, is_personal=False)
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER)
    )
    await audit.record(
        db,
        action=AuditAction.MEMBER_ADDED,
        target_type=AuditTargetType.WORKSPACE,
        target_id=workspace.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={"role": WorkspaceRole.OWNER.value, "reason": "workspace_created"},
    )
    await db.commit()
    await db.refresh(workspace)
    return _out(workspace, WorkspaceRole.OWNER)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(access: ViewerAccess) -> WorkspaceOut:
    workspace, role = access
    return _out(workspace, role)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    payload: WorkspaceUpdate,
    request: Request,
    access: OwnerAccess,
    user: CurrentUser,
    db: DbSession,
) -> WorkspaceOut:
    workspace, role = access
    changed: dict[str, object] = {}
    if payload.name is not None and payload.name != workspace.name:
        changed["name"] = payload.name
        workspace.name = payload.name
    if (
        payload.storage_quota_bytes is not None
        and payload.storage_quota_bytes != workspace.storage_quota_bytes
    ):
        changed["storage_quota_bytes"] = payload.storage_quota_bytes
        workspace.storage_quota_bytes = payload.storage_quota_bytes

    if changed:
        await audit.record(
            db,
            action=AuditAction.PERMISSION_CHANGED,
            target_type=AuditTargetType.WORKSPACE,
            target_id=workspace.id,
            workspace_id=workspace.id,
            actor_id=user.id,
            request=request,
            metadata={"changed": changed},
        )
    await db.commit()
    await db.refresh(workspace)
    return _out(workspace, role)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    request: Request, access: OwnerAccess, user: CurrentUser, db: DbSession
) -> None:
    workspace, _ = access
    if workspace.is_personal:
        # Schema.md §2 — a personal workspace exists for as long as its user does.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A personal workspace cannot be deleted.",
        )
    await audit.record(
        db,
        action=AuditAction.DELETE,
        target_type=AuditTargetType.WORKSPACE,
        target_id=workspace.id,
        workspace_id=None,  # the workspace row is about to disappear
        actor_id=user.id,
        request=request,
        metadata={"name": workspace.name},
    )
    await db.delete(workspace)
    await db.commit()


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(access: ViewerAccess, db: DbSession) -> list[MemberOut]:
    """Any member may see who else is in the workspace."""
    workspace, _ = access
    result = await db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace.id)
        .order_by(WorkspaceMember.created_at)
    )
    return [
        MemberOut(
            id=member.id,
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=member.role,
            created_at=member.created_at,
        )
        for member, user in result.all()
    ]


async def _load_member(
    db: DbSession, workspace: Workspace, member_id: uuid.UUID
) -> WorkspaceMember:
    member = await db.get(WorkspaceMember, member_id)
    # Check the member actually belongs to THIS workspace: without it, an owner of
    # workspace A could pass a member id from workspace B and modify it.
    if member is None or member.workspace_id != workspace.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
    return member


@router.patch("/{workspace_id}/members/{member_id}", response_model=MemberOut)
async def change_role(
    member_id: uuid.UUID,
    payload: MemberRoleUpdate,
    request: Request,
    access: OwnerAccess,
    user: CurrentUser,
    db: DbSession,
) -> MemberOut:
    workspace, _ = access
    member = await _load_member(db, workspace, member_id)
    previous = member.role

    try:
        await permissions.change_member_role(db, workspace, member, payload.role)
    except permissions.LastOwnerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    if previous is not member.role:
        await audit.record(
            db,
            action=AuditAction.MEMBER_ROLE_CHANGED,
            target_type=AuditTargetType.WORKSPACE_MEMBER,
            target_id=member.id,
            workspace_id=workspace.id,
            actor_id=user.id,
            request=request,
            metadata={
                "from": previous.value,
                "to": member.role.value,
                "user_id": str(member.user_id),
            },
        )
    await db.commit()

    target = await db.get(User, member.user_id)
    return MemberOut(
        id=member.id,
        user_id=member.user_id,
        email=target.email,
        full_name=target.full_name,
        role=member.role,
        created_at=member.created_at,
    )


@router.delete("/{workspace_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    member_id: uuid.UUID,
    request: Request,
    access: OwnerAccess,
    user: CurrentUser,
    db: DbSession,
) -> None:
    workspace, _ = access
    member = await _load_member(db, workspace, member_id)
    removed_user_id = member.user_id
    removed_role = member.role

    try:
        await permissions.remove_member(db, workspace, member)
    except permissions.LastOwnerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await audit.record(
        db,
        action=AuditAction.MEMBER_REMOVED,
        target_type=AuditTargetType.WORKSPACE_MEMBER,
        target_id=member_id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={"user_id": str(removed_user_id), "role": removed_role.value},
    )
    await db.commit()
