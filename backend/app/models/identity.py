"""Identity, workspace and audit entities (Schema.md §1-4, §10, §12).

Constraints declared here are the ones Schema.md specifies. They live in the database
rather than only in service code because an application-layer check is a convention,
while a constraint is a guarantee.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import DEFAULT_STORAGE_QUOTA_BYTES
from app.models.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AuditAction, AuditTargetType, InvitationStatus, WorkspaceRole


def _pg_enum(enum_cls: type, name: str) -> SAEnum:
    """Postgres enum storing the *values*, not the Python member names."""
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Added in Schema v1.2. No verification flow until M6; the column exists so M6
    # needs no migration. Accepting an invitation implicitly proves control of the
    # address, so invited users can be marked verified at acceptance time.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    memberships: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Denormalised pointer to the current primary owner (Schema.md §2, v1.2).
    # NEVER read to decide access — workspace_members is authoritative. Kept in sync
    # on ownership transfer purely so listing a workspace does not need a join.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    is_personal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    storage_quota_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(str(DEFAULT_STORAGE_QUOTA_BYTES))
    )
    storage_used_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    invitations: Mapped[list[WorkspaceInvitation]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # One personal workspace per user, enforced by the database rather than by
        # remembering to check at every signup path.
        Index(
            "uq_workspaces_one_personal_per_owner",
            "owner_id",
            unique=True,
            postgresql_where=text("is_personal"),
        ),
    )


class WorkspaceMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        _pg_enum(WorkspaceRole, "workspace_role"), nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id",
            name="uq_workspace_members_workspace_id_user_id",
        ),
        Index("ix_workspace_members_user_id", "user_id"),
    )


class WorkspaceInvitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspace_invitations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[WorkspaceRole] = mapped_column(
        _pg_enum(WorkspaceRole, "workspace_role"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[InvitationStatus] = mapped_column(
        _pg_enum(InvitationStatus, "invitation_status"),
        nullable=False,
        server_default=InvitationStatus.PENDING.value,
    )
    invited_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="invitations")

    __table_args__ = (
        # Schema.md §4: one live invitation per email per workspace. Partial, so a
        # revoked or accepted invitation does not block re-inviting the same person.
        Index(
            "uq_workspace_invitations_pending_email",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )


class RefreshToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Schema.md §12. Stored hashed — a database leak must not yield usable sessions."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when this token is exchanged. Presenting a token that already has a
    # replacement means it was captured and replayed — see auth service.
    rotated_to_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    __table_args__ = (Index("ix_refresh_tokens_user_id_revoked_at", "user_id", "revoked_at"),)


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Schema.md §10. Append-only: the application has no update or delete path."""

    __tablename__ = "audit_logs"

    # Nullable because M1 records account-level events (login, login_failed) that are
    # not scoped to any workspace. Workspace-scoped actions always set it.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True
    )
    # NULL for anonymous share-link access, and for a failed login where no user resolved.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[AuditAction] = mapped_column(
        _pg_enum(AuditAction, "audit_action"), nullable=False
    )
    target_type: Mapped[AuditTargetType] = mapped_column(
        _pg_enum(AuditTargetType, "audit_target_type"), nullable=False
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audit_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    __table_args__ = (
        Index("ix_audit_logs_workspace_id_created_at", "workspace_id", "created_at"),
        Index("ix_audit_logs_actor_id_created_at", "actor_id", "created_at"),
    )
