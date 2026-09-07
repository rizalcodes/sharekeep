from __future__ import annotations

import os

import pytest

# Keep tests off any real infrastructure.
os.environ.setdefault("FILESHARE_ENVIRONMENT", "local")
os.environ.setdefault("FILESHARE_SUPABASE_STORAGE_BUCKET", "fileshare-test-bucket")
os.environ.setdefault("FILESHARE_SUPABASE_URL", "https://testproject.supabase.co")
os.environ.setdefault("FILESHARE_FRONTEND_ORIGIN", "http://localhost:3000")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
