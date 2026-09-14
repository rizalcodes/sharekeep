# Milestones — Secure File Sharing Platform

Order follows PRD.md §7. A milestone is **Done** only when its manual validation column
has actually been performed, not when the code merely exists (Rules.md §6).

| # | Deliverable | Status |
|---|---|---|
| M0 | Project scaffold: FastAPI backend, Next.js frontend, Docker Compose, Celery+Redis wired, storage bucket configured | **Done** (7 Sep 2026) |
| M1 | Auth + workspace + RBAC (Owner/Editor/Viewer), workspace invitations | **Done** (8 Sep 2026) |
| M2 | File/folder CRUD, nested folders, per-object permissions, chunked upload API, quota | **Done** (8 Sep 2026) |
| M3 | Upload UX: drag & drop, per-chunk progress, retry/resume, auth UI, folder browser | **Done** (9 Sep 2026) |
| M4 | Malware scanning (Celery + VirusTotal) + file versioning | **Done** (10 Sep 2026) |
| M5 | Sharing: 4 link types, password protection, expiry, modifiers | **Done** (11 Sep 2026) |
| M6 | Audit log UI + email notifications (Resend) | **Done** (12 Sep 2026) — first marked Done on 11 Sep in error; see the M6 record |
| M7 | Complete the interface, search + tags, design pass (three stages, PRD §10e) | In progress |
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
- Uploads still land as `scan_skipped` until M4 wires VirusTotal. *(Done in M4; the
  state remains common, for a different and deliberate reason — see below.)*

## M4 — Malware scanning (VirusTotal) · Done

Branch `feature/m4-scanning`. Status: complete, verified against the live stack.

### Docs amended first

The rate-limiting reference document arrived at the start of this milestone. It was
written for **inbound** API-key management — keys we issue to consumers, with tiers,
owners and revocation — while M4 needs the mirror image: outbound budget management
against one provider, with one key we hold ourselves. Only the counter pattern
transferred. `api_keys`, `rate_limit_rules` and `api_usage_logs` describe a feature this
product does not have and were not built; Architecture §3a records the reasoning table
by table.

Architecture §3 was rewritten rather than patched (v1.7, §16), because four decisions
changed its shape:

1. **"Tasks block on the limiter" was not implementable.** Tolerable for a per-minute
   window, catastrophic for a per-day one: at `concurrency=2`, two tasks blocking on an
   exhausted daily budget deadlock every scan for up to 24 hours. Replaced with
   reserve-or-requeue — which the reference document itself recommends.
2. **Budget exhaustion keeps a file `pending`, never `scan_skipped`.** Auto-promoting
   would make exhaustion a scanning bypass anyone could trigger on purpose: burn the
   day's quota with junk, then upload the payload and have it served unscanned.
3. **File content is no longer submitted by default (§3b).** Free-tier submissions are
   shared with the VirusTotal community, so the previous design published a private file
   to a public pool whenever VirusTotal had not seen it. Now hash-lookup only unless
   explicitly enabled.
4. **`flagged` and `scan_failed` had no exit (§3d).** Both were described as lasting
   "until an admin review clears it" with nothing owning that review, so M4 would have
   shipped two permanently inaccessible states.

Two more contradictions surfaced from reading §3 against the code M2 actually shipped:
the checksum was computed **inside** the scan task, so `scanning_enabled=false` produced
no integrity checksum at all; and step 9 described version bookkeeping that
`complete_upload` has done synchronously since M2. Schema went to v1.4 (`scan_results`,
the three override fields, the `scan_override` audit action), PRD gained §10b, and
Design §5 gained the queued state, the required reason on "not scanned", and the rule
that a released file keeps its flagged treatment.

Also corrected: §3d and Design §5 said "owner or admin". There is no admin role — only
viewer, editor, owner — so both documents were fixed rather than left implying one.

### Delivered

- **Outbound budget limiter** (`services/vt_budget.py`) — two calendar windows in Redis,
  granted together in one atomic Lua script, **failing closed**.
- **VirusTotal client** (`services/virustotal.py`) — hash lookup, submission, and
  analysis polling, with response shapes established by probing the live API.
- **Verdict cache** (`scan_results`) keyed on SHA-256, with a staleness window.
- **Two tasks**: `compute_checksum` on every completed upload, and `scan_file_version`
  ordered cheapest-first (cache → reservation → hash lookup → optional submission).
- **Re-drive sweep** every 15 minutes for scans that stopped without resolving.
- **Owner-only override endpoint**, recorded beside the status rather than over it.
- **Scanner budget on `/health/ready`**, reported alongside the checks and never as one.
- **Badge honesty** in the UI: a queued state distinct from scanning, a stated reason on
  every "not scanned", and a released file that stays visibly flagged.

### Verification

| Check | Result |
|---|---|
| Backend suite | 195 passing, 0 skipped, ruff clean |
| Frontend | eslint clean, production build clean across 6 routes |
| Migration | upgrade, downgrade, re-upgrade all clean against the live database |
| Migration/model parity | new permanent test, proven to fail on a deliberate divergence |
| Budget limiter | verified against the running Redis, including atomicity and fail-closed |
| VirusTotal client | verified against the live API, including the submission path |
| End-to-end | verified against the live worker, live VirusTotal and live Supabase |

The end-to-end run: EICAR uploaded, landed `pending`, resolved to `flagged` for exactly
one request, refused to download with a reason naming the flag, checksum matching the
real SHA-256. A second copy of the same content cost **zero** requests. A file
VirusTotal had never seen became `scan_skipped` with the reason stated, not `clean`. The
daily budget was then forced to its limit: the next upload **stayed `pending`** and
undownloadable rather than being promoted — the bypass check. The sweep found it and it
resolved once re-driven. Finally the flagged file was released: still `flagged`, now
downloadable, bytes byte-identical, and the listing still saying it was flagged and who
released it.

### Defects found by running it

