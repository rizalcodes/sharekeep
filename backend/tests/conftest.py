from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from types import SimpleNamespace

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


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def clear_login_throttle():
    """Reset the login throttle counters before each test.

    Found at M4: `core.rate_limit` counts against the real Redis, and those counters
    outlive a test run. Running the suite a few times in a row therefore made legitimate
    logins return 429 — four M1 auth tests failing for a reason that had nothing to do
    with the code under test. The counters were leaking between runs, and between tests.

    A test that wants to prove throttling still can: it generates its own attempts
    inside the test, from a clean count.
    """
    from app.core import rate_limit

    try:
        client = rate_limit.get_client()
        keys = [key async for key in client.scan_iter(match="ratelimit:*")]
        if keys:
            await client.delete(*keys)
    except Exception as exc:  # noqa: BLE001 - no Redis is not a reason to fail every test
        print(f"could not clear login throttle counters: {type(exc).__name__}: {exc}")


@pytest.fixture(autouse=True)
def stub_task_dispatch(monkeypatch, request):
    """Keep the suite out of the real Celery broker.

    Added at M4, after a defect: `upload_file` drives the real upload-complete endpoint,
    which enqueues `compute_checksum`. With a broker URL configured, the suite pushed a
    task per uploaded file into the live queue — 468 of them in one run — where the
    running worker rejected each as an unregistered task. Nothing corrupted, but the
    tests were reaching outside themselves, and on a worker running this code they would
    have been executed against versions that exist only in the test database.

    Dispatched tasks are recorded so a test can assert the enqueue happened, which is
    the part worth asserting; whether Celery can deliver it is Celery's business.
    """
    dispatched: list[tuple[str, tuple, dict]] = []

    def record(name):
        def delay(*args, **kwargs):
            dispatched.append((name, args, kwargs))
            return SimpleNamespace(id="test-task-id")

        return delay

    from app.tasks import content_tasks, scan_tasks

    monkeypatch.setattr(
        content_tasks.compute_checksum, "delay", record("compute_checksum")
    )
    monkeypatch.setattr(
        scan_tasks.scan_file_version, "delay", record("scan_file_version")
    )
    request.node.dispatched_tasks = dispatched
    return dispatched


@pytest.fixture
def dispatched_tasks(stub_task_dispatch):
    """The tasks a test caused to be enqueued, in order."""
    return stub_task_dispatch


@pytest.fixture(autouse=True)
def stub_storage(monkeypatch):
    """Keep the suite off real object storage.

    Every test here is about our own logic — quota arithmetic, permission resolution,
    version pruning — not about whether Supabase works. That was proven end to end
    with real bytes during M0, and is re-proven in M2 manual validation.
    """
    uploads: dict[str, list] = {}
    # Lets a test simulate parts that already landed, keyed by storage key.
    landed_parts: dict[str, list[int]] = {}

    def create_multipart_upload(storage_key, content_type):
        upload_id = f"upload-{len(uploads) + 1}"
        uploads[upload_id] = []
        return upload_id

    def presign_part(storage_key, upload_id, part_number):
        return f"https://storage.test/{storage_key}?partNumber={part_number}"

    def complete_multipart_upload(storage_key, upload_id, parts):
        uploads.pop(upload_id, None)

    def abort_multipart_upload(storage_key, upload_id):
        uploads.pop(upload_id, None)

    from app.services import storage as storage_module

    monkeypatch.setattr(storage_module, "create_multipart_upload", create_multipart_upload)
    monkeypatch.setattr(storage_module, "presign_part", presign_part)
    monkeypatch.setattr(storage_module, "complete_multipart_upload", complete_multipart_upload)
    monkeypatch.setattr(storage_module, "abort_multipart_upload", abort_multipart_upload)
    # Mirrors the real provider's shape: part number and ETag only, no size. An
    # earlier stub implied a `size_bytes` field that Supabase does not return, and the
    # suite passed while the real endpoint raised KeyError on its first live call.
    monkeypatch.setattr(
        storage_module,
        "list_parts",
        lambda key, upload_id: [
            {"part_number": n, "etag": f'"etag-{n}"'} for n in landed_parts.get(key, [])
        ],
    )
    monkeypatch.setattr(storage_module, "delete_object", lambda key: None)
    monkeypatch.setattr(
        storage_module,
        "generate_download_url",
        lambda key, download_filename=None: f"https://storage.test/{key}?signed=1",
    )
    return {"uploads": uploads, "landed_parts": landed_parts}


async def upload_file(
    client: httpx.AsyncClient,
    workspace_id,
    name: str,
    *,
    size_bytes: int = 1024,
    folder_id=None,
    mime_type: str = "text/plain",
):
    """Drive a full upload through the real endpoints, with storage stubbed."""
    base = f"/api/v1/workspaces/{workspace_id}"
    body = {"name": name, "size_bytes": size_bytes, "mime_type": mime_type}
    if folder_id is not None:
        body["folder_id"] = str(folder_id)
    init = await client.post(f"{base}/uploads/init", json=body)
    if init.status_code != 201:
        return init
    payload = init.json()
    parts = [
        {"part_number": p["part_number"], "etag": f'"etag-{p["part_number"]}"'}
        for p in payload["parts"]
    ]
    return await client.post(
        f"{base}/uploads/complete",
        json={"version_id": payload["version_id"], "parts": parts},
    )
