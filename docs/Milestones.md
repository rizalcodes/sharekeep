# Milestones — Secure File Sharing Platform

Order follows PRD.md §7. A milestone is **Done** only when its manual validation column
has actually been performed, not when the code merely exists (Rules.md §6).

| # | Deliverable | Status |
|---|---|---|
| M0 | Project scaffold: FastAPI backend, Next.js frontend, Docker Compose, Celery+Redis wired, storage bucket configured | **Done** (7 Sep 2026) |
| M1 | Auth + workspace + RBAC (Owner/Editor/Viewer), workspace invitations | **Done** (8 Sep 2026) |
| M2 | File/folder CRUD, nested folders, per-object permissions, chunked upload API, quota | **Done** (8 Sep 2026) |
| M3 | Upload UX: drag & drop, per-chunk progress, retry/resume, auth UI, folder browser | **Done** (9 Sep 2026) |
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
| Storage bucket configured | `scripts/bootstrap_storage.py --apply` | private, 50MB/file limit, S3 protocol + multipart accepted |
| Readiness fully green | `GET /health/ready` with the bucket live | `status: ready`, HTTP 200, all four dependencies ok |
| **Multipart upload round trip** | real 2-part (6MB+1MB) upload via presigned URLs, then presigned GET | bytes intact, SHA-256 match |
| ETag exposed to the browser | `access-control-expose-headers` on the part response | `etag` — the client can report part ETags back |
| Content-Disposition forces download | presigned GET with a download filename | `attachment; filename="proof.bin"` |
| **Unsigned URL is rejected** | adversarial: fetch the raw object URL with no signature | HTTP 403 |

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

### Storage bucket brought up (7 Sep 2026)

Bucket `fileshare-files` created on a new, separate Supabase project: private, 50MB
per-file limit, S3 protocol enabled, multipart accepted.

**The CORS question is settled — browser-direct upload works.** The preflight returns
`access-control-allow-origin: *` and, more importantly, a real two-part multipart upload
through presigned URLs succeeded end to end with the bytes verified by SHA-256. So
Architecture §2 step 4 stands as designed: no TUS fallback, and no proxying file bytes
through the API. `access-control-expose-headers: etag` confirms the browser can read
part ETags back, which the flow depends on.

Two defects found here, both only findable by running it:

1. **Supabase reports a missing bucket as HTTP 400, not 404**, with the real status
   buried in the body (`{"statusCode":"404","code":"NoSuchBucket"}`). The first `--apply`
   crashed with an unhandled `HTTPStatusError` instead of creating the bucket.
2. **The region default was wrong**, and a wrong region fails SigV4 signing as a bare
   `403` with no explanatory message — indistinguishable from "S3 access not enabled".
   Established empirically that the gateway accepts the project's real region
   (`ap-south-1`) plus `us-east-1` as a generic fallback, and rejects everything else.
   The bootstrap script now names the region as the likely cause in its failure message.

### Open at the end of M0

- **VirusTotal and Resend keys** not supplied. Not needed until M4 and M6.
- **Rate-limiting reference document** (`api_keys` / `rate_limit_rules` pattern) not
  located. Needed at M4; the limiter will be designed fresh if it does not surface.
- **The client-rendered readiness list** was verified through its inputs (the API
  response and CORS headers) rather than in a browser. Worth an eyeball at M7.


---

## M1 — Auth, workspaces, RBAC, invitations

Branch `feature/m1-auth-rbac`.

### Docs amended first (same pattern as M0)

Schema v1.2 and Architecture v1.3, before any code. Four decisions taken at kickoff:
accept-first invitations with the email locked to the token; revocable refresh tokens
in a new `refresh_tokens` entity; `workspace_members` authoritative for permissions
with `owner_id` as a maintained pointer; and `audit_logs` built in M1 rather than M6,
since M1 produces the events the audit log exists to record.

Architecture §4.0 (authentication) and §4.6 (workspace permission matrix) are new —
v1.2 had specified authentication in a single line, and had never said who may
administer a workspace.

### Delivered

