"""Application configuration.

All environment variables use the FILESHARE_ prefix (Rules.md §2) so this project
cannot collide with the AGENCY_-prefixed variables of the n8n project sharing the VPS.
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# VirusTotal submission ceilings (Architecture.md §3). Defaults reflect the public API
# as documented; overridable so a plan change does not require a code change.
VT_DIRECT_UPLOAD_LIMIT_BYTES = 32 * 1024 * 1024
VT_LARGE_UPLOAD_LIMIT_BYTES = 650 * 1024 * 1024

# Supabase free plan caps a single file at 50MB globally across all buckets.
# Overridable — this is a plan limit, not a protocol limit.
SUPABASE_FREE_PLAN_FILE_LIMIT_BYTES = 50 * 1024 * 1024

# 500MB, per the 7 Sep 2026 storage-provider change (PRD §10). Sized to fit inside
# the Supabase free plan's 1GB account total so quota enforcement is genuinely
# exercised rather than pre-empted by the provider. Returns to 5GB on AWS S3.
DEFAULT_STORAGE_QUOTA_BYTES = 500 * 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FILESHARE_",
        # The repo-root .env is the single source for local development; ../.env is
        # what that resolves to when running from backend/. In Docker the variables
        # are injected directly and no file is read.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Environment ---
    environment: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"

    # --- Database ---
    database_url: str = "postgresql+asyncpg://fileshare:fileshare@localhost:5432/fileshare"

    # --- Redis / Celery ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_db: int = 0
    celery_result_db: int = 1

    # --- Auth ---
    jwt_secret: SecretStr = SecretStr("dev-only-insecure-secret-change-me")
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 14 * 24 * 60 * 60
    # JWTs ride in httpOnly cookies (Architecture.md §9), never localStorage.
    cookie_secure: bool = True
    cookie_domain: str | None = None

    # --- Frontend / CORS ---
    frontend_origin: str = "http://localhost:3000"

    # --- Supabase Storage ---
    # Temporary provider (PRD §10). AWS S3 remains the long-term target, so this is
    # deliberately driven through Supabase's S3-compatible endpoint: boto3, SigV4
    # presigning, and real multipart upload all work unchanged, which keeps the
    # eventual move back to AWS close to a credentials swap.
    supabase_url: str | None = None  # https://<project_ref>.supabase.co
    supabase_service_role_key: SecretStr | None = None  # bypasses RLS — server only
    supabase_storage_bucket: str | None = None
    # Must match the project's real region or SigV4 signing fails with a bare 403.
    # us-east-1 is the safe default: Supabase accepts it as a generic fallback.
    supabase_region: str = "us-east-1"
    # S3 protocol credentials, generated in the Supabase dashboard under
    # Storage settings. Distinct from the service role key: these sign S3 requests.
    supabase_s3_access_key_id: SecretStr | None = None
    supabase_s3_secret_access_key: SecretStr | None = None
    # Overrides the endpoint derived from supabase_url; also the hook for a local
    # MinIO/LocalStack or self-hosted Supabase.
    storage_endpoint_url: str | None = None
    # Provider-side per-file ceiling. Enforced at upload-init so the user gets a clean
    # rejection instead of a provider error mid-upload.
    max_file_size_bytes: int = SUPABASE_FREE_PLAN_FILE_LIMIT_BYTES

    # Signed URLs are short-lived and single-purpose (Architecture.md §9).
    signed_download_url_ttl_seconds: int = 300
    # A single multipart part upload may legitimately take longer than a download.
    signed_upload_url_ttl_seconds: int = 3600
    multipart_chunk_size_bytes: int = 8 * 1024 * 1024

    # Off, uploads land as scan_skipped and are downloadable immediately. On, they land
    # as pending and are NOT downloadable until a scan resolves (Architecture.md §3).
    # This is a behavioural switch, not just a feature toggle.
    scanning_enabled: bool = False

    # --- VirusTotal ---
    virustotal_api_key: SecretStr | None = None
    virustotal_api_base_url: str = "https://www.virustotal.com/api/v3"
    virustotal_direct_upload_limit_bytes: int = VT_DIRECT_UPLOAD_LIMIT_BYTES
    virustotal_max_upload_limit_bytes: int = VT_LARGE_UPLOAD_LIMIT_BYTES
    # Proactive client-side budget (Architecture.md §3a). Reserve-or-requeue: a worker
    # never blocks waiting for budget, because the daily window can be hours away.
    virustotal_requests_per_minute: int = 4
    virustotal_requests_per_day: int = 500

    # Whether file CONTENT may be uploaded to VirusTotal when a hash lookup misses.
    # Default off: free-tier submissions are shared with the VirusTotal community, so
    # enabling this publishes a user's private file to a public analysis pool. Off means
    # unknown files are marked scan_skipped with that reason (Architecture.md §3b).
    virustotal_submit_unknown_files: bool = False
    # How many engines must call a file malicious before it is flagged. 1 means a single
    # engine is enough to withhold the file. That will sometimes be a false positive,
    # and the cost is real — a flagged file is unreachable even by its owner — but the
    # remedy is the accountable admin override (Architecture.md §3d), not a laxer
    # default that serves malware because only one scanner noticed. `suspicious` counts
    # are recorded and never flag on their own: a hedge is not a conviction.
    virustotal_malicious_threshold: int = 1
    # Only consulted when submission is enabled: how long to wait for an analysis verdict.
    virustotal_analysis_poll_attempts: int = 10
    virustotal_analysis_poll_interval_seconds: int = 15

    # --- Scan bookkeeping ---
    # A cached verdict older than this is re-looked-up: a file clean today may be
    # flagged next month (Architecture.md §3c).
    scan_cache_ttl_days: int = 30
    # A version left in pending/scanning longer than this is re-driven by the beat task,
    # which is what recovers work lost to a killed worker.
    scan_stuck_after_minutes: int = 30
    # Streaming chunk for checksum computation. The worker must never hold a whole
    # object in memory.
    checksum_chunk_size_bytes: int = 1024 * 1024

    # --- Resend ---
    resend_api_key: SecretStr | None = None
    resend_from_email: str = "noreply@example.com"

    # --- Quotas ---
    default_storage_quota_bytes: int = DEFAULT_STORAGE_QUOTA_BYTES
    storage_quota_warning_ratio: float = 0.9

    # --- Retention ---
    soft_delete_retention_days: int = 30
    abandoned_multipart_ttl_hours: int = 24

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def celery_broker_url(self) -> str:
        return _swap_redis_db(self.redis_url, self.celery_broker_db)

    @property
    def celery_result_backend(self) -> str:
        return _swap_redis_db(self.redis_url, self.celery_result_db)

    @property
    def supabase_project_ref(self) -> str | None:
        """Extract the project ref from the Supabase URL.

        `https://abcdefgh.supabase.co` -> `abcdefgh`. The ref is the subdomain, and it
        is what the S3-compatible storage hostname is built from.
        """
        if not self.supabase_url:
            return None
        host = urlparse(self.supabase_url).hostname
        if not host:
            return None
        ref = host.split(".", 1)[0]
        return ref or None

    @property
    def storage_s3_endpoint(self) -> str | None:
        """The S3-compatible endpoint boto3 talks to.

        Supabase serves it from the dedicated storage hostname rather than the main
        project URL: https://<project_ref>.storage.supabase.co/storage/v1/s3
        """
        if self.storage_endpoint_url:
            return self.storage_endpoint_url
        ref = self.supabase_project_ref
        if not ref:
            return None
        return f"https://{ref}.storage.supabase.co/storage/v1/s3"

    def missing_production_secrets(self) -> list[str]:
        """Names of secrets that must be set before this is safe to run in production."""
        required = {
            "FILESHARE_SUPABASE_URL": self.supabase_url,
            "FILESHARE_SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key,
            "FILESHARE_SUPABASE_STORAGE_BUCKET": self.supabase_storage_bucket,
            "FILESHARE_SUPABASE_S3_ACCESS_KEY_ID": self.supabase_s3_access_key_id,
            "FILESHARE_SUPABASE_S3_SECRET_ACCESS_KEY": self.supabase_s3_secret_access_key,
            "FILESHARE_VIRUSTOTAL_API_KEY": self.virustotal_api_key,
            "FILESHARE_RESEND_API_KEY": self.resend_api_key,
        }
        missing = [name for name, value in required.items() if not value]
        if self.jwt_secret.get_secret_value().startswith("dev-only"):
            missing.append("FILESHARE_JWT_SECRET")
        return missing


def _swap_redis_db(url: str, db_index: int) -> str:
    """Return `url` pointed at a different Redis logical database."""
    base, _, _ = url.rpartition("/")
    if not base or "//" == base[-2:]:
        return f"{url.rstrip('/')}/{db_index}"
    return f"{base}/{db_index}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
