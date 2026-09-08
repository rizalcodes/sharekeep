"""Database constraint tests.

Schema.md declares these constraints; this file proves the database actually enforces
them by colliding with each one. A constraint that is declared but not created looks
identical to a working one until the day it matters.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core import security
from app.models.enums import AuditAction, AuditTargetType, InvitationStatus, WorkspaceRole
from app.models.identity import (
    AuditLog,
    RefreshToken,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)


async def _user(db, email: str) -> User:
    user = User(
        email=email,
        hashed_password=security.hash_password("correct-horse-battery-staple"),
        full_name="Constraint Tester",
    )
    db.add(user)
    await db.flush()
    return user


async def _workspace(db, owner: User, *, personal: bool = False) -> Workspace:
    workspace = Workspace(name="WS", owner_id=owner.id, is_personal=personal)
    db.add(workspace)
    await db.flush()
    return workspace


async def test_user_email_is_unique(db):
    await _user(db, "unique@example.com")
    with pytest.raises(IntegrityError):
        await _user(db, "unique@example.com")
    await db.rollback()


async def test_a_user_holds_one_role_per_workspace(db):
    """Schema.md §3: UNIQUE (workspace_id, user_id).

    Without it a user could hold both viewer and owner rows, and role resolution would
    depend on which the query happened to return first.
    """
    user = await _user(db, "roles@example.com")
    workspace = await _workspace(db, user)
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))
    await db.flush()

    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.VIEWER))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_only_one_pending_invitation_per_email_per_workspace(db):
    """Schema.md §4 partial unique index."""
    owner = await _user(db, "inviter@example.com")
    workspace = await _workspace(db, owner)

    def _invitation(token: str) -> WorkspaceInvitation:
        return WorkspaceInvitation(
            workspace_id=workspace.id,
            email="invitee@example.com",
            role=WorkspaceRole.EDITOR,
            token_hash=security.hash_token(token),
            status=InvitationStatus.PENDING,
            invited_by=owner.id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )

    db.add(_invitation("token-one"))
    await db.flush()

    db.add(_invitation("token-two"))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_partial_index_allows_reinvite_after_revocation(db):
    """The index must be PARTIAL, not plain unique.

    A plain unique index would permanently block re-inviting anyone who had ever been
    invited, which is why the WHERE clause matters.
    """
    owner = await _user(db, "reinviter@example.com")
    workspace = await _workspace(db, owner)

    first = WorkspaceInvitation(
        workspace_id=workspace.id,
        email="again@example.com",
        role=WorkspaceRole.EDITOR,
        token_hash=security.hash_token("first-token"),
        status=InvitationStatus.REVOKED,
        invited_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(first)
    await db.flush()

    second = WorkspaceInvitation(
        workspace_id=workspace.id,
        email="again@example.com",
        role=WorkspaceRole.EDITOR,
        token_hash=security.hash_token("second-token"),
        status=InvitationStatus.PENDING,
        invited_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(second)
    await db.flush()  # must not raise

    result = await db.execute(
        select(WorkspaceInvitation).where(WorkspaceInvitation.email == "again@example.com")
    )
    assert len(result.scalars().all()) == 2


async def test_one_personal_workspace_per_user(db):
    """PRD §8: exactly one personal workspace, enforced in the database."""
    user = await _user(db, "personal@example.com")
    await _workspace(db, user, personal=True)

    db.add(Workspace(name="Second Personal", owner_id=user.id, is_personal=True))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_multiple_non_personal_workspaces_are_allowed(db):
    user = await _user(db, "many@example.com")
    await _workspace(db, user, personal=False)
    await _workspace(db, user, personal=False)  # must not raise


async def test_refresh_token_hash_is_unique(db):
    user = await _user(db, "tokens@example.com")
    shared = security.hash_token("same-raw-token")

    for _ in range(2):
        db.add(
            RefreshToken(
                user_id=user.id,
                token_hash=shared,
                expires_at=datetime.now(UTC) + timedelta(days=14),
            )
        )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_audit_log_survives_deletion_of_its_actor(db):
    """An audit trail that vanishes with the user it records is not an audit trail.

    actor_id is ON DELETE SET NULL, deliberately not CASCADE.
    """
    user = await _user(db, "vanishing@example.com")
    entry = AuditLog(
        actor_id=user.id,
        action=AuditAction.LOGIN,
        target_type=AuditTargetType.USER,
        target_id=user.id,
    )
    db.add(entry)
    await db.flush()
    entry_id = entry.id

    await db.delete(user)
    await db.flush()
    # The ORM defines no relationship for actor_id, so Postgres applies SET NULL but
    # the identity map still holds the old value. Expire to read the real row.
    db.expire_all()

    surviving = await db.get(AuditLog, entry_id)
    assert surviving is not None, "audit entry must outlive its actor"
    assert surviving.actor_id is None
    assert surviving.action is AuditAction.LOGIN


async def test_deleting_a_workspace_cascades_to_its_members(db):
    user = await _user(db, "cascade@example.com")
    workspace = await _workspace(db, user)
    member = WorkspaceMember(
        workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER
    )
    db.add(member)
    await db.flush()
    member_id = member.id

    await db.delete(workspace)
    await db.flush()

    assert await db.get(WorkspaceMember, member_id) is None


async def test_workspace_owner_cannot_be_deleted_while_owning(db):
    """ON DELETE RESTRICT: removing a user must not orphan a workspace row."""
    user = await _user(db, "restricted@example.com")
    await _workspace(db, user)

    await db.delete(user)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_invitation_token_hash_is_unique(db):
    owner = await _user(db, "toke@example.com")
    workspace = await _workspace(db, owner)
    shared = security.hash_token("collide")

    for email in ("one@example.com", "two@example.com"):
        db.add(
            WorkspaceInvitation(
                workspace_id=workspace.id,
                email=email,
                role=WorkspaceRole.VIEWER,
                token_hash=shared,
                status=InvitationStatus.PENDING,
                invited_by=owner.id,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_ids_are_uuid_not_sequential(db):
    """Architecture.md §9: no guessable identifiers anywhere."""
    user = await _user(db, "uuid@example.com")
    workspace = await _workspace(db, user)
    assert isinstance(user.id, uuid.UUID)
    assert isinstance(workspace.id, uuid.UUID)
    assert user.id.version == 4