**The event loop bug, and only manual validation could have caught it.** Every Celery
task bridged sync to async with `asyncio.run`, which builds a **new** event loop per
call — while the database engine and the Redis clients are module-level singletons whose
pooled connections stay bound to the loop that opened them. The first task in a worker
process succeeded and the second failed with `RuntimeError: got Future attached to a
different loop`. The pre-M4 tasks hid it by running at most once per fork; scanning runs
per upload, so the second upload in a worker's lifetime broke.

Two things made it worse than an ordinary bug. It presented as a *database* error, and
the budget limiter dutifully failed closed on the same exception and logged "budget
unavailable" — both pointing away from the cause. Fixed with one loop per process
(`core/async_bridge.py`), created lazily so a fork never inherits its parent's, and
covered by a subprocess regression test **with a negative control** proving the old
bridge genuinely fails that way.

**A starved scan displayed "Scanning".** The version was marked `scanning` before the
budget reservation, so a file waiting for capacity showed a spinner that would not move
for hours — precisely the dishonesty Design §5 exists to prevent. The reservation now
comes first, and a budget requeue returns the badge to "queued" unless an analysis
really is running at VirusTotal.

**The test suite was firing real Celery tasks at the live broker.** `upload_file` drives
the real upload-complete endpoint, which now enqueues `compute_checksum`, so one run
pushed 468 tasks into the running worker's queue — rejected there only because that
worker image predated the task module. On a worker running this code they would have
executed against versions that exist only in the test database. Now stubbed by an
autouse fixture that records dispatches instead.

**Login throttling had never been tested.** Leftover rate-limit counters in the shared
Redis made four unrelated M1 auth tests fail, and chasing that revealed the control
required by Architecture §4.0 had no test at all since M1. Counters are now cleared per
test, and throttling is asserted: it engages, it does *not* fire on the first attempt,
and it stays engaged. An untested security control is a claim, not a control.

**An override released an unscanned file.** Caught by the new tests, not by running:
`version_is_downloadable` honoured an override on `pending` or `scanning`. The API
refuses to create one there, so it should be unreachable — but a stray row from a
migration or a manual edit would have handed out a completely unscanned file, and that
function is the last thing between a request and the bytes.

Two of my own assertions were wrong rather than the product, and both are worth
recording. The validation script expected a cold verdict cache on a re-run, when a cache
hit costing zero requests is the cache working. And it expected a budget-starved scan to
resume the moment the counter was cleared, when the task had correctly scheduled itself
for the next UTC midnight — the real recovery path is the sweep, so the script now tests
that instead of a fiction.

### Open at the end of M4

- **`scan_skipped` is now the common outcome**, not an edge case: with submission off by
  default, any file VirusTotal has not seen before lands there. This is the deliberate
  privacy trade in §3b, and it means most genuinely novel content is not virus-checked.
  The badge says so; a deployment that prefers coverage flips one variable.
- **Submission mode is verified but not exercised in production.** The path is tested
  against the live API with self-generated bytes; nothing here has run it with
  `virustotal_submit_unknown_files=true` under real load, and it costs roughly ten times
  the budget per file.
- **A false positive locks the owner out until an override.** Deliberate, with the
  threshold configurable, but worth stating plainly.
- **Recovery latency after budget exhaustion is up to ~45 minutes** (a 30-minute stuck
  threshold plus a 15-minute sweep), in the case where budget frees earlier than the
  task's own scheduled retry.
- No notification is sent when a file is flagged. The `scan_flagged` audit entry is
  written and the notification queue is M6.

## M5 — Sharing: four link types · Done

Branch `feature/m5-sharing`. Status: complete, verified against the live stack.

### Docs amended first

Architecture §4.2–§4.5 was rewritten rather than annotated (v1.8, §17), because four
decisions changed the rules rather than adding to them. Each is recorded in PRD §10b's
sibling section §10c with its reasoning.

1. **Tokens are stored hashed.** Schema §9 specified a raw `token` column, which would
   have made the most widely *distributed* bearer token in the system the only one not
   protected the way M1 already protects refresh tokens and invitations. The cost is
   stated up front instead of discovered: a link cannot be copied again after creation.
2. **A scan override does not extend to share links.** M4 lets an owner release a
   flagged file for their own workspace; letting that same decision feed a public link
   would turn "I accept this risk" into "strangers accept this risk on my word".
3. **Only attempts against links that exist are audited.** An unknown token has no
   workspace to attribute and no target that resolves, and its volume is
   attacker-controlled — with a fail-open inbound limiter as the only bound.
4. **Share links get their own 60-second signed-URL TTL.** Once a URL is issued,
   revoking the link cannot recall it, so the TTL *is* the revocation window.

Three further points were specified because resolution is meaningless without them:
`max_downloads` counts resolutions rather than completed downloads and increments under
a row lock; a link tracks the file so it always serves the current version; and
validation must accept type+modifier combinations. Schema went to v1.5, Rules §3 gained
the hashed-bearer-token convention as a rule rather than a coincidence of three
implementations, and Design §5 covers the once-only link display, the independent
modifiers, the honest "uses" label, and refusing to share a flagged file.

### Verified rather than assumed, before designing

The live bucket was probed first, because §4.3's claims are the provider's to keep:

- `ResponseContentDisposition` **is** honoured, so "download-only" is a fact.
- Supabase serves `content-type: text/plain` regardless of what an object was stored
  as, so a shared `.html` or `.svg` cannot execute in a visitor's browser — a second
  independent layer. The corollary: `files.mime_type` is our own UI metadata, not what
  storage returns.
- A `PUT` against a download URL is refused 403; the signature really is GET-scoped.
- An expired signature returns HTTP **400**, not 403.
- The signed URL discloses the whole storage key — workspace, file and version UUIDs
  plus the filename — to the recipient. Low severity, since every workspace endpoint
  refuses non-members independently, but recorded because a share recipient is by
  definition outside the workspace.

