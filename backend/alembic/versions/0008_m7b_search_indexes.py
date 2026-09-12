"""M7b: trigram indexes for substring search

`ILIKE '%term%'` cannot use a B-tree index, so searching names by substring would mean a
sequential scan of every file in the workspace (Architecture §4.8). `pg_trgm` makes it
indexable.

Hand-written, like 0004-0007. Two things worth stating:

- `CREATE EXTENSION` needs a privileged role. It works here because this project runs its
  own Postgres container whose application user owns the database; it was checked against
  the live container (`pg_available_extensions` lists pg_trgm 1.6, and the `fileshare`
  role is a superuser) rather than assumed. On a managed Postgres that does not allow it,
  this migration is where the failure would surface.
- Folders are searched by name too, so both `files.name` and `folders.name` get an index.
  Indexing only files would leave half the query on a sequential scan.

Revision ID: 0008_m7b_search_indexes
Revises: 0007_m7_rollback_purge
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_m7b_search_indexes"
down_revision: str | None = "0007_m7_rollback_purge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # gin_trgm_ops, not the default: it indexes trigrams of the value, which is what
    # makes a leading-wildcard ILIKE usable.
    op.execute(
        "CREATE INDEX ix_files_name_trgm ON files USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_folders_name_trgm ON folders USING gin (name gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_folders_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_files_name_trgm")
    # The extension is deliberately left in place: another migration, or a future index,
    # may depend on it, and dropping a shared extension on a downgrade is a bigger
    # side effect than leaving it.
