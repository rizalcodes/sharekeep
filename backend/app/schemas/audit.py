"""Audit-log read schemas (Architecture.md §4.7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import AuditAction, AuditTargetType


class AuditEntryOut(BaseModel):
    """One row, as the reader sees it.

    `label` is the target's name **as recorded at the time**, not as it is now. The log
    is append-only, and resolving `target_id` live would let a rename rewrite what the
    log says happened last week. `label` is therefore null for rows written before the
    convention existed (M1-M5), and the interface says so rather than inventing a name.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: AuditAction
    target_type: AuditTargetType
    target_id: uuid.UUID | None
    label: str | None
    actor_id: uuid.UUID | None
    actor_email: str | None
    actor_name: str | None
    ip_address: str | None
    user_agent: str | None
    metadata: dict[str, Any] | None
    created_at: datetime


class AuditPage(BaseModel):
    """A keyset page, newest first.

    `next_cursor` is null when the end has been reached. Keyset rather than offset
    because the table is append-only: an offset shifts every time a new row lands, so
    paging through a busy log would skip and repeat rows.
    """

    entries: list[AuditEntryOut]
    next_cursor: str | None
    # What the log deliberately does not contain, so the absence reads as a decision
    # rather than a gap (Design.md §5).
    excludes_account_events: bool = True
