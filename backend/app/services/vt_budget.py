"""Outbound VirusTotal request budget (Architecture.md §3a).

This is the counter pattern from the API rate-limiting reference, adapted from its
original inbound shape. The reference designs limits for keys *we issue* to consumers,
with tiers and revocation; here there is one key, held by us, and the thing being
protected is a third party's free-tier allowance.

Three things make this deliberately *not* an extension of `core.rate_limit`:

1. **It fails closed.** `core.rate_limit` throttles logins and fails open, because a
   Redis outage must not lock users out of the product when Argon2 is the real control.
   Here, failing open means firing at VirusTotal with no idea how much budget is left,
   which is precisely the quota burn this module exists to prevent. No reservation, no
   call.
2. **Two windows must be granted together.** Per-minute and per-day are checked and
   incremented in one atomic script. Doing it in two steps means a call can consume a
   day token, find the minute window full, and leave the day budget permanently short
   by one for every requeue.
3. **Windows are calendar buckets, not sliding TTLs.** The key carries the bucket, so
   "when does this reopen" is arithmetic on the clock rather than a question for Redis.
   TTLs only clean up.

There is no refund path. A reservation that goes unused over-counts by one, which costs
a request; a call made without a reservation risks a 429 and, worse, an inaccurate view
of a budget we cannot inspect from the outside. Over-counting is the safe direction.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "vt:budget"

# Grant both windows or neither. Returning which window blocked lets the caller wait the
# right amount of time: seconds for a minute window, potentially hours for a day.
_RESERVE_LUA = """
local minute_key   = KEYS[1]
local day_key      = KEYS[2]
local minute_limit = tonumber(ARGV[1])
local day_limit    = tonumber(ARGV[2])
local minute_ttl   = tonumber(ARGV[3])
local day_ttl      = tonumber(ARGV[4])

local minute_used = tonumber(redis.call('GET', minute_key) or '0')
local day_used    = tonumber(redis.call('GET', day_key)    or '0')

if day_used >= day_limit then
  return {0, 2, minute_used, day_used}
end
if minute_used >= minute_limit then
  return {0, 1, minute_used, day_used}
end

minute_used = redis.call('INCR', minute_key)
if minute_used == 1 then redis.call('EXPIRE', minute_key, minute_ttl) end
day_used = redis.call('INCR', day_key)
if day_used == 1 then redis.call('EXPIRE', day_key, day_ttl) end

return {1, 0, minute_used, day_used}
"""


class Window(enum.StrEnum):
    """Which budget refused the reservation."""

    NONE = "none"
    MINUTE = "minute"
    DAY = "day"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Reservation:
    """The outcome of asking for one VirusTotal request."""

    granted: bool
    blocked_by: Window
    retry_after_seconds: int
    minute_used: int
    day_used: int

    @property
    def exhausted_for_the_day(self) -> bool:
        """A file waiting on this stays `pending`, possibly for hours (§3a)."""
        return self.blocked_by is Window.DAY


_client: aioredis.Redis | None = None


def get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
    return _client


def reset_client() -> None:
    """Drop the cached connection. Used by tests that point at a different Redis."""
    global _client
    _client = None


def _buckets(now: datetime) -> tuple[str, str]:
    return (
        f"{_KEY_PREFIX}:minute:{now.strftime('%Y%m%d%H%M')}",
        f"{_KEY_PREFIX}:day:{now.strftime('%Y%m%d')}",
    )


def _seconds_until_next_minute(now: datetime) -> int:
    return max(1, 60 - now.second)


def _seconds_until_next_day(now: datetime) -> int:
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - start_of_day).total_seconds()
    return max(1, int(86400 - elapsed))


async def reserve(*, now: datetime | None = None) -> Reservation:
    """Claim one VirusTotal request, or explain when to come back.

    Never blocks and never sleeps: the caller requeues. At `concurrency=2`, two workers
    sleeping on an exhausted daily budget would stall every scan for up to a day, which
    is why Architecture §3a was amended away from blocking.
    """
    now = now or datetime.now(UTC)
    minute_key, day_key = _buckets(now)
    minute_ttl = _seconds_until_next_minute(now)
    day_ttl = _seconds_until_next_day(now)

    try:
        granted, blocked_code, minute_used, day_used = await get_client().eval(
            _RESERVE_LUA,
            2,
            minute_key,
            day_key,
            settings.virustotal_requests_per_minute,
            settings.virustotal_requests_per_day,
            # A minute bucket that is only seconds from rolling over would otherwise be
            # deleted before the window it represents has passed.
            minute_ttl + 5,
            day_ttl + 60,
        )
    except Exception as exc:  # noqa: BLE001 - deliberate fail-closed, see module docstring
        logger.warning(
            "VirusTotal budget unavailable, refusing the call: %s", type(exc).__name__
        )
        return Reservation(
            granted=False,
            blocked_by=Window.UNAVAILABLE,
            retry_after_seconds=60,
            minute_used=0,
            day_used=0,
        )

    if int(granted) == 1:
        return Reservation(
            granted=True,
            blocked_by=Window.NONE,
            retry_after_seconds=0,
            minute_used=int(minute_used),
            day_used=int(day_used),
        )

    blocked = Window.DAY if int(blocked_code) == 2 else Window.MINUTE
    return Reservation(
        granted=False,
        blocked_by=blocked,
        retry_after_seconds=day_ttl if blocked is Window.DAY else minute_ttl,
        minute_used=int(minute_used),
        day_used=int(day_used),
    )


async def usage(*, now: datetime | None = None) -> dict[str, int | bool]:
    """Current consumption, for the readiness surface (Architecture.md §3a).

    Read-only: asking how much budget is left must not spend any of it.
    """
    now = now or datetime.now(UTC)
    minute_key, day_key = _buckets(now)
    try:
        minute_used, day_used = await get_client().mget(minute_key, day_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("VirusTotal budget unreadable: %s", type(exc).__name__)
        return {"available": False}

    minute = int(minute_used or 0)
    day = int(day_used or 0)
    return {
        "available": True,
        "minute_used": minute,
        "minute_limit": settings.virustotal_requests_per_minute,
        "day_used": day,
        "day_limit": settings.virustotal_requests_per_day,
        "day_remaining": max(0, settings.virustotal_requests_per_day - day),
    }
