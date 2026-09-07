"""Create and verify the S3 bucket this platform stores file binaries in.

Safe by default: with no flags it only *reports* what is and is not configured.
Pass --apply to actually create/modify the bucket.

    python scripts/bootstrap_s3.py            # check only, changes nothing
    python scripts/bootstrap_s3.py --apply    # create + configure, idempotent

What --apply guarantees (Architecture.md §8):
  - the bucket exists in FILESHARE_AWS_REGION
  - Block Public Access is fully on   (no object is ever public)
  - default SSE-S3 encryption is on
  - CORS allows browser-direct multipart PUT from the frontend origin ONLY,
    exposing ETag so the client can report part ETags back
  - a lifecycle rule aborts incomplete multipart uploads after 7 days, so an
    abandoned browser upload cannot quietly accrue storage cost
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as `python scripts/bootstrap_s3.py` from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import ClientError  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.storage import get_s3_client  # noqa: E402

CORS_RULES = {
    "CORSRules": [
        {
            "AllowedHeaders": ["*"],
            "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
            "AllowedOrigins": [settings.frontend_origin],
            "ExposeHeaders": ["ETag"],
            "MaxAgeSeconds": 3000,
        }
    ]
}

LIFECYCLE_RULES = {
    "Rules": [
        {
            "ID": "abort-incomplete-multipart-uploads",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        }
    ]
}

PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}


def _ok(label: str, detail: str = "") -> None:
    print(f"  [ok]   {label}" + (f" - {detail}" if detail else ""))


def _bad(label: str, detail: str = "") -> None:
    print(f"  [FAIL] {label}" + (f" - {detail}" if detail else ""))


def bucket_exists(client, bucket: str) -> bool:
    try:
        client.head_bucket(Bucket=bucket)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchBucket"):
            return False
        if code == "403":
            raise SystemExit(
                f"bucket '{bucket}' exists but these credentials cannot access it (403)"
            ) from exc
        raise


def create_bucket(client, bucket: str, region: str) -> None:
    kwargs = {"Bucket": bucket}
    # us-east-1 rejects an explicit LocationConstraint; every other region requires it.
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    client.create_bucket(**kwargs)
    print(f"  created bucket '{bucket}' in {region}")


def apply_configuration(client, bucket: str) -> None:
    client.put_public_access_block(
        Bucket=bucket, PublicAccessBlockConfiguration=PUBLIC_ACCESS_BLOCK
    )
    print("  applied Block Public Access (all four settings on)")

    client.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                    "BucketKeyEnabled": True,
                }
            ]
        },
    )
    print("  applied default SSE-S3 encryption")

    client.put_bucket_cors(Bucket=bucket, CORSConfiguration=CORS_RULES)
    print(f"  applied CORS (origin: {settings.frontend_origin})")

    client.put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration=LIFECYCLE_RULES
    )
    print("  applied lifecycle rule: abort incomplete multipart uploads after 7 days")


def check_configuration(client, bucket: str) -> bool:
    healthy = True

    try:
        block = client.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        if all(block.get(k) for k in PUBLIC_ACCESS_BLOCK):
            _ok("Block Public Access", "all four settings on")
        else:
            _bad("Block Public Access", json.dumps(block))
            healthy = False
    except ClientError:
        _bad("Block Public Access", "not configured")
        healthy = False

    try:
        client.get_bucket_encryption(Bucket=bucket)
        _ok("Default encryption")
    except ClientError:
        _bad("Default encryption", "not configured")
        healthy = False

    try:
        cors = client.get_bucket_cors(Bucket=bucket)["CORSRules"]
        origins = {o for rule in cors for o in rule.get("AllowedOrigins", [])}
        exposed = {h for rule in cors for h in rule.get("ExposeHeaders", [])}
        if settings.frontend_origin in origins and "ETag" in exposed:
            _ok("CORS", f"origins={sorted(origins)}")
        else:
            _bad("CORS", f"origins={sorted(origins)} exposed={sorted(exposed)}")
            healthy = False
    except ClientError:
        _bad("CORS", "not configured - browser-direct multipart upload will fail")
        healthy = False

    try:
        rules = client.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        if any("AbortIncompleteMultipartUpload" in rule for rule in rules):
            _ok("Lifecycle", "incomplete multipart uploads are aborted")
        else:
            _bad("Lifecycle", "no abort-incomplete-multipart rule")
            healthy = False
    except ClientError:
        _bad("Lifecycle", "not configured")
        healthy = False

    return healthy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually create/modify the bucket (default is a read-only check)",
    )
    args = parser.parse_args()

    bucket = settings.aws_s3_bucket
    if not bucket:
        print("FILESHARE_AWS_S3_BUCKET is not set - nothing to check.")
        return 1
    if not (settings.aws_access_key_id and settings.aws_secret_access_key):
        print("AWS credentials are not set (FILESHARE_AWS_ACCESS_KEY_ID / _SECRET_ACCESS_KEY).")
        return 1

    client = get_s3_client()
    mode = "APPLY" if args.apply else "CHECK (read-only)"
    print(f"Bucket: {bucket}  region: {settings.aws_region}  mode: {mode}\n")

    exists = bucket_exists(client, bucket)
    if not exists:
        if not args.apply:
            print(f"  [FAIL] bucket '{bucket}' does not exist. Re-run with --apply to create it.")
            return 1
        create_bucket(client, bucket, settings.aws_region)

    if args.apply:
        apply_configuration(client, bucket)
        print()

    print("Verification:")
    healthy = check_configuration(client, bucket)
    print()
    if healthy:
        print("Bucket is correctly configured.")
        return 0
    print("Bucket is NOT correctly configured. Re-run with --apply to fix.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
