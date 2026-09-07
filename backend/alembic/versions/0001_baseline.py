"""baseline

Empty baseline so every later migration has a stable root and the alembic_version
table exists from M0 onward. Entity tables arrive from M1 (Schema.md).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-07
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
