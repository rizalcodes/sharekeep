"""Request/response models for auth and identity."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, validate_password_strength
from app.models.enums import WorkspaceRole


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    full_name: str = Field(min_length=1, max_length=255)
    # Optional: present when signup was reached from an invitation link. The email is
    # taken from the invitation, not from this payload (Architecture.md §4.6).
    invitation_token: str | None = None

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        if error := validate_password_strength(v):
            raise ValueError(error)
        return v

    @field_validator("full_name")
    @classmethod
    def _trim(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Full name cannot be blank.")
        return trimmed


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    email_verified_at: datetime | None
    created_at: datetime


class WorkspaceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    is_personal: bool
    role: WorkspaceRole


class CsrfOut(BaseModel):
    csrf_token: str


class SessionOut(BaseModel):
    """Returned on signup/login/refresh. Tokens are NOT in the body — they are set as
    httpOnly cookies, so a script can never read them (Architecture.md §9)."""

    user: UserOut
    workspaces: list[WorkspaceSummary]
    # None on /me when the access cookie is unreadable; set on signup/login/refresh.
    access_token_expires_at: datetime | None = None
    # The CSRF token for this session (Architecture.md §4.0, M8). Unlike the auth tokens
    # it is MEANT to be read by the page: its whole job is to be copied into a header by
    # a script that can read our responses, which a cross-site page cannot. None on /me,
    # which does not rotate it.
    csrf_token: str | None = None