### Delivered

- `share_links` (migration 0005, hand-written), with CHECKs for the three type
  requirements, a rejection of `max_downloads = 0`, and UNIQUE on `token_hash`.
- `services/shares.py`: creation, resolution against every rule in §4.2, revocation,
  and `version_is_shareable` — the stricter scan rule.
- Five endpoints: create, list and revoke under the workspace prefix; preview and redeem
  requiring no authentication at all.
- Per-IP throttling on token attempts and per-link throttling on password attempts.
- Frontend: a share dialog presenting type and modifiers as independent choices, and a
  public `/s/[token]` page handling password entry, sign-in for private links, and every
  refusal with its own reason.

### Verification

| Check | Result |
|---|---|
| Backend suite | 237 passing, 0 skipped, ruff clean |
| Frontend | eslint clean, production build clean across 7 routes |
| Migration | upgrade, downgrade, re-upgrade clean; parity test confirms it matches the models |
| End-to-end | verified against live Supabase with real bytes and a genuinely anonymous client |

The live run: a public link delivered **byte-identical** content to a visitor with no
account, as an attachment rather than inline. A password link refused an empty and a
wrong password, withheld the file name until the password was right, then delivered
identical bytes. A private link refused an anonymous caller (401) and a signed-in
stranger (403), worked for the named recipient, and gave them nothing else in the
workspace (404 on the folder listing). A two-use limit allowed two and returned 410 on
the third. An unknown token returned a flat 404. Per-IP throttling engaged.

**The revocation window was proven in both directions**, since it is a documented
limitation rather than something the design removes: after revoking, the link itself
returned 410 while an already-issued URL still returned 200 — and that URL was refused
with HTTP 400 once its 60 seconds elapsed.

### Defects found by running it

**The refusal explained the wrong policy.** The first live run tried to share a
just-uploaded file and got a 409 telling the user that a scan override does not extend
to share links — while the actual reason was that the scan had not finished yet. A
message about a policy that does not apply is worse than a vague one, because the reader
acts on it. Now `share_refusal_reason` gives the reason that matches the state, with
tests asserting that a pending file's refusal does not mention overrides at all.

**The storage stub did not mirror the real signature.** Adding `ttl_seconds` to
`generate_download_url` broke every share test with `TypeError: <lambda>() got an
unexpected keyword argument` — the same class of fault as M3's `ListParts` stub. Fixed by
mirroring the signature *and* recording every call, which turned the weakness into an
assertion: the 60-second TTL is now checked against what storage was actually asked for
rather than against the response body, which could report 60 while signing for 300. The
authenticated download is asserted to still use the longer default, proving the two are
separate settings rather than one renamed.

Also worth recording: `params` is a Promise in Next 16, unwrapped with React's `use`.
The bundled docs said so; writing `params.token` would have silently been `undefined`.
The repo's own `frontend/AGENTS.md` exists to prompt exactly that check.

### Open at the end of M5

- **A revoked link keeps working for up to 60 seconds** through an already-issued URL.
  Inherent to presigned URLs; narrowed, documented, and proven, not eliminated.
- **`max_downloads` counts uses, not downloads.** A visitor who abandons a transfer has
  still spent one, and a single issued URL can be replayed until it expires.
- **Share links target files only, not folders** (Schema §9 v1 scope).
- **A private link supports one recipient**; multiple recipients means multiple links.
- **No email notification when a file is shared.** The audit entries are written; the
  notification pipeline is M6.
- **`allowed_user_email` must already have an account.** Refused explicitly at creation
  rather than producing a link nobody can open.

## M6 — Audit log UI + email notifications (Resend) · Done

Branch `feature/m6-audit-notifications`. Status: complete, verified against the live
stack including real Resend sends and a real browser. **First marked Done on 11
September with every invitation link broken**; corrected on 12 September — see
"Found after M6 was first marked done" below.

### Verified rather than assumed, before designing

The Resend API was probed first, and two findings changed the plan rather than
decorating it:

- **The configured sender was refused 403 on every send.** `noreply@example.com` is what
  the project's configuration shipped with since M0 — a plausible-looking placeholder
  the provider rejects outright, because its domain is unverified. M6 would not have
  half-worked; it would have delivered nothing, and the reason would have looked like a
  code fault.
- **The key is restricted to sending.** `GET /emails/{id}`, `/domains` and `/api-keys`
  all return 401. **Delivery is therefore unobservable to us** — we cannot know whether a
  message arrived, bounced or was opened. That is a good posture for a leaked key and a
  real constraint on what the schema may claim.
- Without a verified domain the **only** permitted recipient is the account owner; anyone
  else is a permanent 403 naming that address. Five of the six trigger paths could
  otherwise never be exercised end to end.
- **`delivered@resend.dev` and `bounced@resend.dev` are exempt** from that rule, which is
  the only reason a real non-owner delivery could be proven at all.
- **Six sends with no pause drew no 429**, despite a documented 2/second ceiling. The
  limiter follows the documented figure: an unobserved limit is not an absent one.
- Cloudflare fronts the API and refuses some clients by user agent with a bare
  `error code: 1010` and no JSON. Found when urllib was blocked and curl was not, so the
  client sets an explicit User-Agent and treats an unparseable body as transient rather
  than reading it as a verdict.

### Docs amended first

Architecture §5 was rewritten rather than annotated (v1.9, §18) — its three bullets
implied the status field tracked delivery, which the provider makes impossible. §4.7 is
new. Four decisions were put to the developer and recorded in PRD §10d:

1. **The audit log is owner-only.** §4.6 said who may administer a workspace and never
   said who may read its history. The permissive default is not a convenience: the table
   holds the IP address of every share-link visitor and every member's activity pattern.
   Editors and viewers get 403, non-members 404.
2. **`file_shared` notifies only a private link's named recipient.** The other three
   types have nobody to notify, and emailing whoever just pressed "create link" is noise
   dressed as a feature.
