"""S3 object storage access.

Key structure is per-workspace (PRD §8, Architecture.md §8):

    {workspace_id}/{file_id}/{version_id}/{filename}

Defense in depth: workspace isolation is visible in the key itself, and deleting a
workspace becomes a single prefix delete. No client ever receives a raw S3 URL or a
credential — only short-lived, single-purpose signed URLs (Rules.md §3).
"""

from __future__ import annotations

import logging
import posixpath
import re
import unicodedata
import uuid
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class StorageError(RuntimeError):
    """Raised when object storage is unreachable or rejects an operation."""


@lru_cache
def get_s3_client() -> BaseClient:
    """Build the shared boto3 S3 client.

    SigV4 is pinned because presigned URLs for buckets created after 2014 in newer
    regions are invalid under SigV2, and the failure mode is a confusing 403.
    """
    if not settings.aws_s3_bucket:
        raise StorageError("FILESHARE_AWS_S3_BUCKET is not configured")

    kwargs: dict[str, Any] = {
        "region_name": settings.aws_region,
        "config": Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    }
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id.get_secret_value()
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()
    if settings.aws_s3_endpoint_url:
        # MinIO / LocalStack for local development.
        kwargs["endpoint_url"] = settings.aws_s3_endpoint_url
        kwargs["config"] = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        )
    return boto3.client("s3", **kwargs)


def sanitize_filename(filename: str) -> str:
    """Reduce a user-supplied filename to a safe trailing key segment.

    The filename is cosmetic in the key — the UUID segments provide uniqueness — so it
    is safe to be aggressive here. Path separators and traversal sequences must never
    survive into an object key.
    """
    name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    name = posixpath.basename(name.replace("\\", "/")).strip()
    name = _UNSAFE_KEY_CHARS.sub("_", name).strip("._-")
    if not name:
        name = "file"
    return name[:200]


def build_storage_key(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    version_id: uuid.UUID,
    filename: str,
) -> str:
    return f"{workspace_id}/{file_id}/{version_id}/{sanitize_filename(filename)}"


def workspace_prefix(workspace_id: uuid.UUID) -> str:
    return f"{workspace_id}/"


def generate_download_url(storage_key: str, *, download_filename: str | None = None) -> str:
    """Short-lived, download-only signed URL (Architecture.md §9)."""
    params: dict[str, Any] = {"Bucket": settings.aws_s3_bucket, "Key": storage_key}
    if download_filename:
        safe = sanitize_filename(download_filename)
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    try:
        return get_s3_client().generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=settings.signed_download_url_ttl_seconds,
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("failed to generate download URL") from exc


def check_connectivity() -> dict[str, Any]:
    """Readiness probe: can we actually reach the configured bucket?

    Deliberately a HEAD on the bucket rather than a cached flag — the point of a
    readiness check is to test the real dependency.
    """
    if not settings.aws_s3_bucket:
        return {"ok": False, "detail": "bucket not configured"}
    try:
        get_s3_client().head_bucket(Bucket=settings.aws_s3_bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        return {"ok": False, "detail": f"bucket unreachable ({code})"}
    except (BotoCoreError, StorageError) as exc:
        return {"ok": False, "detail": f"bucket unreachable ({type(exc).__name__})"}
    return {"ok": True, "bucket": settings.aws_s3_bucket, "region": settings.aws_region}
