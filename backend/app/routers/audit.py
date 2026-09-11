"""Reading the audit log (Architecture.md §4.7).

Owner-only, and the `OwnerAccess` dependency already produces exactly the two answers
§4.7 requires: a non-member gets 404 so workspace ids stay unenumerable, and a member
without the owner role gets 403 — the endpoint's existence is not a secret, only its
contents.

There is no write path here. `audit_logs` is append-only, so this router offers read and
nothing else; the entries are written by the actions they describe.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import aliased

from app.core.dependencies import DbSession, OwnerAccess
from app.models.enums import AuditAction
from app.models.identity import AuditLog, User
from app.schemas.audit import AuditEntryOut, AuditPage

router = APIRouter(prefix="/api/v1", tags=["audit"])

MAX_PAGE_SIZE = 100


def _encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    """Opaque cursor over the index's own ordering.

    `(created_at, id)`, not `created_at` alone: rows written inside one transaction can
    share a timestamp to the microsecond, and a cursor that cannot break that tie either
    repeats them or skips them.
    """
    raw = f"{created_at.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, _, row_id = raw.rpartition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(row_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        # A malformed cursor is the caller's mistake, not an empty page: silently
        # returning the first page again would hide a paging bug rather than surface it.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "That page cursor is not valid."
        ) from exc


@router.get("/workspaces/{workspace_id}/audit", response_model=AuditPage)
async def read_audit_log(
    access: OwnerAccess,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
    action: Annotated[list[AuditAction] | None, Query()] = None,
    actor_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> AuditPage:
    """One page of this workspace's history, newest first.

    Account-level events (`login`, `login_failed`, `logout`) are absent by construction
    rather than by filtering: they carry `workspace_id = NULL`, so scoping to this
    workspace excludes them. Telling an owner when a collaborator signed in to unrelated
    work is not theirs to know (§4.7).
    """
    workspace, _ = access

    actor = aliased(User)
    query = (
        select(AuditLog, actor.email, actor.full_name)
        .outerjoin(actor, actor.id == AuditLog.actor_id)
        .where(AuditLog.workspace_id == workspace.id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        # One extra row, purely to learn whether another page exists without a
        # second COUNT query over a table that only grows.
        .limit(limit + 1)
    )

    if action:
        query = query.where(AuditLog.action.in_(action))
    if actor_id is not None:
        query = query.where(AuditLog.actor_id == actor_id)
    if q:
        # Matches the recorded label and the actor's address — the two things a reader
        # actually scans for. Deliberately not a search across `metadata`: that would
        # let a query match a hashed value or an internal id and look like a hit.
        term = f"%{q.lower()}%"
        query = query.where(
            or_(
                AuditLog.audit_metadata["label"].astext.ilike(term),
                actor.email.ilike(term),
            )
        )
    if cursor:
        stamp, row_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                AuditLog.created_at < stamp,
                (AuditLog.created_at == stamp) & (AuditLog.id < row_id),
            )
        )

    rows = (await db.execute(query)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    entries = [
        AuditEntryOut(
            id=entry.id,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            # The label recorded at the time. `name` and `file_name` are the two shapes
            # M1-M5 happened to write before the convention was stated; reading them
            # here recovers what history did record instead of discarding it.
            label=(entry.audit_metadata or {}).get("label")
            or (entry.audit_metadata or {}).get("file_name")
            or (entry.audit_metadata or {}).get("name"),
            actor_id=entry.actor_id,
            actor_email=email,
            actor_name=full_name,
            # INET comes back as an ipaddress object, not a str.
            ip_address=str(entry.ip_address) if entry.ip_address else None,
            user_agent=entry.user_agent,
            metadata=entry.audit_metadata,
            created_at=entry.created_at,
        )
        for entry, email, full_name in rows
    ]

    next_cursor = (
        _encode_cursor(rows[-1][0].created_at, rows[-1][0].id) if has_more and rows else None
    )
    return AuditPage(entries=entries, next_cursor=next_cursor)
