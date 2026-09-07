"""Create and verify the Supabase Storage bucket this platform stores file binaries in.

Safe by default: with no flags it only *reports* what is and is not configured.
Pass --apply to actually create/modify the bucket.

    python scripts/bootstrap_storage.py            # check only, changes nothing
    python scripts/bootstrap_storage.py --apply    # create + configure, idempotent

Replaces the earlier bootstrap_s3.py after the 7 Sep 2026 provider change (PRD §10).
The AWS-specific steps it used to perform have no Supabase equivalent and are gone:

  - Block Public Access   -> the bucket's `public: false` flag instead
  - bucket CORS rules     -> not configurable per bucket (see the CORS probe below)
  - default SSE           -> encryption at rest is provider-managed
  - lifecycle rules       -> NO equivalent. Abandoned multipart uploads must be swept
                             by app.tasks.maintenance_tasks.sweep_abandoned_multipart_uploads,
                             which is now the only thing preventing orphaned parts.

What --apply guarantees:
  - the bucket exists and is PRIVATE (no object reachable without a signed URL)
  - a per-bucket file size limit is set

The check also probes two things the docs cannot answer for us:
  - whether the S3-compatible protocol is enabled and the S3 keys work
  - whether a browser could upload directly to a presigned URL, i.e. whether CORS
    permits it (Architecture §2 step 4 depends on this)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/bootstrap_storage.py` from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services import storage  # noqa: E402


def _ok(label: str, detail: str = "") -> None:
    print(f"  [ok]   {label}" + (f" - {detail}" if detail else ""))


def _bad(label: str, detail: str = "") -> None:
    print(f"  [FAIL] {label}" + (f" - {detail}" if detail else ""))


def _warn(label: str, detail: str = "") -> None:
    print(f"  [warn] {label}" + (f" - {detail}" if detail else ""))


def _api() -> httpx.Client:
    key = settings.supabase_service_role_key.get_secret_value()
    return httpx.Client(
        base_url=f"{settings.supabase_url.rstrip('/')}/storage/v1",
        headers={"Authorization": f"Bearer {key}", "apikey": key},
        timeout=20.0,
    )


def _is_not_found(response: httpx.Response) -> bool:
    """Supabase reports a missing bucket as HTTP 400, not 404.

    The real status hides in the body: {"statusCode":"404","code":"NoSuchBucket",...}.
    Checking the transport status alone turns "bucket does not exist yet" into a crash.
    """
    if response.status_code == 404:
        return True
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return body.get("code") == "NoSuchBucket" or str(body.get("statusCode")) == "404"


def get_bucket(client: httpx.Client, bucket: str) -> dict | None:
    response = client.get(f"/bucket/{bucket}")
    if _is_not_found(response):
        return None
    response.raise_for_status()
    return response.json()


def create_bucket(client: httpx.Client, bucket: str) -> None:
    response = client.post(
        "/bucket",
        json={
            "id": bucket,
            "name": bucket,
            "public": False,
            "file_size_limit": settings.max_file_size_bytes,
        },
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"failed to create bucket '{bucket}': {response.status_code} {response.text}"
        )
    print(f"  created private bucket '{bucket}'")


def update_bucket(client: httpx.Client, bucket: str) -> None:
    response = client.put(
        f"/bucket/{bucket}",
        json={
            "public": False,
            "file_size_limit": settings.max_file_size_bytes,
        },
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"failed to update bucket '{bucket}': {response.status_code} {response.text}"
        )
    print("  enforced private access and the per-bucket file size limit")


def check_bucket_configuration(bucket_info: dict) -> bool:
    healthy = True

    if bucket_info.get("public") is False:
        _ok("Bucket is private", "no object reachable without a signed URL")
    else:
        _bad("Bucket is PUBLIC", "every object is world-readable - fix immediately")
        healthy = False

    limit = bucket_info.get("file_size_limit")
    if limit:
        _ok("File size limit", f"{int(limit) / (1024 * 1024):.0f}MB per file")
    else:
        _warn("File size limit", "unset - only the account-wide plan limit applies")

    return healthy


def check_s3_protocol() -> bool:
    """Confirm the S3-compatible endpoint is enabled and the S3 keys actually work."""
    endpoint = settings.storage_s3_endpoint
    if not endpoint:
        _bad("S3 endpoint", "could not derive from FILESHARE_SUPABASE_URL")
        return False
    if not (settings.supabase_s3_access_key_id and settings.supabase_s3_secret_access_key):
        _bad("S3 credentials", "not set - generate them in Storage settings")
        return False

    result = storage.check_connectivity()
    if result["ok"]:
        _ok("S3 protocol", endpoint)
        return True
    _bad(
        "S3 protocol",
        f"{result['detail']} - most often FILESHARE_SUPABASE_REGION "
        f"(currently {settings.supabase_region!r}) not matching the project's real "
        "region, which fails SigV4 signing as a bare 403; otherwise the S3 "
        "connection is not enabled in the dashboard",
    )
    return False


def check_multipart_supported() -> bool:
    """Architecture §2 depends on real multipart upload, so prove it rather than assume."""
    try:
        client = storage.get_storage_client()
        client.list_multipart_uploads(Bucket=settings.supabase_storage_bucket, MaxUploads=1)
    except (ClientError, BotoCoreError, storage.StorageError) as exc:
        _bad("Multipart upload", f"ListMultipartUploads rejected ({type(exc).__name__})")
        return False
    _ok("Multipart upload", "ListMultipartUploads accepted")
    return True


def check_cors() -> bool:
    """Can a browser PUT directly to a presigned URL from the frontend origin?

    Architecture §2 step 4 has the client upload parts straight to storage, never
    through the API. Supabase exposes no per-bucket CORS configuration, so this is
    settled empirically rather than from documentation. A failure here is not fatal
    to M0 - it decides the M3 upload path.
    """
    try:
        url = storage.get_storage_client().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.supabase_storage_bucket,
                "Key": "_cors-probe/probe.bin",
            },
            ExpiresIn=60,
        )
    except (ClientError, BotoCoreError, storage.StorageError) as exc:
        _warn("CORS probe", f"could not presign ({type(exc).__name__})")
        return False

    origin = settings.frontend_origin
    try:
        response = httpx.options(
            url,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        _warn("CORS probe", f"preflight request failed ({type(exc).__name__})")
        return False

    allow_origin = response.headers.get("access-control-allow-origin")
    if allow_origin and allow_origin in ("*", origin):
        _ok("CORS preflight", f"allow-origin: {allow_origin} - browser-direct upload works")
        return True
    _warn(
        "CORS preflight",
        f"no usable allow-origin (status {response.status_code}) - "
        "browser-direct upload may be blocked; see PRD §10 fallback",
    )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and verify the Supabase Storage bucket.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually create/modify the bucket (default is a read-only check)",
    )
    args = parser.parse_args()

    bucket = settings.supabase_storage_bucket
    if not bucket:
        print("FILESHARE_SUPABASE_STORAGE_BUCKET is not set - nothing to check.")
        return 1
    if not settings.supabase_url:
        print("FILESHARE_SUPABASE_URL is not set - nothing to check.")
        return 1
    if not settings.supabase_service_role_key:
        print("FILESHARE_SUPABASE_SERVICE_ROLE_KEY is not set - nothing to check.")
        return 1

    mode = "APPLY" if args.apply else "CHECK (read-only)"
    print(f"Project: {settings.supabase_project_ref}  bucket: {bucket}  mode: {mode}\n")

    with _api() as client:
        info = get_bucket(client, bucket)
        if info is None:
            if not args.apply:
                print(
                    f"  [FAIL] bucket '{bucket}' does not exist. "
                    "Re-run with --apply to create it."
                )
                return 1
            create_bucket(client, bucket)
            info = get_bucket(client, bucket)
        elif args.apply:
            update_bucket(client, bucket)
            info = get_bucket(client, bucket)

        if args.apply:
            print()

        print("Verification:")
        healthy = check_bucket_configuration(info or {})
        s3_ok = check_s3_protocol()
        healthy = healthy and s3_ok
        if s3_ok:
            healthy = check_multipart_supported() and healthy
            # Advisory only: informs the M3 upload path, does not gate the bucket.
            check_cors()

    print()
    if healthy:
        print("Storage bucket is correctly configured.")
        return 0
    print("Storage bucket is NOT correctly configured. Re-run with --apply to fix.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
