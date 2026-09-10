"""Celery tasks must survive running more than once in a worker process.

M4 manual validation caught this, and nothing else could have: `asyncio.run` builds a
fresh event loop per call, while the database engine and the Redis clients are
module-level singletons whose pooled connections stay bound to the loop that opened
them. The first task in a process succeeded; the second died with

    RuntimeError: got Future <Future pending> attached to a different loop

The pre-M4 tasks hid it by running at most once per fork. Scanning runs per upload, so
the second upload broke — and it surfaced as a database error and a "budget
unavailable" warning, both pointing away from the actual cause.

The check runs in a subprocess on purpose. The failure is about which loop owns a
pooled connection, so it cannot be reproduced inside pytest's own loop, and forcing a
loop swap in-process would disturb every other test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

SCRIPT = textwrap.dedent(
    """
    from sqlalchemy import text

    from app.core import async_bridge
    from app.core.database import SessionLocal

    async def touch_the_database():
        async with SessionLocal() as db:
            return (await db.execute(text("SELECT 1"))).scalar_one()

    # Two sequential runs in ONE process is the whole test: the first always worked.
    first = async_bridge.run(touch_the_database())
    second = async_bridge.run(touch_the_database())
    assert first == 1 and second == 1, (first, second)

    # And it must be the same loop, because that is why the connections stay usable.
    assert async_bridge.get_loop() is async_bridge.get_loop()
    print("BRIDGE OK")
    """
)


def test_two_task_runs_in_one_process_both_succeed():
    env = dict(os.environ)
    # noqa justified: the script is a constant defined in this file and the
    # interpreter is our own; there is no external input anywhere in the call.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        timeout=120,
    )
    if "could not translate host name" in result.stderr or "Connect call failed" in result.stderr:
        pytest.skip("no database reachable from the subprocess")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "BRIDGE OK" in result.stdout


def test_asyncio_run_would_have_failed_the_same_way():
    """The negative control: prove the old bridge really is broken.

    Without this, the test above only shows that the current code works — it would not
    show that the thing it replaced was the problem, and a regression test that cannot
    fail on the old code is not evidence of anything.
    """
    broken = SCRIPT.replace(
        "from app.core import async_bridge", "import asyncio"
    ).replace("async_bridge.run", "asyncio.run").replace(
        "assert async_bridge.get_loop() is async_bridge.get_loop()", "pass"
    )
    result = subprocess.run(  # noqa: S603 - same constant script, edited in-file
        [sys.executable, "-c", broken],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        timeout=120,
    )
    if "could not translate host name" in result.stderr or "Connect call failed" in result.stderr:
        pytest.skip("no database reachable from the subprocess")
    assert result.returncode != 0, (
        "asyncio.run per task did NOT fail, so this regression test proves nothing "
        f"about the fix.\nstdout:\n{result.stdout}"
    )
    assert "attached to a different loop" in result.stderr, result.stderr[-800:]
