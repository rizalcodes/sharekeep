"""Invitation endpoints (Architecture.md §4.6).

The preview and accept endpoints are token-addressed rather than workspace-addressed,
because the invitee is by definition not yet a member and cannot pass the workspace
membership check.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession, OwnerAccess
from app.models.enums import AuditAction, AuditTargetType
from app.models.identity import WorkspaceInvitation
from app.schemas.workspace import (
    InvitationCreate,
    InvitationCreated,
    InvitationOut,
    InvitationPreview,
    InvitationTokenRequest,
)
from app.services import audit, invitations

router = APIRouter(prefix=settings.api_v1_prefix, tags=["invitations"])


@router.post(
    "/workspaces/{workspace_id}/invitations",
    response_model=InvitationCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    payload: InvitationCreate,
    request: Request,
    access: OwnerAccess,
    user: CurrentUser,
    db: DbSession,
) -> InvitationCreated:
    workspace, _ = access
    try:
        invitation, raw_token = await invitations.create(
            db,
            workspace=workspace,
            email=payload.email,
            role=payload.role,
            invited_by=user,
        )
    except invitations.InvitationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await audit.record(
        db,
        action=AuditAction.INVITATION_SENT,
        target_type=AuditTargetType.WORKSPACE_INVITATION,
        target_id=invitation.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        # The email is recorded; the token never is — an audit log that stores the
        # token would hand out workspace access to anyone who can read the log.
        metadata={"email": invitation.email, "role": invitation.role.value},
    )
    await db.commit()
    await db.refresh(invitation)

    # Returned exactly once. Until Resend arrives at M6 this is how the link reaches
    # the invitee.
    return InvitationCreated(
        invitation=InvitationOut.model_validate(invitation), token=raw_token
    )


@router.get("/workspaces/{workspace_id}/invitations", response_model=list[InvitationOut])
async def list_invitations(access: OwnerAccess, db: DbSession) -> list[InvitationOut]:
    workspace, _ = access
    result = await db.execute(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.workspace_id == workspace.id)
        .order_by(WorkspaceInvitation.created_at.desc())
    )
    return [InvitationOut.model_validate(row) for row in result.scalars().all()]


@router.delete(
    "/workspaces/{workspace_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    access: OwnerAccess,
    user: CurrentUser,
    db: DbSession,
) -> None:
    workspace, _ = access
    invitation = await db.get(WorkspaceInvitation, invitation_id)
    # Scope check: an owner of workspace A must not be able to revoke B's invitation.
    if invitation is None or invitation.workspace_id != workspace.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found.")

    try:
        await invitations.revoke(db, invitation)
    except invitations.InvitationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await audit.record(
        db,
        action=AuditAction.INVITATION_REVOKED,
        target_type=AuditTargetType.WORKSPACE_INVITATION,
        target_id=invitation.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={"email": invitation.email},
    )
    await db.commit()


@router.post("/invitations/preview", response_model=InvitationPreview)
async def preview_invitation(
    payload: InvitationTokenRequest, db: DbSession
) -> InvitationPreview:
    """Unauthenticated preview, so the acceptance page can render before sign-in.

    POST with the token in the body, not GET with it in the path, even though this is
    a read. A path token is written verbatim into every access log by the web server,
    and an invitation token in a log is workspace access for anyone who can read that
    log. Found by inspecting real container logs during M1 manual validation.

    Reveals only the workspace name, the invited address, and whether signup is
    needed — never the member list or any workspace contents.
    """
    invitation = await invitations.get_by_token(db, payload.token)
    if invitation is None or not invitations.is_usable(invitation):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invitation is no longer valid.")

    return InvitationPreview(
        workspace_name=await invitations.workspace_name_for(db, invitation),
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        requires_signup=not await invitations.user_exists(db, invitation.email),
    )


@router.post("/invitations/accept", response_model=InvitationOut)
async def accept_invitation(
    payload: InvitationTokenRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> InvitationOut:
    """Accept as an already-signed-in user.

    A user whose email differs from the invited address is rejected — an invitation is
    a grant to one address, not a bearer token for whoever opens the link. Users who
    have no account yet go through /auth/signup with the token instead.
    """
    invitation = await invitations.get_by_token(db, payload.token)
    if invitation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invitation is no longer valid.")

    try:
        await invitations.accept(db, invitation=invitation, user=user)
    except invitations.InvitationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    await audit.record(
        db,
        action=AuditAction.INVITATION_ACCEPTED,
        target_type=AuditTargetType.WORKSPACE_INVITATION,
        target_id=invitation.id,
        workspace_id=invitation.workspace_id,
        actor_id=user.id,
        request=request,
        metadata={"role": invitation.role.value, "via": "signed_in"},
    )
    await db.commit()
    await db.refresh(invitation)
    return InvitationOut.model_validate(invitation)