- **6 tables**: users, workspaces, workspace_members, workspace_invitations,
  refresh_tokens, audit_logs. Migration written by hand, not autogenerated, because
  the partial unique indexes carry security weight and autogenerate does not reliably
  emit `postgresql_where`.
- **Auth**: Argon2id, JWT access token (15 min) plus opaque refresh token (14 days)
  stored hashed, rotation on every use, reuse detection that revokes the whole chain,
  real logout, and "log out everywhere".
- **One permission path**: `services/permissions.py` plus `WorkspaceAccess`
  dependencies. No route queries `workspace_members` for itself.
- **Invitations**: accept-first flow, token hashed at rest, bound to the invited
  address, `expires_at` authoritative over `status`.
- **Audit**: every M1 state change recorded with actor, IP and user agent.

### Verification

91 automated tests, lint clean, plus 51 manual checks over real HTTP against the
running container stack.

| Check | Result |
|---|---|
| Automated suite | 91 passed |
| Lint (`ruff`, incl. alembic) | clean |
| Migration applied to real Postgres | `0002_m1_identity` head |
| Partial indexes actually created | both present with correct `WHERE` clauses |
| **Migration/model parity** | `compare_metadata` reports zero differences |
| Constraints collided deliberately | 12 tests, each one fires |
| Manual HTTP validation | 51/51 checks passed |
| Audit rows written | 8 action types, actor + IP populated |
| Passwords in logs | none |

Adversarial cases (Rules.md §6), all passing in both pytest and manual HTTP:

- outsider gets 404 — indistinguishable from a nonexistent workspace, so IDs cannot
  be probed — on read, modify, delete, member list and invite
- editor and viewer refused every owner-only action
- sole owner cannot be demoted or removed; allowed only once a second owner exists
- personal workspace cannot be deleted
- expired invitation refused **while its status is still `pending`** — the case that
  would pass if validation trusted the status column
- revoked invitation refused at both accept and signup
- invitation forwarded to a different account refused
- signup with a token cannot register a different address
- member/invitation IDs from another workspace refused
- refresh token replay revokes every session for that user
- refresh JWT rejected as an access token; forged signature rejected
- login reveals nothing about whether an address is registered

### Defects found by running it, not by reading it

1. **Raw invitation tokens were being written to the uvicorn access log** via the
   `GET /invitations/{token}` path. Anyone able to read the logs could accept
   invitations meant for other people. Fixed in two layers: the preview endpoint is
   now `POST /invitations/preview` with the token in the body, and the redaction
   filter covers secret-bearing URL paths. Re-verified against real container logs:
   zero token-shaped strings remain.
2. **`WorkspaceAccess.__call__` could not be resolved by FastAPI** — a nested
   `Annotated[uuid.UUID, Path()]` under `from __future__ import annotations` becomes a
   string ForwardRef. Every workspace route 500'd. Caught by the test suite.
3. **A manual check was tautological** (`d['owner_id'] == d['owner_id']`), proving
   nothing about the owner pointer. Replaced with a real comparison against the new
   owner's user id.

### Open at the end of M1

- **Frontend is unchanged.** M1 is backend-only; auth pages land at M7 per the PRD.
- **Invitation emails are not sent** — the raw token is returned once from the create
  endpoint. Resend arrives at M6.
- **CSRF** relies on `SameSite=Lax` alone. A double-submit token is a candidate
  hardening at M7/M8.
- **Deployment constraint** (Architecture §4.0): the API and frontend must share a
  registrable domain, or the Lax cookie will not be sent. Applies at M8.


---

## M2 — Files, folders, permissions, quota

Branch `feature/m2-files-folders`.

### Docs amended first

Schema v1.3 and Architecture v1.4. Four findings, plus two scope moves agreed with
Rizal: upload-init/complete pulled from M3 into M2 (a file cannot exist without its
bytes, so M2 would have been unverifiable end to end), and `file_permissions` scheduled
into M2 (PRD §4 promised per-file roles, Architecture §4.1 depended on them, no
milestone owned them).

### Delivered

- **4 tables**: folders, files, file_versions, file_permissions.
- **Folder tree**: nested create/rename/move/delete, breadcrumbs, cycle rejection,
  32-level depth cap, `NULLS NOT DISTINCT` uniqueness so two root folders cannot share
  a name.
