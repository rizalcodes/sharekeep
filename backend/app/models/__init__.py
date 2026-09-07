"""SQLAlchemy models.

Every entity module must be imported here — Alembic's autogenerate only sees what is
registered on `Base.metadata`, and a model that is never imported is silently missing
from migrations. Entity modules land from M1 onward (Schema.md).
"""

from app.models.base import (
    Base,
    CreatedAtMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

__all__ = [
    "Base",
    "CreatedAtMixin",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
]
