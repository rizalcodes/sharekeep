"""Liveness and readiness endpoints.

`/health` answers "is the process up". `/health/ready` actually touches every
dependency — Postgres, Redis, Celery, S3 — because a readiness check that reports
from cached state is a readiness check that lies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import worker_heartbeat
from app.core.config import settings
from app.core.database import get_db
from app.services import storage, vt_budget

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


async def _check_database(db: AsyncSession) -> dict[str, Any]:
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        return {"ok": False, "detail": type(exc).__name__}
    return {"ok": True}


async def _check_redis() -> dict[str, Any]:
    client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
    try:
        await client.ping()
    except Exception as exc:
        return {"ok": False, "detail": type(exc).__name__}
    finally:
        await client.aclose()
    return {"ok": True}


async def _check_storage() -> dict[str, Any]:
    return await asyncio.to_thread(storage.check_connectivity)


@router.get("/health/ready")
async def readiness(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    database, redis_status, storage_status = await asyncio.gather(
        _check_database(db),
        _check_redis(),
        _check_storage(),
    )
    # What serving a request needs (Architecture.md §7a, M8). The Celery worker is not in
    # here: M7c showed a broadcast ping going unanswered by a healthy worker that was busy,
    # and a busy worker must not take the API out of rotation — uploads and downloads keep
    # working while scans wait. It is reported beside the verdict instead, below.
    checks = {
        "database": database,
        "redis": redis_status,
        "storage": storage_status,
    }
    all_ok = all(check["ok"] for check in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    # Reported alongside the checks, never as one of them. An exhausted daily scan
    # budget is ordinary operation on a free tier, not a sick service, and letting it
    # turn readiness into 503 would take the whole API out of rotation over a quota
    # that resets at midnight (Architecture.md §3a).
    scanner: dict[str, Any] = {"enabled": settings.scanning_enabled}
    if settings.scanning_enabled:
        scanner.update(await vt_budget.usage())

    heartbeat_client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
    try:
        worker = await worker_heartbeat.report(heartbeat_client)
    finally:
        await heartbeat_client.aclose()

    return {
        "status": "ready" if all_ok else "degraded",
        "checks": checks,
        "worker": worker,
        "scanner": scanner,
    }
