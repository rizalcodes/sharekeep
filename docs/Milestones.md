# Milestones — Secure File Sharing Platform

Order follows PRD.md §7. A milestone is **Done** only when its manual validation column
has actually been performed, not when the code merely exists (Rules.md §6).

| # | Deliverable | Status |
|---|---|---|
| M0 | Project scaffold: FastAPI backend, Next.js frontend, Docker Compose, Celery+Redis wired, storage bucket configured | In progress |
| M1 | Auth + workspace + RBAC (Owner/Editor/Viewer), workspace invitations | Not started |
| M2 | File/folder CRUD, nested folders, storage quota enforcement | Not started |
| M3 | Upload pipeline: drag & drop + chunked multipart upload to object storage | Not started |
| M4 | Malware scanning (Celery + VirusTotal) + file versioning | Not started |
| M5 | Sharing: 4 link types, password protection, expiry, modifiers | Not started |
| M6 | Audit log + email notifications (Resend) | Not started |
| M7 | Search/filter, frontend polish, design pass | Not started |
| M8 | Deployment to the shared VPS, end-to-end verification | Not started |

---

## M0 — Project scaffold

### Delivered

**Backend** (`backend/`)
- FastAPI app with lifespan startup, CORS scoped to the frontend origin, and security
  response headers. Refuses to boot in production with placeholder secrets.
- `app/core/config.py` — every setting under the `FILESHARE_` prefix (Rules.md §2),
  including the VirusTotal ceilings and rate budget that M4 depends on.
- `app/core/logging.py` — redaction filter applied to every handler. Credentials cannot
  reach the logs even if something logs them by accident (Rules.md §3).
- `app/core/database.py` — async SQLAlchemy engine and session dependency.
- `app/core/celery_app.py` — Celery wired to a dedicated Redis, with `task_acks_late`
  so a task that dies mid-flight is retried rather than lost. Beat schedule registered
  for quota reconciliation, the abandoned-multipart sweep, and the Supabase keepalive.
- `app/models/base.py` — declarative base, UUID PK / timestamp / soft-delete mixins,
  and an explicit constraint naming convention so migrations stay readable.
- `app/services/storage.py` — storage client, per-workspace key construction, filename
  sanitization, signed download URLs, and the bucket connectivity probe. Provider-agnostic
  below the client factory; now targets Supabase Storage's S3-compatible endpoint (PRD §10).
- `app/routers/health.py` — `/health` (liveness) and `/health/ready`, which touches
  Postgres, Redis, a live Celery worker, and the storage bucket on every call.
- Alembic configured for async, URL sourced from the environment, baseline revision in.
- `scripts/bootstrap_storage.py` — read-only by default; `--apply` creates and configures
  the bucket idempotently. Also probes the S3 protocol, multipart support, and CORS.

**Frontend** (`frontend/`)
- Next.js 16 App Router, TypeScript, Tailwind 4, standalone output for a small image.
- **Design tokens locked** (Design.md §7): palette, five scan-status colours, and the
  IBM Plex Sans / Inter pairing. See "Locked design tokens" below.
- `src/lib/api.ts` — fetch wrapper that always sends credentials, because the session
  JWT lives in an httpOnly cookie rather than localStorage.
- Readiness page rendering the live dependency checks.

**Infrastructure**
- `docker-compose.yml` — own Compose project (`fileshare`): postgres, redis, backend,
  worker, beat, frontend. Host ports bound to `127.0.0.1` only. Backend, worker and beat
  share a single pinned image so a rebuild cannot leave one of them on stale code.
- `docker-compose.prod.yml` — production overlay: no published ports, backend and
  frontend joined to the existing reverse-proxy network on the shared VPS.
- `.env.example` covering every `FILESHARE_` variable; `.env` gitignored.

### Locked design tokens (Design.md §7)

| Token | Value | Use |
|---|---|---|
| canvas | `#FAFAFA` | page background |
| surface | `#FFFFFF` | cards, tables |
| border | `#E0E4E8` | dividers |
| ink | `#1F2933` | primary text (never pure black) |
| ink-muted | `#52606D` | secondary text |
| primary | `#1E3A5F` | actions, links |
| primary-hover | `#2C5282` | hover, focus ring |
| status-clean | `#157F3D` | scan clean |
| status-scanning | `#B7791F` | scan pending/in progress |
| status-flagged | `#B42318` | scan flagged |
| status-unscanned | `#6B7280` | file too large to scan |

Headings: IBM Plex Sans (400/500/600). Body: Inter. Mono: IBM Plex Mono.
Light theme only — no dark mode in v1; nothing in Design.md calls for one.

### Verification

