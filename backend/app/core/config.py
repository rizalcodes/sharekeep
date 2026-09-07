"""Application configuration.

All environment variables use the FILESHARE_ prefix (Rules.md §2) so this project
cannot collide with the AGENCY_-prefixed variables of the n8n project sharing the VPS.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# VirusTotal submission ceilings (Architecture.md §3). Defaults reflect the public API
# as documented; overridable so a plan change does not require a code change.
VT_DIRECT_UPLOAD_LIMIT_BYTES = 32 * 1024 * 1024
VT_LARGE_UPLOAD_LIMIT_BYTES = 650 * 1024 * 1024

DEFAULT_STORAGE_QUOTA_BYTES = 5 * 1024 * 1024 * 1024  # 5GB, per PRD §8


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

    # --- AWS S3 ---
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_region: str = "ap-southeast-1"
    aws_s3_bucket: str | None = None
    aws_s3_endpoint_url: str | None = None  # set for MinIO/LocalStack in local dev
    # Signed URLs are short-lived and single-purpose (Architecture.md §9).
    signed_download_url_ttl_seconds: int = 300
    # A single multipart part upload may legitimately take longer than a download.
    signed_upload_url_ttl_seconds: int = 3600
    multipart_chunk_size_bytes: int = 8 * 1024 * 1024

    # --- VirusTotal ---
    virustotal_api_key: SecretStr | None = None
    virustotal_direct_upload_limit_bytes: int = VT_DIRECT_UPLOAD_LIMIT_BYTES
    virustotal_max_upload_limit_bytes: int = VT_LARGE_UPLOAD_LIMIT_BYTES
    # Proactive client-side budget (Rules.md §3) — we block on the limiter, never fire-and-429.
    virustotal_requests_per_minute: int = 4
    virustotal_requests_per_day: int = 500

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

    def missing_production_secrets(self) -> list[str]:
        """Names of secrets that must be set before this is safe to run in production."""
        required = {
            "FILESHARE_AWS_ACCESS_KEY_ID": self.aws_access_key_id,
            "FILESHARE_AWS_SECRET_ACCESS_KEY": self.aws_secret_access_key,
            "FILESHARE_AWS_S3_BUCKET": self.aws_s3_bucket,
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
