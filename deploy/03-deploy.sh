#!/usr/bin/env bash
# Build, migrate and start the stack (Architecture.md §7, M8b).
#
# Expects the repository at /opt/fileshare with a filled-in .env beside it, and the proxy
# already running (deploy/proxy). Safe to re-run: this is also the update path.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/fileshare}"
cd "$APP_DIR"

say() { printf '\n== %s ==\n' "$1"; }

say "What is about to be deployed"
git --no-pager log --oneline -1
echo "branch: $(git rev-parse --abbrev-ref HEAD)"
if [ -n "$(git status --porcelain)" ]; then
  echo "WARNING: the working tree on this host has local modifications:"
  git --no-pager status --short
fi

say "Checking .env"
test -f .env || { echo "ERROR: $APP_DIR/.env is missing. Start from deploy/env.production.example." >&2; exit 1; }
perms=$(stat -c '%a' .env)
[ "$perms" = "600" ] || echo "WARNING: .env is mode $perms; chmod 600 .env"
# Values are never printed — only whether they are present.
missing=()
while read -r name; do
  value=$(grep -E "^${name}=" .env | head -1 | cut -d= -f2-)
  [ -n "$value" ] || missing+=("$name")
done <<'REQUIRED'
FILESHARE_POSTGRES_PASSWORD
FILESHARE_JWT_SECRET
FILESHARE_FRONTEND_ORIGIN
FILESHARE_PUBLIC_API_BASE_URL
FILESHARE_PUBLIC_STORAGE_ORIGIN
FILESHARE_SUPABASE_URL
FILESHARE_SUPABASE_SERVICE_ROLE_KEY
FILESHARE_SUPABASE_STORAGE_BUCKET
FILESHARE_SUPABASE_S3_ACCESS_KEY_ID
FILESHARE_SUPABASE_S3_SECRET_ACCESS_KEY
FILESHARE_VIRUSTOTAL_API_KEY
FILESHARE_RESEND_API_KEY
REQUIRED
if [ "${#missing[@]}" -gt 0 ]; then
  printf 'ERROR: these are empty in .env:\n'
  printf '  - %s\n' "${missing[@]}"
  exit 1
fi
echo "every required variable has a value"

say "Checking the proxy network exists"
docker network inspect proxy >/dev/null 2>&1 || {
  echo "ERROR: network 'proxy' is missing. Run 01-install-docker.sh." >&2; exit 1; }

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

say "Building images"
# The frontend bundle inlines the public origins at build time, so this must happen on a
# host whose .env carries the production values (Architecture.md §15).
"${COMPOSE[@]}" build

say "Starting Postgres and Redis first"
"${COMPOSE[@]}" up -d postgres redis
for _ in $(seq 1 30); do
  state=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Health}}' | awk '/^postgres/{print $2}')
  [ "$state" = "healthy" ] && break
  sleep 2
done
echo "postgres: ${state:-unknown}"

say "Applying migrations"
# Explicit and before the API starts, rather than on container start: a migration that
# runs automatically on every boot is a migration nobody watches.
"${COMPOSE[@]}" run --rm --no-deps backend alembic upgrade head
"${COMPOSE[@]}" run --rm --no-deps backend alembic current

say "Starting the rest"
"${COMPOSE[@]}" up -d
"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'

say "Readiness, from inside the network"
for _ in $(seq 1 20); do
  if body=$(docker run --rm --network fileshare-internal curlimages/curl:latest \
      -fsS http://fileshare-backend:8000/health/ready 2>/dev/null); then
    echo "$body"
    break
  fi
  sleep 3
done

say "Done"
echo "Next: deploy/04-backup.sh to install the nightly dump, then the M8c checks."
