"""Workspace invitations (Schema.md §4, Architecture.md §4.6).

Two rules carry the security weight here:

1. `expires_at` is authoritative, never the `status` column. An expired invitation the
   housekeeping sweep has not yet reached must still be refused.
2. An invitation grants access to **one email address**, not to whoever holds the link.
   A signed-in user whose address differs is rejected, never silently accepted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.models.enums import InvitationStatus, WorkspaceRole
from app.models.identity import User, Workspace, WorkspaceInvitation, WorkspaceMember
from app.services import permissions

INVITATION_TTL_DAYS = 7


class InvitationError(Exception):
    """Invitation could not be created or accepted. Message is client-safe."""


def _now() -> datetime:
    return datetime.now(UTC)


def is_usable(invitation: WorkspaceInvitation) -> bool:
    """The single validity test. `expires_at` beats `status` (Schema.md §4)."""
    return invitation.status is InvitationStatus.PENDING and invitation.expires_at > _now()


async def get_by_token(db: AsyncSession, raw_token: str) -> WorkspaceInvitation | None:
    result = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.token_hash == security.hash_token(raw_token)
        )
    )
    return result.scalar_one_or_none()


async def create(
    db: AsyncSession,
    *,
    workspace: Workspace,
    email: str,
    role: WorkspaceRole,
    invited_by: User,
) -> tuple[WorkspaceInvitation, str]:
    """Create an invitation, returning it with the raw token (shown exactly once)."""
    email = email.lower().strip()

    if role is WorkspaceRole.OWNER:
        raise InvitationError("Invite as editor or viewer, then transfer ownership explicitly.")

    existing_user = await db.execute(select(User).where(User.email == email))
    user = existing_user.scalar_one_or_none()
    if user is not None:
        membership = await permissions.get_membership(db, workspace.id, user.id)
        if membership is not None:
            raise InvitationError("That person is already a member of this workspace.")

    # Supersede any live invitation for the same address rather than colliding with
    # the partial unique index — re-inviting someone is a normal thing to want to do.
    result = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == workspace.id,
            WorkspaceInvitation.email == email,
            WorkspaceInvitation.status == InvitationStatus.PENDING,
        )
    )
    for previous in result.scalars().all():
        previous.status = InvitationStatus.REVOKED
    await db.flush()

    raw, token_hash = security.generate_opaque_token()
    invitation = WorkspaceInvitation(
        workspace_id=workspace.id,
        email=email,
        role=role,
        token_hash=token_hash,
        status=InvitationStatus.PENDING,
        invited_by=invited_by.id,
        expires_at=_now() + timedelta(days=INVITATION_TTL_DAYS),
    )
    db.add(invitation)
    await db.flush()
    return invitation, raw


async def revoke(db: AsyncSession, invitation: WorkspaceInvitation) -> None:
    if invitation.status is not InvitationStatus.PENDING:
        raise InvitationError("Only a pending invitation can be revoked.")
    invitation.status = InvitationStatus.REVOKED
    await db.flush()


async def accept(
    db: AsyncSession,
    *,
    invitation: WorkspaceInvitation,
    user: User,
) -> WorkspaceMember:
    """Accept an invitation on behalf of an authenticated user.

    The user's email must match the invited address. Anything else would turn an
    invitation into a bearer token: forward the link to a colleague and they gain
    access that was never granted to them.
    """
    if not is_usable(invitation):
        raise InvitationError("This invitation is no longer valid.")

    if user.email.lower() != invitation.email.lower():
        raise InvitationError("This invitation was issued to a different email address.")

    existing = await permissions.get_membership(db, invitation.workspace_id, user.id)
    if existing is not None:
        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_by = user.id
        await db.flush()
        return existing

    member = WorkspaceMember(
        workspace_id=invitation.workspace_id,
        user_id=user.id,
        role=invitation.role,
    )
    db.add(member)

    invitation.status = InvitationStatus.ACCEPTED
    invitation.accepted_by = user.id

    # Receiving the token at that address proves control of it, so acceptance is
    # itself a verification event (Schema.md §1, v1.2).
    if user.email_verified_at is None:
        user.email_verified_at = _now()

    await db.flush()
    return member


async def expire_stale(db: AsyncSession) -> int:
    """Housekeeping only. Validity never depends on this having run."""
    result = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.status == InvitationStatus.PENDING,
            WorkspaceInvitation.expires_at <= _now(),
        )
    )
    rows = result.scalars().all()
    for row in rows:
        row.status = InvitationStatus.EXPIRED
    await db.flush()
    return len(rows)


async def workspace_name_for(db: AsyncSession, invitation: WorkspaceInvitation) -> str:
    workspace = await db.get(Workspace, invitation.workspace_id)
    return workspace.name if workspace else "Unknown workspace"


async def user_exists(db: AsyncSession, email: str) -> bool:
    result = await db.execute(select(User.id).where(User.email == email.lower()))
    return result.scalar_one_or_none() is not None


def assert_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)