Performed 7 Sep 2026, and re-run in full after the storage-provider change later the
same day (PRD §10). Every row below was actually executed, not reasoned about.

| Check | How | Result |
|---|---|---|
| Backend unit tests | `pytest` in `backend/` | 24 passed |
| Lint | `ruff check app tests scripts` | clean |
| Frontend production build | `npm run build` | compiled + typechecked, 4 routes |
| Compose files valid | `docker compose config`, base and prod overlay | both valid |
| Log redaction | adversarial: AWS key id, JWT, prefixed API key, DSN password | none survive |
| Filename sanitization | adversarial: `../`, `..\`, absolute path, NUL byte | key stays inside its workspace prefix, depth unchanged |
| Alembic against real Postgres | `alembic upgrade head` on the container DB | `0001_baseline (head)` |
| Full stack up | `docker compose up -d` | 6 containers up, all healthy |
| Postgres reachable from API | `GET /health/ready` | ok |
| Redis reachable from API | `GET /health/ready` | ok |
| Celery round trip | `ping.delay()` from the backend container | returned `pong` in <1s |
| Worker task registration | `celery inspect registered` | all 4 maintenance tasks present |
| Beat schedule registered | inspected `beat_schedule` in the beat container | all 3 cron entries present |
| Storage probe reports honestly | `GET /health/ready` with no storage creds | `storage.ok=false`, HTTP 503 — correct, not a false green |
| CORS allows the frontend | `Origin: http://localhost:3000` | `access-control-allow-origin` + credentials |
| CORS rejects other origins | `Origin: https://evil.example.com` | no allow-origin header |
| Security headers | response headers on `/health` | nosniff, DENY, no-referrer |
| Frontend serves | `curl http://localhost:3000` | renders the M0 shell |
| Storage keepalive round trip | `storage_keepalive.delay()` from the backend container | executed, returned the storage status |
| Storage probe names the gap | `GET /health/ready` with no storage config | reports the exact missing variable, not a generic error |
| Bootstrap script without credentials | `scripts/bootstrap_storage.py` | exits 1 with a clear message, changes nothing |
| Storage bucket configured | `scripts/bootstrap_storage.py --apply` | **not done — needs the new Supabase project's credentials** |

Four fixes came out of executing rather than reviewing:

1. The log redaction regex used `\b` before the keyword, which does not match between
   `_` and `A`, so `FILESHARE_VIRUSTOTAL_API_KEY=...` leaked straight through. Caught by
   the adversarial test.
2. The `beat` container inherited the image's HTTP healthcheck and reported a false
   "unhealthy" despite running correctly, since beat serves no HTTP.
3. **`backend`, `worker` and `beat` each built their own image from the same context**,
   so rebuilding `backend` alone left the worker and beat running stale code — the new
   keepalive task was missing from both. Caught because the round-trip test failed with
   `NotRegistered`. All three now share one pinned image tag.
4. `test_production_startup_requires_real_secrets` was reading the developer's real
   `.env` and could have passed for the wrong reason. Every input is now passed
   explicitly.

A fifth was caught by a test rather than by inspection: the local `.env` still carried
the old 5GB quota and AWS variables after the provider change, and
`test_default_quota_fits_inside_the_provider_account_limit` failed until it was
regenerated from the updated template.

### Storage provider change (7 Sep 2026, mid-M0)

Applied per PRD §10 — AWS S3 → Supabase Storage via its S3-compatible endpoint. What
this actually touched: `config.py` (env block), `storage.py` (client factory only —
key construction, sanitization and prefixing are provider-agnostic and unchanged),
`bootstrap_s3.py` → `bootstrap_storage.py`, the keepalive task, `.env.example`, the
frontend's dependency label, tests, and every doc that named S3.

The storage-key and traversal tests **passed unchanged**, which is the useful signal:
they assert key shape, not provider, so the abstraction held.

### Open at the end of M0

- **Supabase credentials not supplied**, so the bucket has not been created or verified.
  This is the one M0 deliverable not complete. Everything around it is in place —
  `scripts/bootstrap_storage.py --apply` finishes it in one command once the keys land.
- **CORS for browser-direct upload is unproven.** The bootstrap script's preflight probe
  settles it, but needs real credentials to run. This decides the M3 upload path
  (PRD §10).
- **VirusTotal and Resend keys** not supplied. Not needed until M4 and M6.
- **Rate-limiting reference document** (`api_keys` / `rate_limit_rules` pattern) not
  located. Needed at M4; the limiter will be designed fresh if it does not surface.
- **The client-rendered readiness list** was verified through its inputs (the API
  response and CORS headers) rather than in a browser. Worth an eyeball at M7.
