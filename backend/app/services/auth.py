"""Authentication: signup, login, refresh rotation, logout (Architecture.md §4.0)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import settings
from app.models.enums import WorkspaceRole
from app.models.identity import RefreshToken, User, Workspace, WorkspaceMember
from app.services import audit


class AuthError(Exception):
    """Authentication failed. The message is safe to show a client."""


class RefreshTokenReuse(AuthError):
    """A refresh token that had already been rotated was presented again.

    Treated as theft: the token was captured and replayed, so the whole chain for that
    user is revoked rather than just the presented row.
    """


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def create_user(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str,
    email_verified: bool = False,
) -> User:
    user = User(
        email=email.lower(),
        hashed_password=security.hash_password(password),
        full_name=full_name,
        email_verified_at=datetime.now(UTC) if email_verified else None,
    )
    db.add(user)
    await db.flush()
    return user


async def create_personal_workspace(db: AsyncSession, user: User) -> Workspace:
    """Every user gets exactly one personal workspace on signup (PRD §8).

    Created in the caller's transaction, so a failure here cannot leave a user with no
    workspace at all.
    """
    workspace = Workspace(
        name=f"{user.full_name}'s Workspace",
        owner_id=user.id,
        is_personal=True,
    )
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
    )
    await db.flush()
    return workspace


async def authenticate(db: AsyncSession, *, email: str, password: str) -> User | None:
    """Verify credentials without revealing whether the email exists.

    The password is verified against a dummy hash when no user matched, so the timing
    of a failed login does not distinguish "no such account" from "wrong password".
    """
    user = await get_user_by_email(db, email)
    valid = security.verify_password(password, user.hashed_password if user else None)
    if not valid or user is None:
        return None

    # Transparently upgrade the stored hash if Argon2's parameters have moved on.
    if security.needs_rehash(user.hashed_password):
        user.hashed_password = security.hash_password(password)
        await db.flush()
    return user


async def issue_refresh_token(
    db: AsyncSession,
    user: User,
    *,
    request: Request | None = None,
) -> str:
    raw, token_hash = security.generate_opaque_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
            user_agent=audit.user_agent(request),
            ip_address=audit.client_ip(request),
        )
    )
    await db.flush()
    return raw


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every live refresh token for a user. Used on reuse detection."""
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return result.rowcount or 0


async def rotate_refresh_token(
    db: AsyncSession,
    raw_token: str,
    *,
    request: Request | None = None,
) -> tuple[User, str]:
    """Exchange a refresh token for a new one, revoking the presented token.

    Returns (user, new_raw_token). Raises AuthError if the token is unusable, or
    RefreshTokenReuse if it had already been rotated.
    """
    token_hash = security.hash_token(raw_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    row = result.scalar_one_or_none()
    if row is None:
        raise AuthError("Invalid refresh token.")

    # Reuse detection comes first: a rotated token being presented again is the signal
    # that matters, and it must not be masked by the expiry or revocation checks.
    if row.rotated_to_id is not None:
        await revoke_all_for_user(db, row.user_id)
        raise RefreshTokenReuse("Refresh token reuse detected. All sessions revoked.")

    if row.revoked_at is not None:
        raise AuthError("Refresh token has been revoked.")
    # expires_at is authoritative, never a status flag.
    if row.expires_at <= datetime.now(UTC):
        raise AuthError("Refresh token has expired.")

    user = await db.get(User, row.user_id)
    if user is None:
        raise AuthError("Invalid refresh token.")

    raw, new_hash = security.generate_opaque_token()
    replacement = RefreshToken(
        user_id=user.id,
        token_hash=new_hash,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        user_agent=audit.user_agent(request),
        ip_address=audit.client_ip(request),
    )
    db.add(replacement)
    await db.flush()

    row.revoked_at = datetime.now(UTC)
    row.rotated_to_id = replacement.id
    await db.flush()
    return user, raw


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> bool:
    """Revoke a single refresh token. Returns whether anything was revoked."""
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == security.hash_token(raw_token))
    )
    row = result.scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(UTC)
    await db.flush()
    return True


async def list_workspaces_for_user(
    db: AsyncSession, user_id: uuid.UUID
) -> list[tuple[Workspace, WorkspaceRole]]:
    result = await db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.is_personal.desc(), Workspace.created_at)
    )
    return [(workspace, role) for workspace, role in result.all()]
