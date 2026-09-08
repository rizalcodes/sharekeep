"""Request/response models for folders, files and per-object permissions."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import ScanStatus, WorkspaceRole


def _clean_name(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("Name cannot be blank.")
    # Path separators in a display name invite confusion between the logical tree and
    # the storage key. The key is built from UUIDs, but the name should stay a name.
    if "/" in trimmed or "\\" in trimmed:
        raise ValueError("Name cannot contain slashes.")
    if trimmed in (".", ".."):
        raise ValueError("Name cannot be '.' or '..'.")
    return trimmed


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_folder_id: uuid.UUID | None = None

    _check = field_validator("name")(classmethod(lambda cls, v: _clean_name(v)))


class FolderUpdate(BaseModel):
    """`parent_folder_id` is only applied when `move` is true, so that moving to the
    workspace root (null) is distinguishable from 'do not move'."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_folder_id: uuid.UUID | None = None
    move: bool = False

    @field_validator("name")
    @classmethod
    def _validate(cls, v: str | None) -> str | None:
        return None if v is None else _clean_name(v)


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    parent_folder_id: uuid.UUID | None
    name: str
    created_by: uuid.UUID
    created_at: datetime


class BreadcrumbEntry(BaseModel):
    id: uuid.UUID
    name: str


class FolderListing(BaseModel):
    folder: FolderOut | None
    breadcrumb: list[BreadcrumbEntry]
    folders: list[FolderOut]
    files: list[FileOut]


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    folder_id: uuid.UUID | None
    name: str
    mime_type: str
    size_bytes: int
    scan_status: ScanStatus
    tags: list[str]
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class FileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    folder_id: uuid.UUID | None = None
    move: bool = False
    tags: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _validate(cls, v: str | None) -> str | None:
        return None if v is None else _clean_name(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = [t.strip().lower() for t in v if t.strip()]
        if len(cleaned) > 25:
            raise ValueError("At most 25 tags.")
        return sorted(set(cleaned))


class UploadInitRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    mime_type: str = Field(default="application/octet-stream", max_length=255)
    folder_id: uuid.UUID | None = None

    _check = field_validator("name")(classmethod(lambda cls, v: _clean_name(v)))


class UploadPart(BaseModel):
    part_number: int
    size_bytes: int
    url: str


class UploadInitResponse(BaseModel):
    file_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    parts: list[UploadPart]
    # So the client can tell whether a re-upload created a new version.
    is_new_file: bool


class CompletedPart(BaseModel):
    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1, max_length=256)


class UploadCompleteRequest(BaseModel):
    version_id: uuid.UUID
    parts: list[CompletedPart] = Field(min_length=1)


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version_number: int
    size_bytes: int
    checksum_sha256: str | None
    scan_status: ScanStatus
    uploaded_by: uuid.UUID
    created_at: datetime
    is_current: bool = False


class DownloadResponse(BaseModel):
    url: str
    expires_in_seconds: int
    scan_status: ScanStatus


class DeletionReceipt(BaseModel):
    """The batch id is how a restore names what it wants back (Architecture.md §6a)."""

    deletion_batch_id: uuid.UUID
    items_deleted: int
    bytes_released: int


class RestoreRequest(BaseModel):
    deletion_batch_id: uuid.UUID


class QuotaOut(BaseModel):
    storage_quota_bytes: int
    storage_used_bytes: int
    storage_available_bytes: int
    max_file_size_bytes: int


class GrantCreate(BaseModel):
    user_id: uuid.UUID
    role: WorkspaceRole


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_id: uuid.UUID | None
    folder_id: uuid.UUID | None
    user_id: uuid.UUID
    email: EmailStr | None = None
    role: WorkspaceRole
    granted_by: uuid.UUID
    created_at: datetime


FolderListing.model_rebuild()