3. **`scan_flagged` notifies the uploader *and* every owner**, deduplicated. Only owners
   can release a flagged file (§3d); only the uploader knows why theirs vanished.
4. **Mail is redirected in development**, with a banner naming the intended recipient and
   the redirect recorded on the row.

Schema went to v1.6: `notifications` gained `attempts`, `last_error`, `sent_at`,
`provider_message_id` and `redirected_to`; `user_id` became nullable and `email` was
added, because an invitation is addressed to someone who may have no account yet — the
one notification M1 had explicitly deferred to M6 was the one the original table could
not carry. `status = sent` is defined down to "the provider accepted it". §10 gained the
`label` convention. Rules §3 gained two rules — what an email may contain, and why a
send-only key is preferred — and §4's file tree was corrected, having drifted since M4.
Design §5 gained nine entries; it had nothing about either surface.

### Four things the docs had left standing

Found while reading rather than while building, and all fixed here:

- **`AuditAction.QUOTA_EXCEEDED` was written by nothing.** In the enum since M1, raised as
  an exception by `quota.py`, and never audited — so an audit reader would offer a filter
  that could not match a single event, and the empty result would read as "this never
  happened".
- **`storage_quota_warning_ratio = 0.9` had sat in the configuration since M0 with no
  reader at all.** Now wired as an upward crossing, not a state: the obvious version
  would email the owners on every upload for the life of the workspace.
- **Invitations were never emailed.** M1 returned the raw token in the API response as an
  explicit stopgap — the code comment said "Until Resend arrives at M6" — while §4.0 had
  already reasoned about `SameSite=Lax` for the sake of a link in an email that did not
  exist.
- **Audit metadata recorded names inconsistently:** `download` stored `file_name`,
  `delete` stored `name`, `upload` stored neither. Being append-only, the existing rows
  cannot be corrected.

### Delivered

- `notifications` (migration 0006), three new enum types, five trigger points.
- `services/resend.py` — the provider client, kept separate from the domain logic, with
  transient/permanent/not-configured separated because retrying a refusal that is a
  property of the configuration only burns free-tier budget.
- `services/notifications.py` — queueing inside the caller's transaction, recipient
  resolution per §5a, and plain-text-first rendering for all five types.
- `tasks/notification_tasks.py` — delivery with the `sent_at` idempotency guard under a
  row lock, retry state on the row rather than only in Celery, and a bounded sweep on the
  beat schedule as the recovery path.
- `GET /workspaces/{id}/audit` — owner-only, keyset paged on `(created_at, id)`, with
  action filters and a label/actor search.
- Frontend `/activity` — a dense reverse-chronological reading surface, refusals visually
  distinct, the owner-only refusal explaining the policy, and the absence of sign-ins
  explained rather than left looking like a gap.

### Verification

| Check | Result |
|---|---|
| Backend suite | 287 passing (from 237 at the end of M5), 0 skipped, ruff clean |
| Frontend | eslint clean, `tsc --noEmit` clean, production build clean across 9 routes |
| Migration | upgrade, downgrade, re-upgrade clean; parity test confirms it matches the models |
| End-to-end | 31 checks against the live worker and live Resend, all passing, re-run after the fix |
| Browser | 28 checks in real Chrome, starting from the link in the rendered email |
| Permanent failure | 12 checks through the real worker with the redirect off, then restored |

The live run proved the parts a stub cannot. An invitation queued by the API was picked
up by the **separate worker process** and accepted by Resend, with the provider's message
id and `sent_at` recorded. The redirect was applied *and* recorded, while the row still
carried the address it was really for — so a redirected send cannot be mistaken for a
real one. An unverified sender was classified permanent with the provider's own wording
preserved verbatim. `delivered@resend.dev` proved a genuine non-owner delivery. The audit
endpoint refused an editor who **was** a member with 403 and a non-member with 404, and
contained no `login` rows. A refused upload wrote `quota_exceeded` with the attempted
name. The quota warning fired once on the crossing and stayed at one across two further
uploads above the threshold. A private share link's token was absent from the
notification payload.

**Idempotency was proven by the beat schedule rather than asserted.** The sweep ran
repeatedly across the whole session while nine notifications sat in `sent`; every one
still shows `attempts = 1`, and no row with `status = sent` ever took more than one
attempt. The worker log records only the provider's message id — never a recipient or a
subject line — which is the Rules §3 rule holding in practice.

### Defects found by running it

**Rolling back in the quota handler was wrong twice over.** Writing the `quota_exceeded`
audit row after `await db.rollback()` raised `MissingGreenlet` — the rollback expires
every loaded instance, so reading `user.id` on the next line attempted IO from a
synchronous attribute access. Capturing the values first fixed that and exposed the
second fault: the rollback discarded the caller's whole transaction, which in the test
harness took the fixture data with it and produced a foreign-key violation against a
workspace that no longer existed. No rollback is needed at all — `quota.reserve` refuses
before it writes anything, and the commit releases the row lock it took. Two separate
mistakes in four lines, neither visible by reading them.

**The INET column hands back an `IPv4Address`, not a string**, which the response model
rejected outright. Coerced at the boundary rather than by widening the schema's type: the
API contract is a string either way, and widening it would let the shape vary by row.

**`notify_file_shared` was written against a field that does not exist.** It read
`link.allowed_user_email`; a private link stores `allowed_user_id`, which is precisely
why M5 refuses to create one for an address with no account.

**The validation script walked into the M4 event-loop bug.** `asyncio.run` per query
invalidated the engine's pooled connections — "attached to a different loop", the exact
failure `async_bridge` was written for at M4. Worth recording because it was my own new
code repeating a fault the codebase had already solved and documented.

