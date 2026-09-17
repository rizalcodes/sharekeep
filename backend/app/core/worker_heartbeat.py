"""Worker liveness from a heartbeat, not a broadcast ping (Architecture.md §7a, M8).

M7c found that `celery_app.control.ping(timeout=2)` goes unanswered by a worker that is
healthy but busy: with thirteen scans queued, readiness reported `degraded` and returned
503 for a worker that answered moments later. Behind a proxy or monitor that acts on 503,
ordinary scanning load would have taken the API out of rotation.

The heartbeat is written by a thread in the worker's MAIN process, started once the worker
is ready. With the prefork pool the tasks run in child processes, so a saturated pool does
not stop the heartbeat; a dead or wedged main process does, which is the failure worth
reporting. The key expires on its own, so a killed worker disappears without anyone having
to clean up after it.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import redis
from celery.signals import worker_ready, worker_shutdown, worker_shutting_down

from app.core.config import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "fileshare:worker-heartbeat:"
INTERVAL_SECONDS = 15
# Three missed beats. Long enough to ride out a Redis blip, short enough that /status
# says a worker is gone within a minute of it going.
EXPIRY_SECONDS = 45

_stop = threading.Event()
_thread: threading.Thread | None = None


def _key(name: str) -> str:
    return f"{KEY_PREFIX}{name}"


def _beat(name: str) -> None:
    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    while not _stop.is_set():
        try:
            client.set(_key(name), str(time.time()), ex=EXPIRY_SECONDS)
        except redis.RedisError as exc:
            # Logged, never raised: a heartbeat that kills the worker is worse than none.
            logger.warning("worker heartbeat not written: %s", type(exc).__name__)
        _stop.wait(INTERVAL_SECONDS)
    try:
        client.delete(_key(name))
    except redis.RedisError:
        pass


def start(name: str) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_beat, args=(name,), name="worker-heartbeat", daemon=True)
    _thread.start()
    logger.info("worker heartbeat started for %s", name)


def stop() -> None:
    if _stop.is_set():
        return
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
    logger.info("worker heartbeat stopped")


@worker_ready.connect
def _on_worker_ready(sender=None, **_: object) -> None:
    start(getattr(sender, "hostname", None) or socket.gethostname())


# Both. With `worker_shutdown` alone, a clean `docker compose stop` left the key behind — the
# handler never ran before the process exited — so a stopped worker was reported alive until
# the key expired. `worker_shutting_down` fires at the START of a warm shutdown, while the
# main process can still delete it; verified in the container at M8. `stop` is idempotent.
@worker_shutting_down.connect
def _on_worker_shutting_down(**_: object) -> None:
    stop()


@worker_shutdown.connect
def _on_worker_shutdown(**_: object) -> None:
    stop()


async def report(client) -> dict:
    """What readiness shows beside its verdict: how many workers beat, and how recently.

    Never raises. If Redis cannot be read, Redis is already failing its own check, and
    the worker line says it could not tell rather than claiming the worker is dead.
    """
    try:
        keys = [key async for key in client.scan_iter(match=f"{KEY_PREFIX}*")]
        values = [await client.get(key) for key in keys]
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"alive": None, "detail": f"heartbeat unreadable ({type(exc).__name__})"}

    now = time.time()
    ages = [now - float(value) for value in values if value is not None]
    if not ages:
        return {"alive": False, "workers": 0, "detail": "no worker heartbeat"}
    return {
        "alive": True,
        "workers": len(ages),
        "last_seen_seconds": round(max(0.0, min(ages)), 1),
    }
