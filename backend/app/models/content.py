"""Folder, file, version and per-object permission entities (Schema.md §5-8).

Every constraint Schema.md declares lives here rather than only in service code: an
application check is a convention, a constraint is a guarantee.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ScanStatus, WorkspaceRole


def _pg_enum(enum_cls: type, name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )


class SoftDeleteBatchMixin:
    """Soft delete plus the batch that removed it (Architecture.md §6a).

    Restore revives exactly one batch, so restoring a folder cannot resurrect items the
    user had already deleted on purpose beforehand.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    deletion_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, default=None
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class Folder(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteBatchMixin, Base):
    __tablename__ = "folders"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    parent_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        # Postgres treats NULLs as distinct in unique indexes, which would let a
        # workspace hold many root folders of the same name. NULLS NOT DISTINCT (PG15+)
        # is the honest fix; we run Postgres 16 (Schema.md §5 left the choice to
        # implementation and asked for it to be stated).
        Index(
            "uq_folders_workspace_parent_name",
            "workspace_id",
            "parent_folder_id",
            "name",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_folders_workspace_id_parent", "workspace_id", "parent_folder_id"),
        Index("ix_folders_deletion_batch_id", "deletion_batch_id"),
        # A folder cannot be its own parent. Deeper cycles are prevented in the service
        # layer, which can walk the chain; this catches the trivial case in the database.
        CheckConstraint("id <> parent_folder_id", name="no_self_parent"),
    )


class File(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteBatchMixin, Base):
    __tablename__ = "files"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    # Size of the CURRENT version. Not what the file consumes — quota counts every
    # retained version (Schema.md §7, Architecture.md §6).
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # Nullable by necessity: file_versions.file_id is required, so the file row must
    # exist before its first version (Schema.md §6).
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("file_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    scan_status: Mapped[ScanStatus] = mapped_column(
        _pg_enum(ScanStatus, "scan_status"),
        nullable=False,
        server_default=ScanStatus.PENDING.value,
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    versions: Mapped[list[FileVersion]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        foreign_keys="FileVersion.file_id",
    )

    __table_args__ = (
        # This is what makes "re-upload with the same name creates a new version"
        # well-defined rather than ambiguous.
        Index(
            "uq_files_workspace_folder_name",
            "workspace_id",
            "folder_id",
            "name",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_files_workspace_id_folder", "workspace_id", "folder_id"),
        Index("ix_files_deletion_batch_id", "deletion_batch_id"),
        Index("ix_files_tags", "tags", postgresql_using="gin"),
    )


class FileVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, SoftDeleteBatchMixin, Base):
    __tablename__ = "file_versions"

    file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    scan_status: Mapped[ScanStatus] = mapped_column(
        _pg_enum(ScanStatus, "scan_status"),
        nullable=False,
        server_default=ScanStatus.PENDING.value,
    )
    scan_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Admin override (Schema.md §7, Architecture.md §3d). Recorded BESIDE scan_status,
    # never over it: `flagged` stays `flagged`, because nothing re-scanned the file and
    # writing `clean` here would fabricate a verdict no scanner gave.
    scan_override_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scan_override_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    scan_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # In-flight multipart state (Schema.md §7). Present between upload-init and
    # upload-complete; also what the abandoned-upload sweep aborts.
    upload_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    upload_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    file: Mapped[File] = relationship(back_populates="versions", foreign_keys=[file_id])

    __table_args__ = (
        UniqueConstraint("file_id", "version_number", name="uq_file_versions_file_id_version"),
        Index("ix_file_versions_file_id", "file_id"),
        Index("ix_file_versions_deletion_batch_id", "deletion_batch_id"),
        # The sweep looks for sessions that never completed.
        Index(
            "ix_file_versions_pending_uploads",
            "upload_started_at",
            postgresql_where=text("upload_id IS NOT NULL"),
        ),
        # An override is all three fields or none. A half-written override would leave
        # access granted with nobody accountable for granting it.
        CheckConstraint(
            "num_nonnulls(scan_override_at, scan_override_by, scan_override_reason) IN (0, 3)",
            name="override_all_or_nothing",
        ),
        # The scan worker looks up verdicts by content hash (Architecture.md §3c) and
        # the stuck-scan sweep looks up unresolved versions by status.
        Index("ix_file_versions_checksum_sha256", "checksum_sha256"),
    )

    @property
    def is_uploading(self) -> bool:
        return self.upload_id is not None


class FilePermission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Explicit grant on one file or one folder (Schema.md §8).

    Resolution order lives in Architecture.md §4.1 and is implemented once, in
    `services/permissions.py`.
    """

    __tablename__ = "file_permissions"

    file_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=True
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        _pg_enum(WorkspaceRole, "workspace_role"), nullable=False
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        # Exactly one target, enforced by the database rather than by remembering.
        CheckConstraint(
            "num_nonnulls(file_id, folder_id) = 1",
            name="exactly_one_target",
        ),
        UniqueConstraint("file_id", "user_id", name="uq_file_permissions_file_id_user_id"),
        UniqueConstraint("folder_id", "user_id", name="uq_file_permissions_folder_id_user_id"),
        Index("ix_file_permissions_user_id", "user_id"),
    )


class ScanResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Verdict cache keyed on file content (Schema.md §12a, Architecture.md §3c).

    VirusTotal's free tier allows roughly 500 requests a day, so asking again about
    content we have already asked about is the easiest way to waste that budget: two
    users uploading the same PDF should cost one request, not two.

    Deliberately holds no workspace or user reference. A hash verdict is a fact about
    file content, not about who uploaded it, and scoping it per workspace would multiply
    requests for no benefit. It is worker-internal and must never be served through a
    workspace-scoped API — there is nothing in here to isolate, and nothing that should
    let one tenant learn what another has stored.
    """

    __tablename__ = "scan_results"

    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Only settled verdicts are cached. Caching `pending` or `scan_failed` would serve a
    # non-answer back as though it were an answer.
    verdict: Mapped[ScanStatus] = mapped_column(
        _pg_enum(ScanStatus, "scan_status"), nullable=False
    )
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("checksum_sha256", name="uq_scan_results_checksum_sha256"),
        # A verdict is either clean or flagged; the transient states have no place here.
        CheckConstraint(
            "verdict IN ('clean', 'flagged')",
            name="settled_verdicts_only",
        ),
    )