**Two test-harness faults of my own, not the product's.** The new tests carried
`@pytest.mark.anyio`, which forces anyio's own loop while this repo runs
`asyncio_mode = "auto"` with a session-scoped one — a different loop, so every test
failed before reaching any product code. And the live script's editor was never actually
made a member, so its 404 was correct behaviour rather than the 403 the check expected;
there is no "add member" endpoint by design, since membership comes only through an
invitation bound to an address.

`deliver_notification.delay` was added to the autouse dispatch stub. The M4 reasoning
applies with more at stake: an un-stubbed dispatch against a live worker running this
code would not merely be rejected as unregistered — it would send real email to whatever
address a fixture invented, on every test run.

### Found after M6 was first marked done

M6 was first recorded as Done on 11 September (commit `028e0a3`). That was premature,
and this section exists so the record says so rather than quietly absorbing the fix.

**Every invitation email linked to a 404, and put its token where §4.6 forbids it.**
Found on 12 September by re-checking the finished milestone before the merge, not by any
test. The email linked to `/invitations?token=…`; no such page existed, and nothing in
the frontend read an invitation token at all. The query string also contradicted
Architecture §4.6, whose M1 rule — found by inspecting real container logs — is that an
invitation token never travels in a URL a server can log. A frontend server and a
reverse proxy log a query exactly as faithfully as a path.

Why it got through is the part worth keeping. The suite asserted the token was *in* the
email. The live run proved Resend *accepted* the email. Both were true, and neither
followed the link. "The provider accepted it" had been proven; "the reader can use it"
had not been checked by anyone. Earlier in this same milestone I had told the developer
to try inviting someone — had they clicked the link in that email, it would have 404'd.

The fix:

- The link carries the token in the **URL fragment** (`/invitations#token=…`), which no
  server ever receives. §4.6 and Rules §3 now say so.
- A new `/invitations` page reads the fragment, scrubs it from the address bar at once,
  holds the token in `sessionStorage` across a sign-in or sign-up, and sends it only in
  POST bodies. It names the workspace, role and invited address before anything happens,
  and joining is one explicit click.
- Sign-up carries the token and shows the invited address **locked**, because the server
  takes the address from the invitation and ignores the typed one. Sign-in returns to the
  invitation page afterwards.
- In the HTML body, links are now real `<a href>` anchors. Before, they were bare text,
  so whether they could be clicked at all was left to each mail client — the same class
  of fault in a different place.

**Verified in a real browser, starting from the email.** The link is taken from the
stored notification rendered by the same code the worker used for the message Resend
accepted, never from the API response. Installed Chrome, driven by `playwright-core`
from outside the project, against the live stack — 28 checks:

- A new person opens the link, sees the workspace named, creates an account with the
  address locked, lands on the files page as an editor, and the invitation is spent.
- Someone with an account opens it signed out, signs in with the address prefilled, is
  returned to the invitation rather than to the files page, is still not a member, and
  becomes one only on clicking Join.
- A forwarded email opened by a different signed-in user is told plainly the invitation
  is for another address, is offered no Join button, and does not become a member.
- A link with no token says it is incomplete; a tampered token says it cannot be used.
- Across 71 recorded requests the token appeared in **no** URL; it left the browser only
  in POST bodies to `/invitations/preview`, `/invitations/accept` and `/auth/signup`. None
  of the tokens appears anywhere in the backend or frontend logs.

The five new backend tests follow the same path through the API, and a **negative
control** proved they are not decoration: with the old query-string link restored inside
the container, all five failed.

**The browser test found a second defect the fix itself had.** Someone who sees "This
link is incomplete" and pastes the full link into the same tab triggers a fragment-only
navigation — the browser does not reload, so the page kept saying "incomplete" beside a
perfectly good link. The page now listens for `hashchange`. Proven both ways: the first
browser run failed on exactly this, the second passed.

One failure in that first run was mine rather than the product's: the sign-in check read
the email field before the form had loaded the pending invitation. With the test waiting
for the form's "Signing in to join" notice first, the field is correctly prefilled.

**The permanent-failure path was proven end to end through the real worker.** With the
redirect on, the sandbox's owner-only 403 cannot occur inside the pipeline, so this had
only been proven in halves. The redirect was switched off — confirmed from inside the
worker, not just in `.env` — and an invitation queued to an unregistered domain. The
real worker attempted it once, Resend refused it with 403, and the row was marked
`failed` with the provider's own wording, no `sent_at` and no message id. Handing the
same row straight back to the worker returned `already_failed` without contacting the
provider, a sweep run through the worker dispatched nothing, and `attempts` stayed at 1.
The invitation itself remained pending — a refused email does not cost the invitee
their invitation. The redirect was then restored, confirmed from inside the worker again,
and the full live M6 run repeated with redirected delivery recorded as before.

### Open at the end of M6

- **`status = sent` means the provider accepted it, not that it was delivered.** The
  restricted key cannot tell us more. `provider_message_id` is stored so a human can
  answer the rest from the dashboard.
- **Only the account owner can receive mail** until a domain is verified at
  resend.com/domains. Everything else is a permanent 403, which is why
  `FILESHARE_NOTIFICATION_REDIRECT_TO` exists and must be empty in production.
- **No in-app notification list.** PRD §4 is email-only for v1; `notifications.read_at`
  exists for a future in-app surface and nothing reads it yet.
- **Audit rows written in M1–M5 carry no `label`** for some actions. The log is
  append-only, so the reader says the name was not recorded rather than resolving it live
  — which would let a rename rewrite history.
- **No audit-log retention policy.** Every share-link resolution appends a row and
  nothing prunes them. Not a concern at portfolio scale, but it is unbounded growth.
- **The audit log has no export.** Reading it in the browser is the only route.
- **A revocation of access sends no email.** v1 notifies on gaining access only.
- **Email links point at `FILESHARE_FRONTEND_ORIGIN`**, which is `http://localhost:3000`
  in development. M8 must set it to the public URL, or every link in every email will
  point at the recipient's own machine.
