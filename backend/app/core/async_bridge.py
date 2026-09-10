"""One event loop per worker process, for running async code from Celery tasks.

Celery tasks are synchronous and the data layer is async, so every task needs a bridge.
The obvious bridge — `asyncio.run(coro)` — is wrong here, and M4 manual validation is
what proved it.

`asyncio.run` builds a **new** event loop on every call and closes it afterwards. The
async database engine and the Redis clients are module-level singletons, and their
pooled connections stay bound to the loop that opened them. So the first task in a
worker process succeeds and the second one fails with:

    RuntimeError: got Future <Future pending> attached to a different loop

The pre-M4 tasks hid this: nothing ran more than once per process, because the
maintenance tasks fire a few times a day and each landed in a fresh fork. Scanning runs
per upload, so the second upload in a worker's lifetime broke — and broke in a way that
looked like a database fault rather than a loop-management one. Worse, the budget
limiter dutifully failed closed on the same error and reported "budget unavailable",
which is honest but points at entirely the wrong culprit.

The fix is a loop that lives as long as the process, created lazily so a forked child
never inherits its parent's. On creating one, connections carried over from a fork are
abandoned rather than closed: closing them would mean driving a dead loop, and they
belong to a process that is no longer running this code.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_loop_pid: int | None = None


async def _discard_inherited_connections() -> None:
    """Drop pooled handles this process did not open itself.

    `close=False` is deliberate: after a fork the sockets belong to the parent, and
    closing them here would both drive a loop that no longer exists and yank the
    connection out from under the parent.
    """
    from app.core.database import engine

    await engine.dispose(close=False)


def _reset_singletons() -> None:
    from app.core import rate_limit
    from app.services import vt_budget

    vt_budget.reset_client()
    rate_limit._client = None  # noqa: SLF001 - same package, and this is its reset path


def get_loop() -> asyncio.AbstractEventLoop:
    """The loop for this process, created on first use."""
    global _loop, _loop_pid
    pid = os.getpid()
    if _loop is None or _loop.is_closed() or _loop_pid != pid:
        _loop = asyncio.new_event_loop()
        _loop_pid = pid
        asyncio.set_event_loop(_loop)
        _reset_singletons()
        _loop.run_until_complete(_discard_inherited_connections())
        logger.debug("created the event loop for pid %s", pid)
    return _loop


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async function from synchronous Celery task code."""
    return get_loop().run_until_complete(coro)
