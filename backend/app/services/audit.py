"""Audit logging (Schema.md §10).

Built in M1 rather than M6 because the events the audit log exists to record —
login, login_failed, member_role_changed, invitation_accepted — are produced by M1.
M6 covers the query/UI and notification side, not the table.

Append-only: this module offers no update or delete.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AuditAction, AuditTargetType
from app.models.identity import AuditLog


def client_ip(request: Request | None) -> str | None:
    """Best-effort client IP.

    Behind the VPS reverse proxy the peer address is the proxy, so X-Forwarded-For is
    consulted first. Only meaningful because the proxy is trusted and sets it — this
    header is attacker-controlled when it is not.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


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
