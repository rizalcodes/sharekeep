"""Object storage access.

**Provider: Supabase Storage, via its S3-compatible endpoint** (PRD §10, 7 Sep 2026).
AWS S3 remains the long-term target; Supabase is a sequencing decision, not a scope
change. Driving it through the S3 protocol rather than Supabase's native REST/TUS
paths is deliberate — presigned multipart upload works the same way on both, so
returning to AWS is a credentials-and-endpoint change rather than a rewrite.

Everything below the client factory is provider-agnostic on purpose. Key structure is
per-workspace (PRD §8, Architecture.md §8):

    {workspace_id}/{file_id}/{version_id}/{filename}

Defense in depth: workspace isolation is visible in the key itself, and deleting a
workspace becomes a single prefix delete. No client ever receives a raw storage URL or
a credential — only short-lived, single-purpose signed URLs (Rules.md §3).

Where Supabase differs from AWS S3, and what it means here:
  - no lifecycle rules, so abandoned multipart uploads must be swept by the Celery
    beat task in app/tasks/maintenance_tasks.py — that sweep is now the ONLY
    protection against orphaned parts, not a second line of defense
  - no bucket policy / Block Public Access API; privacy is the bucket's `public` flag
  - no server-side encryption configuration; encryption at rest is provider-managed
  - no per-bucket CORS configuration
"""

from __future__ import annotations

import logging
import posixpath
import re
import unicodedata
import uuid
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

PROVIDER = "supabase-storage"

# S3 protocol rule: every part but the last must be at least 5MB. Not a preference.
MIN_PART_SIZE_BYTES = 5 * 1024 * 1024

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class StorageError(RuntimeError):
    """Raised when object storage is unreachable or rejects an operation."""


