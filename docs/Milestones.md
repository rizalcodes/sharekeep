# Milestones — Secure File Sharing Platform

Order follows PRD.md §7. A milestone is **Done** only when its manual validation column
has actually been performed, not when the code merely exists (Rules.md §6).

| # | Deliverable | Status |
|---|---|---|
| M0 | Project scaffold: FastAPI backend, Next.js frontend, Docker Compose, Celery+Redis wired, S3 bucket configured | In progress |
| M1 | Auth + workspace + RBAC (Owner/Editor/Viewer), workspace invitations | Not started |
| M2 | File/folder CRUD, nested folders, storage quota enforcement | Not started |
| M3 | Upload pipeline: drag & drop + chunked multipart upload to S3 | Not started |
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
  for the quota reconciliation and abandoned-multipart sweep tasks.
- `app/models/base.py` — declarative base, UUID PK / timestamp / soft-delete mixins,
  and an explicit constraint naming convention so migrations stay readable.
- `app/services/storage.py` — S3 client, per-workspace key construction, filename
  sanitization, signed download URLs, and the bucket connectivity probe.
- `app/routers/health.py` — `/health` (liveness) and `/health/ready`, which touches
  Postgres, Redis, a live Celery worker, and the S3 bucket on every call.
- Alembic configured for async, URL sourced from the environment, baseline revision in.
- `scripts/bootstrap_s3.py` — read-only by default; `--apply` creates and configures
  the bucket idempotently.

**Frontend** (`frontend/`)
- Next.js 16 App Router, TypeScript, Tailwind 4, standalone output for a small image.
- **Design tokens locked** (Design.md §7): palette, five scan-status colours, and the
  IBM Plex Sans / Inter pairing. See "Locked design tokens" below.
- `src/lib/api.ts` — fetch wrapper that always sends credentials, because the session
  JWT lives in an httpOnly cookie rather than localStorage.
- Readiness page rendering the live dependency checks.

**Infrastructure**
- `docker-compose.yml` — own Compose project (`fileshare`): postgres, redis, backend,
  worker, beat, frontend. Host ports bound to `127.0.0.1` only.
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

Performed 7 Sep 2026. Every row below was actually executed, not reasoned about.

| Check | How | Result |
|---|---|---|
| Backend unit tests | `pytest` in `backend/` | 18 passed |
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
| Worker task registration | `celery inspect registered` | all 3 maintenance tasks present |
| Beat schedule registered | inspected `beat_schedule` in the beat container | both cron entries present |
| S3 probe reports honestly | `GET /health/ready` with no AWS creds | `storage.ok=false`, HTTP 503 — correct, not a false green |
| CORS allows the frontend | `Origin: http://localhost:3000` | `access-control-allow-origin` + credentials |
| CORS rejects other origins | `Origin: https://evil.example.com` | no allow-origin header |
| Security headers | response headers on `/health` | nosniff, DENY, no-referrer |
| Frontend serves | `curl http://localhost:3000` | renders the M0 shell |
| S3 bucket configured | `scripts/bootstrap_s3.py` | **not done — no AWS credentials yet** |

Two fixes came out of this pass rather than from review: the log redaction regex used
`\b` before the keyword, which does not match between `_` and `A`, so
`FILESHARE_VIRUSTOTAL_API_KEY=...` leaked straight through (caught by the adversarial
test); and the `beat` container inherited the image's HTTP healthcheck and reported a
false "unhealthy" despite running correctly, since beat serves no HTTP.

### Open at the end of M0

- **AWS credentials not supplied**, so the S3 bucket has not been created or verified.
  This is the one M0 deliverable not complete. Everything around it is in place —
  `scripts/bootstrap_s3.py --apply` finishes it in one command once the keys land.
- **VirusTotal and Resend keys** not supplied. Not needed until M4 and M6.
- **Rate-limiting reference document** (`api_keys` / `rate_limit_rules` pattern) not
  located. Needed at M4; the limiter will be designed fresh if it does not surface.
- **The client-rendered readiness list** was verified through its inputs (the API
  response and CORS headers) rather than in a browser. Worth an eyeball at M7.
