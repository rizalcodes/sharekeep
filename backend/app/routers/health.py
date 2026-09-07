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

from app.core.config import settings
from app.core.database import get_db
from app.services import storage

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


async def _check_celery() -> dict[str, Any]:
    """Ask the broker whether any worker answers a ping."""
    from app.core.celery_app import celery_app

    def _ping() -> list[dict[str, Any]] | None:
        return celery_app.control.ping(timeout=2.0)

    try:
        replies = await asyncio.to_thread(_ping)
    except Exception as exc:
        return {"ok": False, "detail": type(exc).__name__}
    if not replies:
        return {"ok": False, "detail": "no workers responded"}
    return {"ok": True, "workers": len(replies)}


async def _check_storage() -> dict[str, Any]:
    return await asyncio.to_thread(storage.check_connectivity)


@router.get("/health/ready")
async def readiness(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    database, redis_status, celery_status, storage_status = await asyncio.gather(
        _check_database(db),
        _check_redis(),
        _check_celery(),
        _check_storage(),
    )
    checks = {
        "database": database,
        "redis": redis_status,
        "celery": celery_status,
        "storage": storage_status,
    }
    all_ok = all(check["ok"] for check in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if all_ok else "degraded", "checks": checks}
