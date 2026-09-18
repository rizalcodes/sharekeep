#!/usr/bin/env bash
# Nightly database backup: dump, encrypt, copy off the host (Architecture.md §7, M8b).
#
# Why the database and not the files: the free tier has no room to duplicate objects, and
# the database is what makes them reachable at all. Losing it turns every stored object
# into an orphan — which is exactly what happened to the development stack at M7, where a
# volume wipe left 90 objects nobody could reach.
#
# The dump is encrypted to an age PUBLIC key before it leaves the host. The private key is
# held off the VPS, so neither a compromised VPS nor the Supabase keys the app already
# holds can read a backup — and the dump contains password hashes, refresh-token hashes
# and the audit log's IP addresses.
#
#   ./04-backup.sh            run one backup now
#   ./04-backup.sh --install  install the nightly systemd timer
#
# A backup counts only once it has been restored: deploy/05-restore-drill.sh does that,
# and M8c runs it.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/fileshare}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/fileshare}"
KEEP_LOCAL="${KEEP_LOCAL:-7}"

say() { printf '\n== %s ==\n' "$1"; }

if [ "${1:-}" = "--install" ]; then
  say "Installing the nightly timer"
  sudo tee /etc/systemd/system/fileshare-backup.service >/dev/null <<UNIT_EOF
[Unit]
Description=Nightly encrypted Postgres backup (fileshare)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=${USER}
WorkingDirectory=${APP_DIR}
ExecStart=$(readlink -f "$0")
UNIT_EOF
  sudo tee /etc/systemd/system/fileshare-backup.timer >/dev/null <<'UNIT_EOF'
[Unit]
Description=Run the fileshare backup nightly

[Timer]
# 03:20 UTC, clear of the 03:00 and 04:10 Celery maintenance jobs.
OnCalendar=*-*-* 03:20:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
UNIT_EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now fileshare-backup.timer
  systemctl list-timers fileshare-backup.timer --no-pager
  exit 0
fi

cd "$APP_DIR"
test -f .env || { echo "ERROR: $APP_DIR/.env is missing." >&2; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a
: "${FILESHARE_BACKUP_AGE_RECIPIENT:?FILESHARE_BACKUP_AGE_RECIPIENT is required}"
: "${FILESHARE_POSTGRES_USER:=fileshare}"
: "${FILESHARE_POSTGRES_DB:=fileshare}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
plain="$BACKUP_DIR/fileshare-$stamp.pgc"
sealed="$plain.age"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

say "Dumping the database"
# pg_dump runs INSIDE the postgres container, so its version always matches the server's
# — the usual cause of "server version mismatch" on a host with an older client.
"${COMPOSE[@]}" exec -T postgres \
  pg_dump -Fc -U "$FILESHARE_POSTGRES_USER" "$FILESHARE_POSTGRES_DB" > "$plain"
test -s "$plain" || { echo "ERROR: the dump is empty." >&2; rm -f "$plain"; exit 1; }
echo "dump: $(du -h "$plain" | cut -f1)"

say "Encrypting to the off-host key"
age -r "$FILESHARE_BACKUP_AGE_RECIPIENT" -o "$sealed" "$plain"
# The readable copy never lingers.
shred -u "$plain" 2>/dev/null || rm -f "$plain"
chmod 600 "$sealed"
echo "sealed: $sealed ($(du -h "$sealed" | cut -f1))"

say "Copying it off the host"
"${COMPOSE[@]}" exec -T backend python scripts/upload_backup.py "fileshare-$stamp.pgc.age" < "$sealed"

say "Pruning local copies"
# Keeps the newest KEEP_LOCAL on the host; the off-host copies have their own, longer
# retention (scripts/upload_backup.py).
ls -1t "$BACKUP_DIR"/*.age 2>/dev/null | tail -n +$((KEEP_LOCAL + 1)) | while read -r old; do
  rm -f "$old"
  echo "removed $old"
done
ls -1t "$BACKUP_DIR"/*.age | head -n "$KEEP_LOCAL" | sed 's/^/  keeping /'

say "Done"