- **Object-level permissions (§4.1)** resolved by one recursive CTE: file grant →
  nearest ancestor folder grant → workspace role. Nearest wins **even when less
  permissive**.
- **Chunked upload** (§2): `uploads/init` returns presigned per-part URLs, client
  PUTs straight to storage, `uploads/complete` finalises. The API never sees bytes.
- **Quota** counting every retained version, reserved under a row lock, with a
  10-version retention cap and automatic pruning.
- **Trash and restore** via `deletion_batch_id`.
- **Celery**: `reconcile_storage_usage` and `sweep_abandoned_multipart_uploads` are
  now real. The sweep matters more here than on AWS — Supabase has no lifecycle rules,
  so it is the only thing preventing orphaned multipart parts.

### Verification

138 automated tests, lint clean, plus 33 manual checks against the running stack
**with real bytes through Supabase** (the suite stubs storage; this did not).

| Check | Result |
|---|---|
| Automated suite | 138 passed |
| Lint | clean |
| Migration/model parity | zero drift |
| Migration downgrade → upgrade round trip | clean |
| Partial indexes + `NULLS NOT DISTINCT` | present in Postgres, verified by query |
| Real 7MB multipart upload → download | SHA-256 round trip intact |
| Quota after two versions | counts both, exactly |
| Reconcile across 28 workspaces | zero drift found |
| Celery tasks via worker | both return real results |
| Audit rows | upload, download, folder_created/deleted, version_restored |

Adversarial cases: outsider blocked on file read/download/delete (404, not 403);
folder cycle refused and tree still traversable afterwards; unsigned storage URL
refused (403); oversized upload refused before any storage call; upload-complete
replay refused without double-counting quota; grant on a deleted folder confers
nothing; grants do not leak across workspaces; a narrowing grant beats a higher
workspace role; restore does not resurrect separately-deleted items.

### Defects found by running it

1. **Autogenerate silently dropped a foreign key.** `files.current_version_id →
   file_versions.id` was emitted inline with `use_alter=True`, which Alembic discards
   inside `create_table`; `files` is created first, so the FK simply never existed.
   Only the migration/model parity check caught it. Fixed with an explicit ALTER after
   both tables exist, and an idempotent drop in `downgrade`.
2. **Autogenerate would have aborted the migration mid-way** by trying to `CREATE TYPE`
   an enum that already existed (`workspace_role` from M1) and creating `scan_status`
   twice.
3. **Constraint names were doubled** (`ck_folders_ck_folders_no_self_parent`) because
   the naming convention already prefixes them.
4. A manual check asserted the wrong status code (expected 400 for an unsigned URL;
   the correct answer is 403). My assertion was wrong, not the product.

### Open at the end of M2

- **Frontend untouched.** M3 is the drag-and-drop UI and progress.
- **Scan status is `scan_skipped` on every upload** until M4 wires VirusTotal. This is
  deliberate: marking unscanned files `clean` would make the trust badge lie.
- **Retention sweep not built** — soft-deleted objects are not yet purged after 30
  days. Belongs with M4's Celery work.
- **`mime_type` is client-supplied** and stored as metadata only; never used for a
  security decision.


---

## M3 — Upload UX

Branch `feature/m3-upload-ux`.

### Docs amended first

Architecture v1.5 (§2a resumable upload), Design.md (upload queue, quota pre-check,
interrupted uploads, auth surfaces), PRD §10a (two scope additions, two deferrals).

Two additions to a milestone scoped as "purely frontend", both because M3 could not
otherwise be *validated*: **minimal auth UI** (no login page existed, so nothing in
M1/M2 could be exercised from a browser at all) and **two resume endpoints** (the
server knows which parts landed; nothing exposed it). `ListParts` support was verified
against the live bucket before designing on it.

Deferred to M7: folder drag-and-drop (needs recursive traversal plus server-side
structure creation) and the table/grid toggle from Design.md §5.

### Delivered

