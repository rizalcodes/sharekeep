"""Request/response models for workspaces, members and invitations."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import InvitationStatus, WorkspaceRole


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def _trim(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Workspace name cannot be blank.")
        return trimmed


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    storage_quota_bytes: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Workspace name cannot be blank.")
        return trimmed


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    is_personal: bool
    storage_quota_bytes: int
    storage_used_bytes: int
    created_at: datetime
    # The requester's own role, so the client never has to infer permissions.
    role: WorkspaceRole


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    full_name: str
    role: WorkspaceRole
    created_at: datetime


class MemberRoleUpdate(BaseModel):
    role: WorkspaceRole


class InvitationCreate(BaseModel):
    email: EmailStr
    role: WorkspaceRole = WorkspaceRole.VIEWER

    @field_validator("role")
    @classmethod
    def _no_owner_invites(cls, v: WorkspaceRole) -> WorkspaceRole:
        # Ownership is transferred deliberately, not handed out by invitation.
        if v is WorkspaceRole.OWNER:
            raise ValueError("Invite as editor or viewer, then transfer ownership explicitly.")
        return v


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    email: EmailStr
    role: WorkspaceRole
    status: InvitationStatus
    invited_by: uuid.UUID
    expires_at: datetime
    created_at: datetime


class InvitationCreated(BaseModel):
    """The raw token is returned exactly once, at creation.

    Only the hash is stored. Since M6 the invitation is also emailed (with the token in
    the URL fragment, Architecture §4.6); this copy lets an owner pass it on directly if
    the email is refused.
    """

    invitation: InvitationOut
    token: str


class InvitationPreview(BaseModel):
    """Shown on the acceptance page before the user is signed in.

    Deliberately minimal: a token holder learns the workspace name and which address
    was invited, and nothing about the members or contents.
    """

    workspace_name: str
    email: EmailStr
    role: WorkspaceRole
    expires_at: datetime
    requires_signup: bool


class InvitationTokenRequest(BaseModel):
    """Carries an invitation token in a request BODY.

    Never a path or query parameter: web servers log full URLs, and a logged
    invitation token is workspace access for anyone who can read the log.
    """

    token: str