@lru_cache
def get_storage_client() -> BaseClient:
    """Build the shared boto3 client pointed at Supabase's S3-compatible endpoint.

    SigV4 is pinned because Supabase's S3 gateway presigns with Signature Version 4
    only. Path-style addressing is required — the endpoint is a single fixed host and
    does not resolve virtual-hosted bucket subdomains the way AWS does.
    """
    if not settings.supabase_storage_bucket:
        raise StorageError("FILESHARE_SUPABASE_STORAGE_BUCKET is not configured")

    endpoint = settings.storage_s3_endpoint
    if not endpoint:
        raise StorageError(
            "storage endpoint is not configured — set FILESHARE_SUPABASE_URL "
            "(or FILESHARE_STORAGE_ENDPOINT_URL)"
        )
    if not (settings.supabase_s3_access_key_id and settings.supabase_s3_secret_access_key):
        raise StorageError(
            "S3 protocol credentials are not configured — generate them in the "
            "Supabase dashboard under Storage settings"
        )

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.supabase_region,
        aws_access_key_id=settings.supabase_s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.supabase_s3_secret_access_key.get_secret_value(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


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


def exceeds_provider_file_limit(size_bytes: int) -> bool:
    """Whether the provider will refuse this file outright.

    Checked at upload-init so an oversized file is rejected cleanly up front rather
    than failing partway through a multipart upload.
    """
    return size_bytes > settings.max_file_size_bytes


def generate_download_url(storage_key: str, *, download_filename: str | None = None) -> str:
    """Short-lived, download-only signed URL (Architecture.md §9)."""
    params: dict[str, Any] = {
        "Bucket": settings.supabase_storage_bucket,
        "Key": storage_key,
    }
    if download_filename:
        safe = sanitize_filename(download_filename)
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    try:
        return get_storage_client().generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=settings.signed_download_url_ttl_seconds,
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("failed to generate download URL") from exc


def check_connectivity() -> dict[str, Any]:
    """Readiness probe: can we actually reach the configured bucket?

    Deliberately a live HEAD rather than a cached flag — the point of a readiness
    check is to test the real dependency.
    """
    try:
        client = get_storage_client()
    except StorageError as exc:
        # Distinguish "you haven't configured this yet" from "it's broken" — the two
        # need completely different responses from whoever reads the probe.
        return {"ok": False, "detail": f"not configured: {exc}"}
    try:
        client.head_bucket(Bucket=settings.supabase_storage_bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        return {"ok": False, "detail": f"bucket unreachable ({code})"}
    except BotoCoreError as exc:
        return {"ok": False, "detail": f"bucket unreachable ({type(exc).__name__})"}
    return {
        "ok": True,
        "provider": PROVIDER,
        "bucket": settings.supabase_storage_bucket,
        "region": settings.supabase_region,
    }


# --- Multipart upload (Architecture.md §2, added in M2) ---------------------------
#
# The client uploads parts straight to storage with presigned URLs; the backend never
# sees the bytes (Rules.md §3). Verified working against the live bucket during M0.

def create_multipart_upload(storage_key: str, content_type: str) -> str:
    """Open a multipart session and return the provider's upload id."""
    try:
        response = get_storage_client().create_multipart_upload(
            Bucket=settings.supabase_storage_bucket,
            Key=storage_key,
            ContentType=content_type,
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("failed to start upload") from exc
    return response["UploadId"]


def plan_parts(size_bytes: int) -> list[tuple[int, int]]:
    """Split a file into (part_number, part_size).

    Every part except the last must be at least 5MB — an S3 protocol rule, and the
    reason the chunk size is not simply a UI preference.
    """
    chunk = max(settings.multipart_chunk_size_bytes, MIN_PART_SIZE_BYTES)
    if size_bytes <= 0:
        return [(1, 0)]
    parts: list[tuple[int, int]] = []
    remaining = size_bytes
    number = 1
    while remaining > 0:
        this_part = min(chunk, remaining)
        parts.append((number, this_part))
        remaining -= this_part
        number += 1
    return parts


def presign_part(storage_key: str, upload_id: str, part_number: int) -> str:
    try:
        return get_storage_client().generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": settings.supabase_storage_bucket,
                "Key": storage_key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=settings.signed_upload_url_ttl_seconds,
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("failed to sign upload part") from exc


def complete_multipart_upload(
    storage_key: str, upload_id: str, parts: list[dict[str, Any]]
) -> None:
    """Finalise the upload. `parts` is [{"PartNumber": n, "ETag": "..."}]."""
    try:
        get_storage_client().complete_multipart_upload(
            Bucket=settings.supabase_storage_bucket,
            Key=storage_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": sorted(parts, key=lambda p: p["PartNumber"])},
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("failed to finalise upload") from exc


def abort_multipart_upload(storage_key: str, upload_id: str) -> None:
    """Abort an abandoned session.

    Supabase Storage has no lifecycle rules, so the Celery sweep calling this is the
    ONLY thing preventing orphaned parts from accumulating (Architecture.md §2).
    """
    try:
        get_storage_client().abort_multipart_upload(
            Bucket=settings.supabase_storage_bucket,
            Key=storage_key,
            UploadId=upload_id,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning("failed to abort upload for %s: %s", storage_key, type(exc).__name__)


def delete_object(storage_key: str) -> None:
    try:
        get_storage_client().delete_object(
            Bucket=settings.supabase_storage_bucket, Key=storage_key
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning("failed to delete %s: %s", storage_key, type(exc).__name__)


# --- Reading objects back (Architecture.md §3, added in M4) -----------------------
#
# Upload never routes bytes through the backend, but scanning has to: the checksum is
# computed over the stored object, and VirusTotal submission needs the content. Both
# stream in fixed-size chunks. A worker holding a whole 50MB object in memory alongside
# a second concurrent task is avoidable, and the ceiling rises again on S3.


def stream_object(storage_key: str, *, chunk_size: int | None = None) -> Iterator[bytes]:
    """Yield an object's bytes in chunks, never materialising the whole thing."""
    size = chunk_size or settings.checksum_chunk_size_bytes
    response = get_storage_client().get_object(
        Bucket=settings.supabase_storage_bucket, Key=storage_key
    )
    body = response["Body"]
    try:
        yield from body.iter_chunks(chunk_size=size)
    finally:
        body.close()


def download_to_path(storage_key: str, path: str, *, chunk_size: int | None = None) -> int:
    """Copy an object to a local path, returning the byte count actually written.

    The count is returned rather than assumed from metadata so a caller can compare it
    against what the database believes and notice a truncated read instead of scanning
    a partial file and calling the result a verdict.
    """
    written = 0
    with open(path, "wb") as handle:
        for chunk in stream_object(storage_key, chunk_size=chunk_size):
            handle.write(chunk)
            written += len(chunk)
    return written


def list_parts(storage_key: str, upload_id: str) -> list[dict[str, Any]]:
    """Which parts already landed, with their ETags.

    This is what makes resume possible after a page reload: the client's record of
    completed parts dies with the page, but the provider still has it.

    **Only part numbers and ETags are returned.** Supabase's ListParts omits the `Size`
    field that AWS includes — verified against the live bucket, after an earlier version
    of this function assumed AWS's shape and raised KeyError on the first real call.
    Part sizes come from our own upload plan instead, which is the authoritative source
    for them anyway.
    """
    try:
        response = get_storage_client().list_parts(
            Bucket=settings.supabase_storage_bucket,
            Key=storage_key,
            UploadId=upload_id,
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("failed to list uploaded parts") from exc
    return [
        {"part_number": p["PartNumber"], "etag": p["ETag"]}
        for p in response.get("Parts", [])
    ]
