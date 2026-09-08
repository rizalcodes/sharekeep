from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

# Keep tests off any real infrastructure.
os.environ.setdefault("FILESHARE_ENVIRONMENT", "local")
os.environ.setdefault("FILESHARE_SUPABASE_STORAGE_BUCKET", "fileshare-test-bucket")
os.environ.setdefault("FILESHARE_SUPABASE_URL", "https://testproject.supabase.co")
os.environ.setdefault("FILESHARE_FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("FILESHARE_COOKIE_SECURE", "false")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


# --- Database-backed fixtures ----------------------------------------------------
#
# M1 is the first milestone with real persistence, so these are new. Each test runs
# inside a transaction that is rolled back afterwards: tests cannot leak state into
# each other, and the suite leaves the database exactly as it found it.

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.base import Base  # noqa: E402

TEST_DATABASE_URL = os.environ.get(
    "FILESHARE_TEST_DATABASE_URL",
    settings.database_url.rsplit("/", 1)[0] + "/fileshare_test",
)


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    # The schema is created from the models rather than by running migrations, so a
    # model/migration divergence cannot hide here. A separate test asserts the
    # migration produces the same shape.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """A session bound to a transaction that is always rolled back."""
    connection = await db_engine.connect()
    transaction = await connection.begin()
    maker = async_sessionmaker(bind=connection, expire_on_commit=False, autoflush=False)
    session = maker()
    try:
        yield session
    finally:
        await session.close()
        # A test that triggered an expected IntegrityError will have rolled back
        # already; rolling back twice is not an error worth surfacing.
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture(loop_scope="session")
async def client(db: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client whose requests share the test's rolled-back session."""

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as async_client:
        yield async_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(loop_scope="session")
async def make_client(db: AsyncSession):
    """Factory for independent clients, each with its own cookie jar.

    Multi-user tests need genuinely separate sessions — reusing one client would let
    one user's cookies leak into another's requests and quietly invalidate the test.
    """
    clients: list[httpx.AsyncClient] = []

    async def _factory() -> httpx.AsyncClient:
        async def _override_get_db():
            yield db

        app.dependency_overrides[get_db] = _override_get_db
        instance = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        clients.append(instance)
        return instance

    yield _factory

    for instance in clients:
        await instance.aclose()
    app.dependency_overrides.clear()


DEFAULT_PASSWORD = "correct-horse-battery-staple"


async def signup(
    client: httpx.AsyncClient,
    email: str,
    *,
    full_name: str = "Test User",
    password: str = DEFAULT_PASSWORD,
    invitation_token: str | None = None,
) -> httpx.Response:
    body = {"email": email, "password": password, "full_name": full_name}
    if invitation_token:
        body["invitation_token"] = invitation_token
    return await client.post("/api/v1/auth/signup", json=body)


async def new_user(make_client_factory, email: str, **kwargs) -> tuple[httpx.AsyncClient, dict]:
    """Create a signed-in user, returning their client and session payload."""
    client = await make_client_factory()
    response = await signup(client, email, **kwargs)
    assert response.status_code == 201, response.text
    return client, response.json()
