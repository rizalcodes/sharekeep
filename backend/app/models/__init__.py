"""SQLAlchemy models.

Every entity module must be imported here — Alembic's autogenerate only sees what is
registered on `Base.metadata`, and a model that is never imported is silently missing
from migrations. Entity modules land from M1 onward (Schema.md).
"""

from app.models.base import (
    Base,
    CreatedAtMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.models.enums import (
    AuditAction,
    AuditTargetType,
    InvitationStatus,
    WorkspaceRole,
)
from app.models.identity import (
    AuditLog,
    RefreshToken,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)

__all__ = [
    "AuditAction",
    "AuditLog",
    "AuditTargetType",
    "Base",
    "CreatedAtMixin",
    "InvitationStatus",
    "RefreshToken",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "User",
    "Workspace",
    "WorkspaceInvitation",
    "WorkspaceMember",
    "WorkspaceRole",
]
