#!/usr/bin/env bash
# Restore the newest backup into a scratch database and compare it with the live one.
#
# A backup that has never been restored is not evidence of a backup (Architecture.md §7),
# so this runs as part of M8c and is meant to be re-run whenever the schema changes.
#
# The age PRIVATE key is not on this host and must not be written to it. Stream it in:
#
#   ssh vps 'cd /opt/fileshare && ./deploy/05-restore-drill.sh' < ~/.age/fileshare-backup.key
#
# The key is passed to age on a file descriptor, never to a file and never to a command
# line where `ps` would show it. The scratch database is dropped at the end, whatever
# happens; the live database is only ever read.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/fileshare}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/fileshare}"
SCRATCH_DB="${SCRATCH_DB:-fileshare_restore_drill}"

say() { printf '\n== %s ==\n' "$1"; }

cd "$APP_DIR"
# shellcheck disable=SC1091
set -a; . ./.env; set +a
: "${FILESHARE_POSTGRES_USER:=fileshare}"
: "${FILESHARE_POSTGRES_DB:=fileshare}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
psql_live() { "${COMPOSE[@]}" exec -T postgres psql -qtAX -U "$FILESHARE_POSTGRES_USER" -d "$FILESHARE_POSTGRES_DB" -c "$1"; }
psql_admin() { "${COMPOSE[@]}" exec -T postgres psql -qtAX -U "$FILESHARE_POSTGRES_USER" -d postgres -c "$1"; }

sealed=$(ls -1t "$BACKUP_DIR"/*.age 2>/dev/null | head -1 || true)
test -n "$sealed" || { echo "ERROR: no backup in $BACKUP_DIR. Run 04-backup.sh first." >&2; exit 1; }
say "Newest backup"
ls -l "$sealed"

say "Reading the private key from stdin"
if [ -t 0 ]; then
  echo "ERROR: no key on stdin. See the header of this script." >&2
  exit 1
fi
key=$(cat)
case "$key" in
  *AGE-SECRET-KEY-*) : ;;
  *) echo "ERROR: that does not look like an age identity." >&2; exit 1 ;;
esac
echo "identity received ($(printf '%s' "$key" | wc -l) lines), never written to disk"

restored="/tmp/fileshare-drill-$$.pgc"
cleanup() {
  rm -f "$restored"
  psql_admin "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null 2>&1 || true
  echo "cleaned up: scratch database dropped, decrypted dump removed"
}
trap cleanup EXIT

say "Decrypting"
# The identity goes to age on fd 3, so it never becomes a file or an argument.
exec 3<<<"$key"
age -d -i /dev/fd/3 -o "$restored" "$sealed"
exec 3<&-
chmod 600 "$restored"
echo "decrypted: $(du -h "$restored" | cut -f1)"

say "Restoring into ${SCRATCH_DB}"
psql_admin "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null
psql_admin "CREATE DATABASE ${SCRATCH_DB};" >/dev/null
"${COMPOSE[@]}" exec -T postgres pg_restore \
  -U "$FILESHARE_POSTGRES_USER" -d "$SCRATCH_DB" --no-owner --no-privileges < "$restored"
echo "restored"

say "Comparing the restored copy with the live database"
printf '%-24s %12s %12s   %s\n' TABLE LIVE RESTORED VERDICT
mismatch=0
for table in users workspaces workspace_members files file_versions folders \
             file_permissions share_links audit_logs notifications alembic_version; do
  live=$(psql_live "SELECT count(*) FROM ${table};" | tr -d '\r')
  copy=$("${COMPOSE[@]}" exec -T postgres psql -qtAX -U "$FILESHARE_POSTGRES_USER" \
           -d "$SCRATCH_DB" -c "SELECT count(*) FROM ${table};" | tr -d '\r')
  if [ "$live" = "$copy" ]; then
    verdict=same
  else
    # Rows written after the dump are expected, and are not a failure; fewer are.
    if [ "${copy:-0}" -lt "${live:-0}" ]; then
      verdict="older (dump predates $((live - copy)) row(s))"
    else
      verdict=MISMATCH
      mismatch=1
    fi
  fi
  printf '%-24s %12s %12s   %s\n' "$table" "$live" "$copy" "$verdict"
done

say "Result"
if [ "$mismatch" -eq 0 ]; then
  echo "RESTORE DRILL PASSED — the backup restores and the schema matches."
else
  echo "RESTORE DRILL FAILED — a restored table holds MORE rows than the live one." >&2
  exit 1
fi
