"""M7a: version rollback and the retention sweep

Hand-written, like 0004-0006; the migration/model parity test checks it.

- `file_versions.restored_from_version_id` records a rollback's source (Architecture §4.9).
- `file_versions.purged_at` records that the retention sweep deleted a version's object,
  which Architecture §6a described and nothing implemented until M7 (§6b).
- `audit_action` gains `version_rolled_back`. `version_restored` has always meant
  restore-from-trash, and one action meaning two things would defeat the log.

`ADD VALUE IF NOT EXISTS`, as in 0004, so a downgrade followed by an upgrade works; the
downgrade leaves the enum value in place because Postgres cannot drop one.

Revision ID: 0007_m7_rollback_purge
Revises: 0006_m6_notifications
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_m7_rollback_purge"
down_revision: str | None = "0006_m6_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "file_versions",
        sa.Column("restored_from_version_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_file_versions_restored_from_version_id_file_versions"),
        "file_versions",
        "file_versions",
        ["restored_from_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "file_versions",
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_file_versions_awaiting_purge",
        "file_versions",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL AND purged_at IS NULL"),
    )
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older servers;
    # PG16 allows it, and IF NOT EXISTS makes re-upgrade after downgrade idempotent.
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'version_rolled_back'")


def downgrade() -> None:
    op.drop_index("ix_file_versions_awaiting_purge", table_name="file_versions")
    op.drop_column("file_versions", "purged_at")
    op.drop_constraint(
        op.f("fk_file_versions_restored_from_version_id_file_versions"),
        "file_versions",
        type_="foreignkey",
    )
    op.drop_column("file_versions", "restored_from_version_id")
