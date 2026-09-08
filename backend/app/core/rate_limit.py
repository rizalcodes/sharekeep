"""Redis-backed fixed-window rate limiting.

Architecture.md §4.0 requires login throttling per IP and per email; §4.4 requires the
same for share-link endpoints at M5. One implementation serves both.

Fails open on a Redis outage: a storage problem should not lock every user out of the
product. The tradeoff is recorded rather than assumed — throttling is a mitigation
here, not the primary control, since passwords are Argon2-hashed regardless.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None


def get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
    return _client


async def hit(key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Count one attempt. Returns (allowed, attempts_so_far)."""
    redis_key = f"ratelimit:{key}"
    try:
        client = get_client()
        pipe = client.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds)
        count, _ = await pipe.execute()
    except Exception as exc:  # noqa: BLE001 - deliberate fail-open
        logger.warning("rate limiter unavailable, allowing request: %s", type(exc).__name__)
        return True, 0
    return int(count) <= limit, int(count)


async def reset(key: str) -> None:
    """Clear a counter, e.g. after a successful login."""
    try:
        await get_client().delete(f"ratelimit:{key}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("rate limiter reset failed: %s", type(exc).__name__)
