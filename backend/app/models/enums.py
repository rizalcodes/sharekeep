"""Enumerations shared by models and schemas (Schema.md)."""

from __future__ import annotations

import enum


class WorkspaceRole(enum.StrEnum):
    """Workspace-level role. Ordered least- to most-privileged by `level`."""

    VIEWER = "viewer"
    EDITOR = "editor"
    OWNER = "owner"

    @property
    def level(self) -> int:
        return _ROLE_LEVELS[self]

    def can_act_as(self, required: WorkspaceRole) -> bool:
        """Whether this role satisfies a requirement for `required` or above."""
        return self.level >= required.level


_ROLE_LEVELS: dict[WorkspaceRole, int] = {
    WorkspaceRole.VIEWER: 0,
    WorkspaceRole.EDITOR: 1,
    WorkspaceRole.OWNER: 2,
}


class InvitationStatus(enum.StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    # Set lazily by housekeeping. NEVER the authority for validation — `expires_at` is
    # (Schema.md §4), or an expired invitation the sweep has not reached is accepted.
    EXPIRED = "expired"


class AuditAction(enum.StrEnum):
    """Schema.md §10. Only the M1 subset is exercised so far; the rest land with their
    milestones. The enum is created in full so later milestones need no migration."""

    UPLOAD = "upload"
    DOWNLOAD = "download"
    SHARE_CREATED = "share_created"
    SHARE_REVOKED = "share_revoked"
    SHARE_ACCESSED = "share_accessed"
    SHARE_ACCESS_DENIED = "share_access_denied"
    DELETE = "delete"
    PERMISSION_CHANGED = "permission_changed"
    VERSION_RESTORED = "version_restored"
    FOLDER_CREATED = "folder_created"
    FOLDER_DELETED = "folder_deleted"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    MEMBER_ADDED = "member_added"
    MEMBER_REMOVED = "member_removed"
    MEMBER_ROLE_CHANGED = "member_role_changed"
    INVITATION_SENT = "invitation_sent"
    INVITATION_ACCEPTED = "invitation_accepted"
    INVITATION_REVOKED = "invitation_revoked"
    SCAN_FLAGGED = "scan_flagged"
    QUOTA_EXCEEDED = "quota_exceeded"


class AuditTargetType(enum.StrEnum):
    FILE = "file"
    FOLDER = "folder"
    SHARE_LINK = "share_link"
    WORKSPACE_MEMBER = "workspace_member"
    WORKSPACE_INVITATION = "workspace_invitation"
    WORKSPACE = "workspace"
    USER = "user"
