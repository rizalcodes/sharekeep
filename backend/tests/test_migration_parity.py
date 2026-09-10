"""The migrations and the models must describe the same schema.

`conftest.py` builds the test schema from `Base.metadata`, deliberately, so a divergence
between models and migrations cannot hide behind a passing suite. It has always claimed
"a separate test asserts the migration produces the same shape" — until M4 that test did
not exist, and the gap was not theoretical: autogenerate silently dropped
`files.current_version_id`'s foreign key in M2, the migration ran clean, and only an
ad-hoc comparison caught it. Ad-hoc checks are the ones nobody runs twice.

This builds a throwaway database, upgrades it with the real migration chain, and asks
Alembic what still differs from the models. Anything at all is a failure.

The test function is deliberately synchronous: `alembic/env.py` calls `asyncio.run`
itself, which would raise inside an already-running loop.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (registers every mapped class on Base.metadata)
from alembic import command
from app.core.config import settings
from app.models.base import Base

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Alembic's own bookkeeping table is not described by the models and never should be.
IGNORED_TABLES = {"alembic_version"}


def _server_url() -> str:
    return settings.database_url.rsplit("/", 1)[0]


async def _run_on_server(statements: list[str]) -> None:
    """CREATE/DROP DATABASE cannot run inside a transaction."""
    engine = create_async_engine(
        _server_url() + "/postgres", isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _diff_against_models(url: str) -> list:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_diff_sync)
    finally:
        await engine.dispose()


def _diff_sync(connection) -> list:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    return _significant(compare_metadata(context, Base.metadata))


def _significant(diffs: list) -> list:
    kept: list = []
    for diff in diffs:
        # Alembic reports either a tuple or a list of tuples per difference.
        entries = diff if isinstance(diff, list) else [diff]
        for entry in entries:
            if entry[0] in ("add_table", "remove_table"):
                if getattr(entry[1], "name", None) in IGNORED_TABLES:
                    continue
            elif len(entry) > 1 and entry[1] in IGNORED_TABLES:
                continue
            kept.append(entry)
    return kept


def test_migrations_match_the_models():
    name = f"fileshare_parity_{uuid.uuid4().hex[:8]}"
    url = _server_url() + "/" + name

    try:
        asyncio.run(_run_on_server([f'CREATE DATABASE "{name}"']))
    except Exception as exc:  # pragma: no cover - infrastructure, not logic
        pytest.skip(f"cannot create a scratch database for the parity check: {exc}")

    original = settings.database_url
    try:
        config = Config(os.path.join(BACKEND_ROOT, "alembic.ini"))
        config.set_main_option("script_location", os.path.join(BACKEND_ROOT, "alembic"))
        # env.py reads the URL off the settings object, so pointing the scratch database
        # at the config alone would migrate the wrong database.
        object.__setattr__(settings, "database_url", url)
        command.upgrade(config, "head")
        diffs = asyncio.run(_diff_against_models(url))
    finally:
        object.__setattr__(settings, "database_url", original)
        asyncio.run(
            _run_on_server(
                [
                    # noqa justified: `name` is a uuid4 hex string this function
                    # generated, and Postgres accepts no bind parameters in DDL.
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # noqa: S608
                    f"WHERE datname = '{name}' AND pid <> pg_backend_pid()",
                    f'DROP DATABASE IF EXISTS "{name}"',
                ]
            )
        )

    assert diffs == [], (
        "The migration chain and the models disagree. Each entry below is something the "
        "migrations would have to change to match the models:\n  "
        + "\n  ".join(repr(d) for d in diffs)
    )