- **The invitation link was proven in a real browser against the rendered email, not
  clicked from an actual inbox.** The developer confirmed the email's appearance in
  Gmail; clicking its link there is the one step no script here can take.
- **After joining, the files page opens on the first workspace, not the new one.** The
  new workspace is in the switcher, and the confirmation page says so.

## M7 — Complete the interface, search, design pass · In progress

Branch `feature/m7-search-polish`. Split into three stages by decision (PRD §10e), each
verified and committed on its own, merged together.

### Why M7 is bigger than its line in the PRD

Mapping every backend endpoint against the frontend showed that most of v1's features had
an API and no interface — including downloading one's own file. Polish presupposes the
surfaces exist, so M7 builds them first.

### Plan

| Stage | Scope | Status |
|---|---|---|
| M7a | App shell with a shared workspace selection; row menu with download, versions + rollback, rename, move, delete; trash; per-item permissions (list, grant, revoke — files and folders); members, roles and invitations; releasing a flagged file; workspace settings; sign out everywhere; the permission indicator; member-only grants | **Done** (12 Sep 2026) |
| M7b | Search over name and tags with the five metadata filters; trigram index; tag editor with per-tag limits | **Done** (14 Sep 2026) |
| M7c | Design pass across every page: scan badge colours corrected to Design §3/§5, file-type icons, table/grid toggle, folder drag-and-drop, the readiness page M0 never eyeballed, anti-slop audit | **Done** (15 Sep 2026) |

### M7b — search, tags, and one step still unverified

Delivered: `GET /workspaces/{id}/search` and `/tags`; migration 0008 adding `pg_trgm`
with trigram indexes on `files.name` and `folders.name`; per-tag validation; a search
page with the five metadata filters as removable chips; a tag editor; and tags on file
rows, clickable through to a search.

Four decisions, all taken before coding and recorded in PRD §10e:

1. **The typed query matches a name or any tag.** §4.8 first restricted it to names,
   which would have meant typing "invoice" found nothing for a file named `q3.pdf`
   tagged `invoice` — while PRD §4 promises "filename/metadata/tag search". The tag
   *filter* stays exact: the query is the forgiving way in, the filter the precise one.
2. **Several tag filters mean AND**, because every other control on that page narrows.
3. **Folders leave the results when a file-only filter is used**, and the page says so.
   A folder has no type, size or scan status, so one surviving a "flagged only" search
   would read as a claim about the folder.
4. **Tags appear on file rows**, not only inside an editor. An invisible tag is one
   nobody applies, which would have left tag search with an empty field to search.

**The defect writing the test caught.** The query used `lower(name) LIKE …`, which
returns exactly the right rows and **cannot use a trigram index built on `name`** — it
had quietly defeated the index migration 0008 exists for. It is now `ILIKE` on the
column, and the test asserts the planner picks `ix_files_name_trgm`, with the old
expression included as a contrast proving it does not. The test carries its own evidence.

Two things were checked against the live system rather than assumed: `pg_trgm` is
available (1.6) and the application role may create it — which is also exactly where a
managed Postgres that forbids `CREATE EXTENSION` would fail, so M8 inherits that note —
and the test schema needs the extension for the same reason the real one does; without
it the entire suite errored at setup with `operator class "gin_trgm_ops" does not exist`.

| Check | Result |
|---|---|
| Backend suite | 361 passing (from 328 after M7a), 33 new search tests, 0 skipped, ruff clean |
| Migration | 0008 upgrade, downgrade, re-upgrade clean; parity test agrees with the models |
| Frontend | eslint clean, `tsc --noEmit` clean, production build clean across 13 routes |
| Browser | **Not run** — see below |

**The browser run, and what it reached (14 Sep 2026).** A machine restart brought Docker
back, the database was intact — revision `0008`, 79 users, 134 workspaces, 61 files, 446
audit rows, `pg_trgm` and both trigram indexes still in place, so nothing was rebuilt — and
the frontend was deployed with M7b for the first time. Six of the nine sections of the
browser script ran before Docker failed again:

| Checked in Chrome | Result |
|---|---|
| A tag is shown on a file row, and clicking it opens search already filtered by it | passed |
| …returning only the tagged files | passed |
| One query matching a folder by name, a file by name, and two files by tag | passed |
| A type filter dropping the folder, with the page saying why | passed |
| Two tags narrowing to the single file carrying both | passed |
| An empty result naming the query and the tag filters that emptied it | passed |
| A result carrying its folder path | passed |
| Downloading from a result, opening a folder result, the tag editor's refusal wording, cross-workspace isolation in the browser | **not run** |

**The harness was wrong before the product was.** The first run reported six failures. All
six were the script's: it read each row's entire `textContent` and split on a newline, but
the row renders without block separators, so it compared
`"q3.binother · M7b … · 3.0 KB · just now2026invoiceNot scanned"` against `"q3.bin"` —
while the filtering underneath was correct. Its download wait also polled the workspace
root for a file that lives in a subfolder, so it timed out after three minutes claiming a
scan had never finished. Both are fixed, and the script now waits for a named set of
results rather than reading whatever is on screen at the moment it looks.

**One real defect the run did find.** Clicking a tag navigates to `/search?tag=…`, and
removing that chip left the parameter in the URL — so a reload silently restored a filter
the user had cleared, and the results were then wrong for a reason nothing on screen
explained. The page now scrubs the query once read, the same way the invitation page
scrubs its token fragment.

**The full browser run, and the root cause of the Docker failures.** All nine sections
pass — 20 checks in real Chrome against the live stack — including the four that had never
run: downloading from a search result, opening a folder result, the tag editor's refusal
wording, and search not crossing into another workspace.