**Backend** — `GET /uploads` (in-flight sessions with real progress), `GET
/uploads/{version_id}/parts` (completed parts plus freshly signed URLs and an
authoritative byte layout), `DELETE /uploads/{version_id}` (abandon and reclaim quota).

**Frontend** — sign in / sign up / sign out, workspace switcher, folder browser with
breadcrumbs and folder creation, drag-and-drop dropzone, upload queue with real
per-chunk progress, quota meter, interrupted-upload recovery, scan badges.

- **Progress is real**: a fraction of bytes that actually landed, with `part n/m`
  alongside it. No indeterminate spinner (Design.md §5).
- **Failures are classified**: network/timeout/5xx/429 retry with exponential backoff
  up to four attempts per part; 403/404/409/413 stop immediately with a specific
  message and no retry button. An upload that cannot succeed never presents as trying.
- **Quota is checked before the first byte moves**, against both the per-file provider
  ceiling and remaining workspace space, and accounts for what is already queued so
  several individually-fitting files cannot overflow together.
- **Resume is honest**: it says the browser cannot reopen the file by itself and asks
  for the same file, verified on name and exact byte size.

### Verification

| Check | Result |
|---|---|
| Backend suite | 152 passed |
| Backend lint | clean |
| Frontend production build | compiled + typechecked, 6 routes |
| Frontend lint (`eslint --max-warnings=0`) | clean |
| **Resume byte integrity, real storage** | 20MB in 3 parts, interrupted after part 1, resumed — **SHA-256 identical** |
| Interrupted upload discoverable | correct progress, 1/3 parts, 8MB reported |
| Layout offsets | exact and contiguous: (0, 8388608), (8388608, 8388608), (16777216, 4194304) |
| Cancel reclaims quota | 30408704 → 20971520 |
| Nothing dangling after resume | in-flight list empty, quota counts the file once |

Adversarial, against the live stack: an outsider gets 404 listing another workspace's
uploads, 404 fetching resume URLs by either workspace id, 404 cancelling; sees an empty
list in their own workspace; a viewer sees no in-flight uploads and gets 403 on resume;
anonymous folder listing is 401. Folder listing was probed by substituting workspace ids
in both directions.

### Defect found by running it

**`ListParts` does not return `Size` on Supabase, only `PartNumber`, `LastModified` and
`ETag`.** AWS returns `Size`, the code assumed it, and the first real call raised
`KeyError: 'Size'` — a 500 on the listing endpoint. The automated suite passed
throughout, because the stub returned an empty part list and never exercised the field.

Two fixes, and the second matters more: part sizes now come from our own upload plan
rather than the provider (which is the authoritative source for them anyway), and **the
stub was corrected to mirror the provider's real shape**, with new tests that simulate
parts already landed. A stub more generous than reality is worse than no stub, because
it converts a real defect into a passing suite.

### Second defect, found after M3 was marked done

**`FILESHARE_PUBLIC_API_BASE_URL` never reached the browser.** It was a runtime
`environment:` entry on the `frontend` service, but Next.js inlines `NEXT_PUBLIC_*`
into the bundle at build time. Nothing failed locally: the compose default and the
`ARG` default in `frontend/Dockerfile` were both `http://localhost:8000`, so the right
value was present for the wrong reason.

It would have surfaced first at M8, and quietly — the deployed bundle would have called
`localhost:8000`, meaning each visitor's own machine, with no server-side log to point
at the cause. Moved to `build.args`; the runtime entry was removed rather than kept,
since leaving it would advertise a knob that does nothing.

Verified by building with a deliberately different value and grepping the bundle
actually served, plus a negative control that rebuilt the old configuration: the fixed
build put the probe value in 4 chunks and `localhost:8000` in none, the control did the
exact reverse while the environment variable was set. "The container starts" would not
have distinguished the fix from the coincidence it replaced.

### Open at the end of M3

- **Folder drag-and-drop and table/grid toggle** deferred to M7.
- **No resume across devices**: an interrupted upload is listed anywhere you sign in,
  but only the machine holding the file can finish it.
- **Auth UI is deliberately plain**; the design pass is M7.
- Uploads still land as `scan_skipped` until M4 wires VirusTotal.
