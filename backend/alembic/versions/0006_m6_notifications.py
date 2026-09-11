"""M6: notifications

Hand-written, like 0004 and 0005. The migration/model parity test is what checks this
file, rather than the generator being trusted to produce it.

Three genuinely new enum types are created up front rather than left to `create_table`,
which is the fault that would have aborted the M2 migration halfway. `quota_exceeded`
already exists in `audit_action`: M1 created that enum with its full value list, so
nothing here needs `ALTER TYPE` even though M6 is the first milestone to write the value.

Nothing existing is altered. Every column added is on a new table, so this migration
cannot fail against populated data.

Revision ID: 0006_m6_notifications
Revises: 0005_m5_share_links
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_m6_notifications"
down_revision: str | None = "0005_m5_share_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPE_VALUES = (
    "file_shared",
    "permission_granted",
    "storage_quota_warning",
    "scan_flagged",
    "workspace_invitation",
)
CHANNEL_VALUES = ("email",)
STATUS_VALUES = ("pending", "sent", "failed")

NOTIFICATION_TYPE = postgresql.ENUM(
    *TYPE_VALUES, name="notification_type", create_type=False
)
NOTIFICATION_CHANNEL = postgresql.ENUM(
    *CHANNEL_VALUES, name="notification_channel", create_type=False
)
NOTIFICATION_STATUS = postgresql.ENUM(
    *STATUS_VALUES, name="notification_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*TYPE_VALUES, name="notification_type").create(bind, checkfirst=True)
    postgresql.ENUM(*CHANNEL_VALUES, name="notification_channel").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*STATUS_VALUES, name="notification_status").create(
        bind, checkfirst=True
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        # Nullable: an invitation is addressed to someone who may have no account yet.
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("type", NOTIFICATION_TYPE, nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "channel", NOTIFICATION_CHANNEL, nullable=False, server_default="email"
        ),
        sa.Column(
            "status", NOTIFICATION_STATUS, nullable=False, server_default="pending"
        ),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redirected_to", sa.String(length=320), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_notifications_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    # The delivery task's only query: oldest pending rows first.
    op.create_index(
        "ix_notifications_status_created_at", "notifications", ["status", "created_at"]
    )
    op.create_index(
        "ix_notifications_user_id_created_at", "notifications", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_id_created_at", table_name="notifications")
    op.drop_index("ix_notifications_status_created_at", table_name="notifications")
    op.drop_table("notifications")
    postgresql.ENUM(name="notification_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="notification_channel").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="notification_type").drop(op.get_bind(), checkfirst=True)
