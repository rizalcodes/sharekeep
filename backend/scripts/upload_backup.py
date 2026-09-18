"""Send an already-encrypted database dump to its own bucket (Architecture.md §7, M8b).

Reads the dump from STDIN so nothing has to be mounted into this container: the host
script encrypts with `age` and pipes the result in. The dump therefore only ever exists
here as bytes in flight, and only in a form this host cannot decrypt — the age private key
is deliberately not on the VPS.

    docker compose exec -T backend python scripts/upload_backup.py NAME < dump.age

The bucket is separate from the file bucket, so a listing of one never sees the other
(§8), and a retention sweep removes copies older than the window.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Run as a path (`python scripts/upload_backup.py`), Python puts THIS directory on the
# path rather than the application root, so `app` would not import. Same line, and the
# same reason, as scripts/bootstrap_storage.py. Found by running it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.services import storage  # noqa: E402

PREFIX = "postgres/"
RETAIN_DAYS = 30


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: upload_backup.py NAME  (dump on stdin)", file=sys.stderr)
        return 2
    name = argv[1]
    bucket = settings.backup_bucket
    if not bucket:
        print("FILESHARE_BACKUP_BUCKET is not set", file=sys.stderr)
        return 2

    payload = sys.stdin.buffer.read()
    if not payload:
        print("nothing on stdin — refusing to upload an empty backup", file=sys.stderr)
        return 1
    # An age file starts with its own header. Checked because uploading an unencrypted
    # dump would put password hashes and audit IPs in a bucket the app's own keys reach.
    if not payload.startswith(b"age-encryption.org/"):
        print("stdin is not an age-encrypted file — refusing to upload it", file=sys.stderr)
        return 1

    client = storage.get_storage_client()
    key = f"{PREFIX}{name}"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="application/octet-stream",
    )
    print(f"uploaded {key} ({len(payload) / 1024:.0f} KiB) to {bucket}")

    cutoff = datetime.now(UTC) - timedelta(days=RETAIN_DAYS)
    removed = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=PREFIX):
        for entry in page.get("Contents", []):
            if entry["LastModified"] < cutoff:
                client.delete_object(Bucket=bucket, Key=entry["Key"])
                removed += 1
    if removed:
        print(f"removed {removed} copies older than {RETAIN_DAYS} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