Getting there meant finding out why Docker kept dying, and it was not Docker. **Both host
drives were completely full**: C: had 0 bytes free of 76 GB, D: 0.1 GB of 42.8 GB. That
explains every symptom in order — a truncated write corrupting containerd's bbolt store
(`SIGBUS` on an mmap'd page), the engine crash-looping, `apt-get` failing inside a build
with "Read-only file system" when WSL's ext4 remounted itself read-only, a truncated image
blob giving "short read: expected 52629973 bytes but got 15793529", and finally the Docker
CLI itself dying with `pageAlloc: out of memory` because Windows could not grow a page file
on a drive with nothing left. A factory reset had been performed and had *not* fixed it,
because the reset freed 2.6 GB and the disks were still full.

With the developer's approval: a stale 33.6 GB `docker_data.vhdx` from a pre-reset
configuration was deleted (no WSL distro referenced it any more), Docker's disk image
location was moved from C: to D: where there is room, and ~5.2 GB of caches and temp files
were cleared from C:. The migration chain then applied cleanly from `0001` to `0008` on an
empty database — something only a wipe makes it possible to prove — and the stack came up
with database, Redis, Celery, storage and scanner all reporting ready.

**Two defects the browser run exposed, neither of them in search.**

*Every validation refusal in the product reached the user as "[object Object]".* FastAPI
sends two shapes: a deliberate `HTTPException` has a string `detail`, while a Pydantic
failure sends a **list** of error objects — and the client called `String()` on it. The tag
editor was refusing `bad/slash` for exactly the right reason and showing the reader nothing
they could act on. This was not a tag bug: it applied to every field-level refusal in the
app. The client now reads the list, strips Pydantic's `Value error, ` prefix, and names the
field only when the message does not already. The refusal now reads "tags: A tag can
contain letters, digits, spaces, hyphens, underscores and dots".

*The test database was never created by code.* `fileshare_test` was made by hand at M1 and
survived on this machine until the volume wipe removed it — at which point all 361 tests
errored with `database "fileshare_test" does not exist`, on code that had not changed. A
suite that cannot run on a fresh machine is not one anyone else can run, so the harness now
creates it when missing, for the same reason it creates `pg_trgm`.

**The harness was wrong three times before the product was.** The first run reported six
failures and the second one more; every one was the script's. It read each row's whole
`textContent` and split on a newline the markup does not contain, so it compared
`"q3.binother · M7b … · 3.0 KB · just now2026invoiceNot scanned"` against `"q3.bin"`. It
polled the workspace root for a file that lives in a subfolder, then blamed a scan for
never finishing. It waited for a download from a name that renders as plain text until the
scan resolves, so the click hit nothing. And it asserted on `getByRole("alert")`, which on
this page matched the document title. Each is recorded because the pattern matters: a
failing check is a claim about the product that has to be verified like any other.

| Verified | Result |
|---|---|
| Backend suite | 361 passing, 0 failures, 0 errors, on a database the harness created itself; ruff clean |
| Migrations | `0001`…`0008` applied to an empty database; round-trip and parity clean |
| Frontend | eslint, `tsc --noEmit`, production build clean across 13 routes |
| Browser | 20 checks across 9 sections, real Chrome, live stack — all passing |

### Open at the end of M7b

- **40 MB across 116 objects sits in the Supabase bucket**, most of it now orphaned: the
  local database was wiped while the bucket is remote, so no row refers to those bytes any
  more. Harmless at 40 MB of a 1 GB plan, and `list_prefix` plus the workspace purge task
  make a sweep straightforward when it matters.
- **Tag search over a substring scans the workspace's files.** The GIN index on `tags`
  serves exact membership, which is what the tag *filter* uses; the typed query matches
  tags with `ILIKE`, which no index covers. Acceptable at this scale, and noted where the
  query is built.
- **C: is the drive Docker Desktop itself runs from**, and it sits at ~4 GB free. The data
  disk now lives on D:, but a long build can still press on C:.

### M7a — what it delivered, and what it found

Every feature v1 already had an API for now has an interface, and the signed-in pages
share one shell: the session and the chosen workspace belong to the shell, so opening
Activity shows the workspace the user was just looking at. Pages are keyed by workspace,
so switching starts fresh rather than carrying a folder or filter across. Row actions are
built from the viewer's role on *that item* — a grant can narrow or widen it (§4.1) — and
anything their role would refuse is absent rather than shown.

Found while building the surfaces that expose them, both recorded in the docs first:

- **Deleting a workspace orphaned every object it held** (Architecture §6c). The one hard
  delete in the system: once the rows were gone, nothing could find the objects, because
  the retention sweep works from `file_versions`. A background prefix purge now runs.
  Probed before designing: Supabase supports paginated listing under a prefix and
  **refuses** S3's batch delete, so cleanup is one request per object.
- **An owner could raise their own workspace's quota** without bound, past the 1 GB
  bucket every workspace shares (§4.6). Now lower-only, up to the platform ceiling.
- **Nobody could leave a workspace**, though §4.6's invariant already spoke of an owner
  not being "allowed to leave" (§4.6).
- **M6's claim that every audit write carried a label was wrong.** The download wrote no
  metadata at all, so M6's search for metadata blocks never saw it, and two grant writes
  did not match its pattern. A structural test now walks the syntax tree and fails on any
  workspace-scoped `audit.record` without one.

### Verification (M7a)

| Check | Result |
|---|---|
| Backend suite | 328 passing (from 287 at the end of M6), 0 skipped, ruff clean |
| Frontend | eslint clean, `tsc --noEmit` clean, production build clean across 12 routes |
| Migration | 0007 upgrade, downgrade, re-upgrade clean; parity test agrees with the models |
| Browser | 38 checks in real Chrome against the live stack, run twice with no failures |

The browser run drove the whole surface as a person would: downloading a file the UI
could not download before, versions and a rollback, rename, a new folder and a move,
delete with undo, a restore from the trash, an invitation accepted through the link the
Members page shows once, a narrowed grant seen from the other person's own browser, an
owner refused a quota raise, and the last owner refused leaving.

**A negative control for the security fix**: with the pre-M7 resolver restored inside the
container, a grant row for a non-member resolved to `owner`. The latent defect was real,
not theoretical.

Three failures in the first browser runs were the test's fault, not the product's, and
the third is worth keeping: a file is **not** downloadable immediately after a rollback.
A restored version is a new version and waits for its scan, even though the verdict comes
from the cache at no cost. The interface is right to withhold it; the test had to wait,
the way a person would.

### Docs amended first

Architecture v1.10 (§4.1 membership precondition, §4.8 search, §4.9 rollback, §6b trash,
§19), Schema v1.7, PRD §10e, Design §3 and §5.

### M7c — the design pass, and what only looking could find

All six items delivered. The first four were specified in the docs before any code
(PRD §10f, Design §3 and §5); the last two were investigations, and both found things.

**Badges.** "Queued" was amber, which Design §3 reserves for work happening *now* — a
file waiting on the scanner's daily budget is not being scanned, so it is neutral
alongside "Not scanned". "Scan failed" borrowed flagged red, so a scan that could not run
looked like a file the scanner had convicted; §3 named an amber-red for it at M0 and never
gave it a value, and M7c values it (`#C2410C`). The override path is unchanged: a released
file keeps its own status treatment and never turns green.

**Icons.** One per category, derived server-side from the same mapping the type filter
uses (Architecture §4.8), so an icon and a filter cannot disagree about what a file is.
They were then *drawn by looking at them*: at 16px every type collapsed into "a page", and
the spreadsheet mark read as a crossed-out document, the image as a figure, the audio as a
blot and the archive's zipper ran into the page's own top edge. Now 18px, with the marks
redrawn.

**Grid.** A density choice rather than a gallery — there are no thumbnails, because
previews would mean reading file contents this product never reads. A card carries the same
facts as a row, and the choice is remembered per person.

**Dropped folders** (deferred from M3). The entry tree is walked, folders are recreated
top-down, and a name that already exists **merges** instead of being refused or duplicated.
The whole tree is counted against the quota before the first byte moves. Verified by a
synthetic `webkitGetAsEntry` tree dispatched as a real drop event, twice — the second drop
merged, and the server's own 409 on the duplicate folder is in the Postgres log.

### Defects found by running it (M7c)

- **The readiness page had never been opened in a browser since M0.** It worked — four
  dependencies checked live, all ok — and carried three defects that only looking could
  find: it announced itself as "MILESTONE M0 · SCAFFOLD"; it threw away the overall
  ready/degraded verdict the API computes; and it rendered none of what each check reports
  about itself (worker count, storage provider, bucket, region, scan budget) under a
  heading promising "every dependency below". Architecture v1.11 §7a now specifies the
  endpoint, which had only ever existed as its implementation, and Design §5 gained the
  entry it never had — a surface with no design entry is a surface nobody re-reads, which
  is how the M0 heading survived seven milestones.
- **The shell header wrapped inside its own nav** at 1024px, dropping Trash and Settings to
  a second line while the account menu stayed on the first, so the header looked different
  depending on how many items a role could see. Two deliberate rows now.
- **At 390px the brand, workspace switcher and account address overlapped.** Two causes in
  one row: a `<select>` will not shrink below its widest option unless told it may, and
  `flex-1` sets the group's basis to 0 so the row never decided it had to wrap.
- **The grid stayed two columns on a phone**, truncating a card's facts to "Document ·
  2.0 …" — a card that no longer carries what a row does.
- **The nav said Members and the page said People**, and the activity filter was the only
  surface using a filled navy button for "selected" while the shell nav, the view toggle
  and the search chips all use the subtle fill.

**Left alone deliberately.** The sole owner still sees Remove and an editable role select.
§5 says an action a role cannot perform should be absent, but this refusal is a state
invariant rather than a permission — which member is the last owner changes — and the page
states the rule in prose rather than offering a disabled control with a tooltip nobody
reads. The M7 reasoning stands; the tension is recorded rather than quietly resolved.

**Open finding for M8.** `/health/ready` reports the Celery worker from
`control.ping(timeout=2.0)`, which a healthy-but-*busy* worker does not answer: observed
here with thirteen scan tasks queued, where the page read `degraded` and the endpoint
returned 503 for a worker that reported `ok` moments later. That is the same flaw the scan
budget was deliberately kept out of the verdict to avoid, and it matters on the VPS if a
proxy or monitor health-checks the endpoint. Recorded in Architecture §7a with three
candidate fixes; it is an architecture decision, not a design-pass change.

### Verification (M7c)

| Check | Result |
|---|---|
| Backend suite | 361 passing, 0 failed, 0 skipped, ruff clean |
| Frontend | eslint clean, `tsc --noEmit` clean, production build clean across 14 routes |
| Browser | 38 checks in real Chrome against the live stack, all passing on the final run |
| Looked at | 6 signed-in pages plus `/status` at 1200px and 390px, and the icon set at 18px and 112px |

The browser run reads the colour the browser actually computed rather than the class name,
because a class pointing at a token nobody defined would still look wrong on screen — and
that was exactly the M0 defect this milestone fixed.

**Five failures in the browser runs were the harness's fault, not the product's**, and each
is worth keeping: forcing scan statuses in the database *while the scan pipeline was still
writing*, so the worker's verdict landed after the UPDATE; `` in a Python heredoc
becoming a literal backspace byte, leaving two regexes that could never match; reading
`/status` while it still said "Checking…" (where "storage" passed only because the word
appears in the intro prose); asserting a dropped folder's files existed while they were
still uploading; and asserting `ready` immediately after queueing thirteen scans, which is
what surfaced the M8 finding above.

### Docs amended first

PRD §10f (the six M7c decisions), Design §3 (the amber-red valued) and §5 (icons, grid,
dropped folders, the readiness page), Architecture v1.11 (§7a readiness semantics, §20).
