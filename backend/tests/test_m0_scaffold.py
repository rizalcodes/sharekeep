"""M0 scaffold tests.

These cover the pieces that are real at M0: configuration wiring, log redaction, and
storage key construction. Each security-sensitive helper gets an adversarial case as
well as a happy path (Rules.md §6).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import redact
from app.main import app
from app.services import storage

# --- Configuration ---------------------------------------------------------------

def test_env_prefix_is_fileshare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rules.md §2: an AGENCY_-prefixed variable from the n8n project must not leak in."""
    monkeypatch.setenv("FILESHARE_SUPABASE_REGION", "eu-west-1")
    monkeypatch.setenv("AGENCY_SUPABASE_REGION", "us-east-1")
    assert Settings().supabase_region == "eu-west-1"


def test_celery_urls_use_separate_redis_databases() -> None:
    s = Settings(redis_url="redis://localhost:6379/0")
    assert s.celery_broker_url.endswith("/0")
    assert s.celery_result_backend.endswith("/1")
    assert s.celery_broker_url != s.celery_result_backend


def test_production_startup_requires_real_secrets() -> None:
    # Every input is passed explicitly: a developer's real .env must not be able to
    # make this pass by accident.
    missing = Settings(
        environment="production",
        jwt_secret=SecretStr("dev-only-insecure-secret-change-me"),
        supabase_url=None,
        supabase_service_role_key=None,
        supabase_storage_bucket=None,
        supabase_s3_access_key_id=None,
        supabase_s3_secret_access_key=None,
        virustotal_api_key=None,
        resend_api_key=None,
    ).missing_production_secrets()
    assert "FILESHARE_JWT_SECRET" in missing
    assert "FILESHARE_SUPABASE_SERVICE_ROLE_KEY" in missing
    assert "FILESHARE_SUPABASE_S3_ACCESS_KEY_ID" in missing


def test_a_fully_configured_production_setup_reports_nothing_missing() -> None:
    missing = Settings(
        environment="production",
        jwt_secret=SecretStr("a-real-secret"),
        supabase_url="https://abcdefghijkl.supabase.co",
        supabase_service_role_key=SecretStr("service-role"),
        supabase_storage_bucket="fileshare-files",
        supabase_s3_access_key_id=SecretStr("key"),
        supabase_s3_secret_access_key=SecretStr("secret"),
        virustotal_api_key=SecretStr("vt"),
        resend_api_key=SecretStr("resend"),
    ).missing_production_secrets()
    assert missing == []


def test_signed_url_ttl_is_short() -> None:
    """Architecture.md §9: download URLs are minutes, not hours."""
    assert Settings().signed_download_url_ttl_seconds <= 900


# --- Storage provider wiring (Supabase, per PRD §10) ------------------------------

def test_s3_endpoint_is_derived_from_the_supabase_url() -> None:
    """Supabase serves the S3 API from the storage hostname, not the project URL."""
    s = Settings(supabase_url="https://abcdefghijkl.supabase.co")
    assert s.supabase_project_ref == "abcdefghijkl"
    assert (
        s.storage_s3_endpoint
        == "https://abcdefghijkl.storage.supabase.co/storage/v1/s3"
    )


def test_explicit_endpoint_overrides_derivation() -> None:
    """Self-hosted Supabase and local MinIO both need to bypass the derived host."""
    s = Settings(
        supabase_url="https://abcdefghijkl.supabase.co",
        storage_endpoint_url="http://localhost:54321/storage/v1/s3",
    )
    assert s.storage_s3_endpoint == "http://localhost:54321/storage/v1/s3"


def test_endpoint_is_none_without_a_supabase_url() -> None:
    assert Settings(supabase_url=None).storage_s3_endpoint is None


def test_default_quota_fits_inside_the_provider_account_limit() -> None:
    """A default workspace quota larger than the whole account makes the quota check
    unreachable — the provider would reject the upload first. PRD §10."""
    supabase_free_plan_total = 1024 * 1024 * 1024
    assert Settings().default_storage_quota_bytes <= supabase_free_plan_total


def test_provider_file_limit_is_enforced_before_upload() -> None:
    """Oversized files must be rejected at upload-init, not mid-multipart."""
    limit = Settings().max_file_size_bytes
    assert not storage.exceeds_provider_file_limit(limit)
    assert storage.exceeds_provider_file_limit(limit + 1)


# --- Log redaction ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "using key AKIAIOSFODNN7EXAMPLE for upload",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def",
        'FILESHARE_VIRUSTOTAL_API_KEY="a1b2c3d4e5f6"',
        "connecting to postgresql+asyncpg://fileshare:hunter2@db:5432/fileshare",
    ],
)
def test_secrets_never_survive_redaction(raw: str) -> None:
    cleaned = redact(raw)
    for secret in ("AKIAIOSFODNN7EXAMPLE", "eyJhbGciOiJIUzI1NiIs", "a1b2c3d4e5f6", "hunter2"):
        assert secret not in cleaned, f"{secret!r} leaked through redaction: {cleaned}"


def test_secret_bearing_url_paths_are_redacted() -> None:
    """Found during M1 manual validation: raw invitation tokens were reaching the
    uvicorn access log via the URL path, which is workspace access for anyone who can
    read the log. The endpoint no longer does this; the filter is the second layer."""
    token = "3GzJuk9_WcrOp3Wxfb7rgaj7gOMoWYZ7ouyb7PsZ3mrXCeISwcLxAGI9oV-DHaQw"
    line = f'GET /api/v1/invitations/{token} HTTP/1.1 200'
    cleaned = redact(line)
    assert token not in cleaned
    assert "[REDACTED_TOKEN]" in cleaned


def test_redaction_leaves_ordinary_messages_alone() -> None:
    message = "uploaded version 3 of report.pdf to workspace 4f9a"
    assert redact(message) == message


# --- Storage keys ----------------------------------------------------------------

def test_storage_key_uses_per_workspace_prefix() -> None:
    workspace_id, file_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = storage.build_storage_key(workspace_id, file_id, version_id, "quarterly report.pdf")
    assert key == f"{workspace_id}/{file_id}/{version_id}/quarterly_report.pdf"
    assert key.startswith(storage.workspace_prefix(workspace_id))


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/absolute/path/secret.key",
        "....//....//escape.txt",
        "nul\x00byte.txt",
    ],
)
def test_filename_sanitization_blocks_traversal(hostile: str) -> None:
    """A filename must never be able to steer the object key out of its prefix."""
    safe = storage.sanitize_filename(hostile)
    assert "/" not in safe
    assert "\\" not in safe
    assert ".." not in safe
    assert "\x00" not in safe

    workspace_id = uuid.uuid4()
    key = storage.build_storage_key(workspace_id, uuid.uuid4(), uuid.uuid4(), hostile)
    assert key.startswith(f"{workspace_id}/")
    assert key.count("/") == 3, f"key escaped its prefix depth: {key}"


def test_filename_sanitization_never_returns_empty() -> None:
    assert storage.sanitize_filename("...") == "file"
    assert storage.sanitize_filename("") == "file"


# --- API -------------------------------------------------------------------------

def test_health_endpoint_is_up() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_security_headers_are_set() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
