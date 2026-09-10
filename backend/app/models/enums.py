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


class ScanStatus(enum.StrEnum):
    """Malware scan state (Schema.md §6-7).

    The access rule keyed on these values lives in Architecture.md §3: only `clean` and
    `scan_skipped` may be served. M4 drives the transitions; M2 only sets the value an
    upload starts with.
    """

    PENDING = "pending"
    SCANNING = "scanning"
    CLEAN = "clean"
    FLAGGED = "flagged"
    # File was too large for the scanner to accept, or scanning is not enabled yet.
    # Served, but with a visible "not scanned" badge — the badge must tell the truth.
    SCAN_SKIPPED = "scan_skipped"
    SCAN_FAILED = "scan_failed"

    @property
    def allows_download_without_override(self) -> bool:
        """Whether this status alone permits serving bytes (Architecture.md §3).

        Not the full rule. Since M4 an admin override recorded on a version can also
        permit a download, so a caller deciding whether to issue a URL must use
        `scan.version_is_downloadable`, which considers both. This property exists for
        that function and for tests of the status table itself.
        """
        return self in (ScanStatus.CLEAN, ScanStatus.SCAN_SKIPPED)

    @property
    def is_overridable(self) -> bool:
        """Whether an admin may release a version in this state (Architecture.md §3d).

        Only the two dead-end states. Overriding `pending` or `scanning` would race the
        scan that is about to answer, and overriding a downloadable state is a no-op.
        """
        return self in (ScanStatus.FLAGGED, ScanStatus.SCAN_FAILED)


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
    # Added at M4. Releasing a file the scanner flagged is exactly the kind of act an
    # audit log exists for, so this one is not optional (Architecture.md §3d).
    SCAN_OVERRIDE = "scan_override"
    QUOTA_EXCEEDED = "quota_exceeded"


class AuditTargetType(enum.StrEnum):
    FILE = "file"
    FOLDER = "folder"
    SHARE_LINK = "share_link"
    WORKSPACE_MEMBER = "workspace_member"
    WORKSPACE_INVITATION = "workspace_invitation"
    WORKSPACE = "workspace"
    USER = "user"
