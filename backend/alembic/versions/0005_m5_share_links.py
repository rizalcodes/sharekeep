"""M5: share_links

Hand-written, like 0004. Autogenerate produced two silent faults in M2, so the
migration/model parity test (added at M4) is what checks this file rather than the
generator being trusted to produce it.

One thing worth stating: `share_link_type` is a genuinely new enum type, so it is
created once up front rather than left to `create_table` — the same fault that would
have aborted the M2 migration halfway. The four `share_*` audit actions and the
`share_link` target type already exist: M1 created both enums with their full value
lists, so nothing here needs `ALTER TYPE`.

Revision ID: 0005_m5_share_links
Revises: 0004_m4_scanning
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_m5_share_links"
down_revision: str | None = "0004_m4_scanning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHARE_LINK_TYPE_VALUES = ("public", "private", "password_protected", "expiring")
SHARE_LINK_TYPE = postgresql.ENUM(
    *SHARE_LINK_TYPE_VALUES, name="share_link_type", create_type=False
)


def upgrade() -> None:
    postgresql.ENUM(*SHARE_LINK_TYPE_VALUES, name="share_link_type").create(
        op.get_bind(), checkfirst=True
    )

    op.create_table(
        "share_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        # SHA-256 hex, never the raw token (Schema.md §9, Rules.md §3).
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("link_type", SHARE_LINK_TYPE, nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=True),
        sa.Column("allowed_user_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_downloads", sa.Integer(), nullable=True),
        sa.Column(
            "download_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(link_type <> 'password_protected' OR hashed_password IS NOT NULL) "
            "AND (link_type <> 'expiring' OR expires_at IS NOT NULL) "
            "AND (link_type <> 'private' OR allowed_user_id IS NOT NULL)",
            name=op.f("ck_share_links_type_requirements"),
        ),
        sa.CheckConstraint(
            "max_downloads IS NULL OR max_downloads > 0",
            name=op.f("ck_share_links_usable_download_limit"),
        ),
        sa.ForeignKeyConstraint(
            ["allowed_user_id"],
            ["users.id"],
            name=op.f("fk_share_links_allowed_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_share_links_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name=op.f("fk_share_links_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_share_links")),
        sa.UniqueConstraint("token_hash", name="uq_share_links_token_hash"),
    )
    op.create_index("ix_share_links_file_id", "share_links", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_share_links_file_id", table_name="share_links")
    op.drop_table("share_links")
    postgresql.ENUM(name="share_link_type").drop(op.get_bind(), checkfirst=True)
