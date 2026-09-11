"""Request/response models for folders, files and per-object permissions."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import ScanStatus, ShareLinkType, WorkspaceRole


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
    # Permission indicator (Design.md §5, M7). Folders have no share links.
    my_role: WorkspaceRole | None = None
    explicit_grants: int = 0


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
    # Resolved server-side (Architecture.md §3): a client re-deriving the access table
    # could derive it differently from the code that enforces it.
    is_downloadable: bool = False
    # Why a "not scanned" badge says what it says. Three different situations share
    # that badge, and the interface has to distinguish them (Design.md §5).
    scan_reason: str | None = None
    # A released file keeps showing that it was flagged, and adds who released it.
    scan_override_at: datetime | None = None
    scan_override_by: uuid.UUID | None = None
    scan_override_reason: str | None = None
    tags: list[str]
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    # The permission indicator (Design.md §5, M7): the viewer's own role on this item,
    # resolved server-side by the same §4.1 rules that enforce it, and whether the item
    # has live share links or explicit grants. Absent outside a listing.
    my_role: WorkspaceRole | None = None
    active_share_links: int = 0
    explicit_grants: int = 0


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
    scan_detail: dict | None = None
    # Override fields are surfaced, not hidden behind the status: the interface has to
    # keep showing that a released file was flagged and who released it (Design.md §5).
    scan_override_at: datetime | None = None
    scan_override_by: uuid.UUID | None = None
    scan_override_reason: str | None = None
    # The resolved access answer, so a client never has to re-derive the §3 table and
    # risk deriving it differently from the server.
    is_downloadable: bool = False
    uploaded_by: uuid.UUID
    # Resolved server-side (M7) so the versions panel can say who uploaded each one.
    uploaded_by_email: str | None = None
    uploaded_by_name: str | None = None
    # Set on a version created by rollback, so it reads "restored from version 3"
    # rather than as an ordinary upload (Architecture.md §4.9).
    restored_from_version_id: uuid.UUID | None = None
    restored_from_version_number: int | None = None
    created_at: datetime
    is_current: bool = False


class ScanOverrideRequest(BaseModel):
    """Releasing a file the scanner flagged demands a stated reason.

    Not optional and not free-form-empty: the audit entry is only meaningful if it
    records why someone decided to serve a file that was flagged.
    """

    reason: str = Field(min_length=10, max_length=1000)


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


class InFlightUpload(BaseModel):
    """An upload that was started but never completed (Architecture.md §2a)."""

    version_id: uuid.UUID
    file_id: uuid.UUID
    file_name: str
    folder_id: uuid.UUID | None
    total_bytes: int
    uploaded_bytes: int
    parts_completed: int
    parts_total: int
    started_at: datetime
    version_number: int


class PartLayout(BaseModel):
    """Where one part sits in the source file."""

    part_number: int
    offset: int
    size_bytes: int


class ResumeState(BaseModel):
    """What the client needs to pick an interrupted upload back up.

    `completed_parts` are already stored and must be replayed verbatim into
    upload-complete; `remaining_parts` carry freshly signed URLs, because presigned
    URLs are deliberately short-lived and a stale one would fail confusingly.

    `layout` gives the byte offset and size of EVERY part, completed or not. The client
    must not infer offsets from the remaining parts alone: if only the final (short)
    part is outstanding, an inferred chunk size is wrong and the upload would write the
    wrong byte range - corrupting the file silently, which is the worst failure a file
    platform can have.
    """

    version_id: uuid.UUID
    file_id: uuid.UUID
    file_name: str
    total_bytes: int
    completed_parts: list[CompletedPart]
    remaining_parts: list[UploadPart]
    layout: list[PartLayout]
    uploaded_bytes: int

# --- Share links (Architecture.md §4.2, added at M5) -----------------------------


class ShareLinkCreate(BaseModel):
    """The type is the primary rule; the rest are modifiers valid on any type.

    Deliberately not modelled as four mutually exclusive shapes — that reading is what
    Schema.md §9 v1.1 exists to correct, and it would make "password-protected and
    expiring" impossible to express.
    """

    link_type: ShareLinkType
    password: str | None = Field(default=None, min_length=8, max_length=128)
    expires_at: datetime | None = None
    max_downloads: int | None = Field(default=None, ge=1)
    # For a private link. Taken as an email so the sharer names a person, not a UUID.
    allowed_user_email: EmailStr | None = None


class ShareLinkOut(BaseModel):
    """A link as its owner sees it. Never carries the token — it is not stored."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_id: uuid.UUID
    link_type: ShareLinkType
    has_password: bool = False
    allowed_user_id: uuid.UUID | None = None
    expires_at: datetime | None = None
    max_downloads: int | None = None
    # Named for what it counts: link redemptions, not completed downloads
    # (Architecture.md §4.2). The API never sees the bytes.
    uses: int = 0
    revoked_at: datetime | None = None
    is_active: bool = True
    created_by: uuid.UUID
    created_at: datetime


class ShareLinkCreated(ShareLinkOut):
    """The one and only response that contains the raw token.

    Tokens are stored hashed, so this value cannot be produced again for this link.
    """

    token: str
    url_path: str


class SharePreview(BaseModel):
    """What a visitor is told before redeeming a link.

    Deliberately minimal while a gate remains. The file name is withheld until a
    required password has been supplied: sending the link and the password through
    separate channels is the whole point of password protection, so someone holding only
    the link should not learn what the file is called.
    """

    requires_password: bool
    requires_sign_in: bool
    expires_at: datetime | None = None
    # Populated only once nothing is left to satisfy.
    file_name: str | None = None
    size_bytes: int | None = None


class ShareDownloadRequest(BaseModel):
    password: str | None = None


class ShareDownloadResponse(BaseModel):
    url: str
    expires_in_seconds: int
    file_name: str
    size_bytes: int
    # Stated so a client can show it: what remains after this use, if there is a limit.
    uses_remaining: int | None = None
