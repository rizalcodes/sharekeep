"""Audit logging (Schema.md §10).

Built in M1 rather than M6 because the events the audit log exists to record —
login, login_failed, member_role_changed, invitation_accepted — are produced by M1.
M6 covers the query/UI and notification side, not the table.

Append-only: this module offers no update or delete.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import AuditAction, AuditTargetType
from app.models.identity import AuditLog


def client_ip(request: Request | None) -> str | None:
    """The client's address, as far as the proxies we actually trust can vouch for it.

    Each reverse proxy APPENDS the address it received the request from to
    X-Forwarded-For; it does not replace what the client sent. So with N trusted proxies
    in front of us the client is the Nth entry from the right, and every entry to the
    left of it is text the client chose. Until M8 this took the leftmost entry: a
    different made-up address per request made 80 share-token attempts with no refusal,
    and let a visitor write any IP they liked into the audit log (Architecture §4.4).

    With `trusted_proxy_hops = 0` (development, tests) the header is ignored and the TCP
    peer is the answer. An entry that is not an IP address is ignored as well: the audit
    column is INET, and a malformed header must not be able to break the write.
    """
    if request is None:
        return None
    peer = request.client.host if request.client else None
    hops = settings.trusted_proxy_hops
    if hops <= 0:
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    entries = [entry.strip() for entry in forwarded.split(",")] if forwarded else []
    entries = [entry for entry in entries if entry]
    if not entries:
        return peer
    # Fewer entries than trusted hops means the request skipped an outer proxy; every
    # entry present was still appended by a proxy we trust, so the furthest one is the
    # best observation we have.
    candidate = entries[-hops] if len(entries) >= hops else entries[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer


def user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    agent = request.headers.get("user-agent")
    return agent[:512] if agent else None


async def record(
    db: AsyncSession,
    *,
    action: AuditAction,
    target_type: AuditTargetType,
    target_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    request: Request | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit entry.

    Deliberately does NOT commit — the entry joins the caller's transaction, so an
    action that is rolled back cannot leave an audit trail claiming it happened.
    """
    entry = AuditLog(
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        ip_address=client_ip(request),
        user_agent=user_agent(request),
        audit_metadata=metadata,
    )
    db.add(entry)
    return entry
