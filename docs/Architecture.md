# Architecture: Secure File Sharing Platform — System Design

**Scope note:** system/service design — how components interact, the background job pipeline, security architecture. Not data structure (`Schema.md`) or naming/conventions (`Rules.md`).

**Version:** v1.7 — scan pipeline reworked at the start of M4, 10 September 2026 (see §16). Previously v1.6 (frontend build-time configuration, §15). Previously v1.5 (resumable upload, §14). Previously v1.4 (M2 content model, §13). Previously v1.3 (M1 auth, §12), v1.2 (storage provider, §11) and v1.1 (kickoff, §10).

---

## 1. System Components

- **Frontend:** Next.js (same stack as `ecommerce-ai-platform`).
- **Backend API:** FastAPI.
- **Database:** PostgreSQL (own instance on the shared VPS, separate from the n8n project's database).
- **Object storage:** Supabase Storage, accessed through its **S3-compatible endpoint** (file binaries only — application metadata lives in our own Postgres per `Schema.md`). Temporary provider; AWS S3 remains the long-term target — see §11. Only Storage is used: not Supabase Auth, not Supabase's Postgres for application data.
- **Task queue:** Celery, backed by its own Redis instance (see §7 — a dedicated container, not a shared DB index).
- **Email:** Resend (notifications).
- **Malware scanning:** VirusTotal API (external).
- **Deployment:** Docker Compose, own stack on the existing shared VPS (alongside the paused n8n project).

## 2. Upload Flow (Chunked)

1. Client requests an upload session: `POST /files/upload-init` with filename, size, mime type, target folder.
2. Backend validates: storage quota check (would this upload exceed the target workspace's `storage_quota_bytes`?), permission check (does the user have Editor+ access to the target folder?).
3. Backend creates a `files` row (if new) or prepares a new `file_versions` row (if re-uploading an existing name), generates a multipart upload session against the storage provider's S3-compatible endpoint, returns pre-signed URLs for each part to the client.
4. Client uploads parts directly to object storage using the signed URLs (not proxied through the backend), collecting each part's ETag. **Verified 7 Sep 2026 against the live bucket:** Supabase exposes no per-bucket CORS configuration, so this was proven rather than assumed — the preflight returns `access-control-allow-origin: *`, `access-control-expose-headers: etag` lets the client read part ETags back, and a real two-part presigned multipart upload round-tripped with SHA-256 verified. This step stands as designed; no proxy fallback is needed.
5. Client notifies backend on completion: `POST /files/upload-complete`, returning the part number/ETag list.
6. Backend finalizes the multipart upload, sets `file_versions.scan_status = pending`, enqueues a Celery scan task, updates the target workspace's `storage_used_bytes` transactionally.
7. File is visible in the UI but marked "scanning" until the Celery task completes.

### 2a. Resuming an interrupted upload (specified in v1.5, M3)

A browser refresh or a closed tab destroys the client's record of which parts landed. The provider can answer that question — `ListParts` and `ListMultipartUploads` are both supported on Supabase's S3 endpoint, verified against the live bucket on 9 Sep 2026 — but nothing exposed it, so two endpoints are added:

- `GET /workspaces/{id}/uploads` — in-flight sessions for the workspace: file name, total size, bytes already stored, when it started.
- `GET /workspaces/{id}/uploads/{version_id}/parts` — which part numbers already landed, plus freshly presigned URLs for the ones that did not. Presigned URLs are short-lived by design, so a resume always re-signs rather than reusing stale ones.

**The limit that cannot be engineered away:** a browser cannot re-read a local file after a reload without the user selecting it again — the `File` object dies with the page. So resume is *"this upload is 62% done; choose the same file to continue"*, and the client verifies the re-chosen file matches on name and exact byte size before continuing. Claiming automatic resume would be claiming something no browser permits.

Both endpoints resolve access through the same permission path as everything else, and an in-flight session belonging to another workspace is invisible rather than forbidden.

**Abandoned uploads:** a multipart upload that is initiated but never completed leaves orphan parts and a `file_versions` row with no object behind it. A Celery beat task sweeps both — aborting multipart uploads older than 24h and reaping the corresponding pending rows. On AWS S3 a bucket lifecycle rule would catch these as a second line of defense; **Supabase Storage has no lifecycle rules, so this sweep is the only protection** and must be implemented properly at M3.

## 3. Malware Scan Pipeline (Celery)

**The constraints that shape this design:** the VirusTotal API caps direct file submission at 32MB (32–650MB requires a separate upload-URL endpoint; above 650MB submission is impossible), the free tier allows roughly 4 requests/minute and 500/day, and **free-tier submissions are shared with the VirusTotal community**. Sending a customer's file content to a public analysis pool is a poor property for a product named "secure file sharing", so the default is to look up by hash and **never** upload file content — see §3b.

Two separate concerns, deliberately two separate tasks (amended v1.7, see §16):

- `content_tasks.compute_checksum(file_version_id)` — runs on every completed upload, whether or not scanning is enabled. Streams the object from storage in fixed-size chunks (never loading the whole object into worker memory) and stores the SHA-256 in `file_versions.checksum_sha256`. This is an **integrity** feature and does not belong behind the scanning flag: tying it to scanning meant that with `scanning_enabled=false` no checksum was ever computed at all.
- `scan_tasks.scan_file_version(file_version_id)` — enqueued by the above when scanning is enabled.

### The scan task

1. Read `checksum_sha256` (guaranteed present — the checksum task is its predecessor). Set `scan_status = scanning`.
2. **Local verdict cache first (§3c).** If a fresh verdict for this SHA-256 already exists, use it. This costs **zero** VirusTotal requests and is the main defence for a 500/day budget.
3. **Reserve budget from the rate limiter (§3a).** No VirusTotal call is ever made without a reservation. If no budget is available the task is requeued, not blocked.
4. **Hash lookup:** `GET /files/{sha256}`. One request, **no file content transmitted**, and it resolves any file VirusTotal has seen before. A hit returns the full engine verdict → step 6.
5. **On a miss** (VirusTotal has never seen this file), behaviour depends on `virustotal_submit_unknown_files` (default **false**, see §3b):
   - **Submission disabled (default):** `scan_status = scan_skipped`, reason recorded in `scan_detail`. No bytes leave our infrastructure.
   - **Submission enabled**, `size <= 32MB` → submit via `POST /files`, poll the analysis, then verdict.
   - **Submission enabled**, `32MB < size <= 650MB` → obtain a large-file upload URL via `GET /files/upload_url`, submit, poll, verdict. *Reachable today: the 50MB per-file ceiling (PRD §10) sits above 32MB, so this branch is live and must be tested — only the >650MB branch is currently unreachable.*
   - `size > 650MB` → `scan_status = scan_skipped`, reason recorded.
6. On a verdict: write `file_versions.scan_status` and, if this is the current version, `files.scan_status` → `clean` or `flagged`. Engine hit summary into `scan_detail`, and the verdict into the cache (§3c).
7. If `flagged`: the file is **not** deleted automatically (same "don't auto-delete on an uncertain signal" principle used in the n8n project's stale-SKU handling). It is marked flagged, all access is blocked, an `audit_logs` entry (`scan_flagged`) is written, and a notification is queued. The only exit is an explicit admin override — §3d.
8. If the scan itself fails (API error, timeout): `scan_status = scan_failed`, retried with exponential backoff. After retries are exhausted the version stays inaccessible until a later successful scan or an admin override (§3d). **A rate-limit requeue is not a failure** and does not consume this retry budget (§3a).

**Version bookkeeping.** Earlier drafts had the scan task "chain into version processing" to set `files.current_version_id`, `size_bytes` and `mime_type`. It does not: `complete_upload` sets those synchronously the moment the upload finishes (M2), long before any scan runs. A file therefore becomes *visible* immediately and becomes *downloadable* only when its scan status allows — which is the intended behaviour, and is why the two were separated. The scan task writes scan fields only.

### 3a. Rate limiting and budget (outbound)

Distinct from the inbound, per-IP throttling of share links in §4.4. This governs **our own consumption of one third-party API** using a single key that we hold.

Two fixed windows in **Redis**, each read-and-incremented atomically so two workers cannot both see the last remaining token: a per-minute window with a native TTL, and a per-day window. Sized from `virustotal_requests_per_minute` and `virustotal_requests_per_day`, which are configuration, not database rows — VirusTotal sets these limits, we do not, so a table whose stated purpose is "change limits without a redeploy" would only let us change a number we cannot control.

**Reserve-or-requeue, never block.** A worker thread must never sleep waiting for budget. When no token is available the task is re-queued with a countdown until the window reopens, plus jitter so that a burst of uploads does not stampede the moment it does. Blocking was the original wording and it does not survive contact with the daily window: at `concurrency=2`, two tasks blocking on an exhausted daily budget would deadlock the entire scan pipeline for up to 24 hours.

**When the daily budget is exhausted, a file stays `pending`** — it is not downloadable, and it is not quietly promoted to `scan_skipped`. Promoting it would turn budget exhaustion into a scanning bypass that anyone can trigger on purpose: upload enough junk to burn the day's budget, then upload the payload and have it served unscanned. The cost of the honest choice is that a file uploaded after the budget runs out waits for the next window, so the UI must show a distinct "queued for scanning" state rather than an indefinite spinner (Design.md §5).

Redis is the right home for these counters, and not merely because it is already deployed: the increment-and-check must be atomic, the per-minute window must expire on its own (a Postgres counter table would need a sweeper task purely to delete rows), and the data is **disposable** — losing a rate counter costs at worst a few extra requests and one 429, unlike losing a quota row. There is also no failure mode in which the limiter is unavailable while scans still run: Redis is the Celery broker, so if Redis is down no scan task is delivered at all.

**No generic API call-log table.** `file_versions.scan_detail` already records the analysis id and outcome per version, which answers "why did this file's scan behave this way". A separate log of every outbound call would duplicate that for analytics the PRD does not ask for, and — the lesson from the n8n project's Gemini incident — what actually prevents a quota from burning silently is a counter that *refuses* the call, not a log written after the fact. Current window consumption is exposed on the existing `/status` surface.

**Re-driving stuck work.** A worker killed mid-scan leaves a version in `scanning` with nothing scheduled. A beat task re-drives versions left in `pending` or `scanning` past a threshold, which also covers tasks lost to a requeue that never came back.

### 3b. File content is not submitted by default

`virustotal_submit_unknown_files` defaults to **false**. On a hash miss the file is marked `scan_skipped` with the reason "not previously seen by the scanner; content not submitted", and no bytes leave our infrastructure.

This is a deliberate trade and the cost is real: files VirusTotal has never seen — which includes most genuinely private documents — are never actually scanned, and their badge says so. The alternative, enabled by the flag, gives far broader coverage but publishes a customer's private file to a public analysis pool on the free tier. For a product whose name promises the opposite, the default belongs on the side of not transmitting content; deployments that would rather have coverage can flip one variable.

**Knock-on:** this makes `scan_skipped` a common, reachable state again. PRD §10 recorded it as temporarily unreachable because the 50MB ceiling sits far below VirusTotal's 650MB submission limit; that reasoning covered only the size route to `scan_skipped`, and the hash-miss route is now the usual one.

### 3c. Verdict cache (`scan_results`)

Keyed on SHA-256. A verdict already known for a hash is reused without spending a request, so two users uploading the same PDF, or the same file re-uploaded after an edit-and-revert, cost nothing. With a 500/day budget this is the single largest saving available, and it is why the table exists rather than the checksum being looked up against `file_versions`: version rows are pruned by the 10-version retention cap and hard-deleted with their files, so they cannot be relied on as a cache.

Verdicts age — a file clean today may be flagged next month — so an entry older than `scan_cache_ttl_days` (default 30) is treated as absent and re-looked-up. Schema in `Schema.md` §12a.

### 3d. Flagged files and admin override

Without an exit, `flagged` and an exhausted-retry `scan_failed` are permanent dead ends: the file is inaccessible to everyone, including its owner, forever. A workspace owner or admin may therefore override a specific version.

**The override does not rewrite history.** `scan_status` stays `flagged` or `scan_failed`; the override is recorded separately (`scan_override_at`, `scan_override_by`, `scan_override_reason` — Schema.md §7) and is what grants access. Setting the status to `clean` instead would be a lie: nothing re-scanned the file and nothing pronounced it safe. The UI must keep showing that the file was flagged **and** that a named person chose to release it.

Every override is written to `audit_logs` with the new `scan_override` action, including who, when, and the stated reason. An override applies to one version only — it does not carry to a later upload of the same file.

**Access rule by scan status** (the single source of truth for whether bytes may be served):

| `scan_status` | Downloadable? |
|---|---|
| `pending`, `scanning` | No — the UI shows the file with a scanning or "queued for scanning" badge, but no download URL is issued |
| `clean` | Yes |
| `scan_skipped` | Yes, with a persistent "not scanned" badge |
| `flagged` | **No** — including to the owner — unless an admin override is recorded on that version (§3d) |
| `scan_failed` | No, until a retry succeeds or an admin override is recorded |

Because two of these rows now depend on the override record and not on the enum alone, downloadability is resolved by a function over the version, not by a property of `ScanStatus` in isolation.

**Version rollback interaction:** rolling back to an older version is permitted only if that target version's own scan status allows access under the table above. A flagged current version does not block rollback to a clean earlier one — that is precisely the recovery path.

## 4. Sharing & Access Control

**Every file access request (direct API, or via a share link) goes through the same permission-resolution function.** One code path, so there is exactly one place to audit and one place to test adversarially.

### 4.0 Authentication and session handling

Specified in v1.3 — v1.2 committed only to "JWTs in httpOnly, Secure, SameSite cookies" (§9) and left everything else open.

- **Passwords:** Argon2id. Minimum 12 characters, maximum 128 — the upper bound matters, since an unbounded input to a memory-hard hash is a denial-of-service vector. No composition rules (NIST-aligned).
- **Access token:** short-lived JWT (15 min), carried in an httpOnly cookie. Stateless; never consulted against the database.
- **Refresh token:** opaque random string, 14 days, stored **hashed** in `refresh_tokens` (Schema.md §12). Rotated on every use — the presented token is revoked and points at its replacement.
- **Reuse detection:** presenting an already-rotated refresh token means the token was captured and replayed. Response is to revoke that user's entire chain, not just the presented row, and force re-authentication.
- **Logout** revokes the refresh row, so it genuinely ends the session rather than clearing a cookie while the token stays valid.
- **`SameSite=Lax`**, not `Strict`: a `Strict` cookie is withheld on the cross-site navigation that clicking an emailed invitation link produces, which would break invitation acceptance. Lax blocks cross-site POST, which is the CSRF case that matters.
- **Deployment constraint this creates:** a Lax cookie is only sent when the API and frontend share a registrable domain. `app.example.com` + `api.example.com` works; two unrelated domains would force `SameSite=None` and reopen CSRF. M8 must deploy them as one site.
- **Login throttling:** per-IP and per-email, in Redis. §4.4 requires this for share links; the same reasoning applies to a password endpoint, and `login_failed` already exists in the audit enum.
- **Enumeration:** login failures are indistinguishable whether the email exists or not, and signup does not reveal whether an address is already registered through a differing response.

### 4.1 Effective role resolution (authenticated user)

Resolved in strict precedence order — **first match wins**, more specific always beats less specific:

1. An explicit `file_permissions` grant on the file itself.
2. An explicit `file_permissions` grant on the file's folder, then its parent, then its grandparent — **nearest ancestor wins**. A grant on a deep subfolder overrides a grant on the workspace root, including when the nearer grant is *less* permissive. Most-permissive-wins is explicitly rejected: it makes a narrowing grant silently ineffective, which is a security failure mode.
3. The requester's `workspace_members.role` for the file's workspace.
4. No membership → denied.

Workspace `owner` is not a bypass: it resolves through the same function, it simply loses to nothing.

**Implementation notes added in v1.4**, because step 2 is a tree walk on the hottest path in the system:

- The ancestor chain is resolved with a single recursive CTE, not a query per level. A folder ten deep must not cost ten round trips on every file access.
- **Nesting is capped at 32 levels.** Unbounded depth means unbounded cost here, and no legitimate structure needs more.
- **Cycles are impossible by construction**: moving a folder into its own descendant is rejected (409). A cycle would make this walk non-terminating, so the guard protects the permission path, not just tidiness of the tree.
- A grant is only honoured while its target row is not soft-deleted; a grant on a deleted folder confers nothing.

### 4.2 Share-link resolution (unauthenticated, or a different user than the owner)

The **primary type** determines the base rule:
- `public`: any bearer of the token.
- `expiring`: valid only while `now() < expires_at`.
- `password_protected`: token plus the correct password, checked against `hashed_password` (constant-time comparison via the password hasher).
- `private`: requester must be authenticated AND match `allowed_user_id`.

**Modifiers apply on top of any type** (Schema.md §9): if `expires_at` is set it is enforced regardless of type; if `hashed_password` is set the password is required regardless of type; if `max_downloads` is set, `download_count < max_downloads` is enforced. So a password-protected link that also expires enforces both.

Applied to every link regardless of type: `revoked_at IS NULL`, and the underlying file's `deleted_at IS NULL` and scan status permitting access per §3.

### 4.3 Serving the bytes

On successful resolution, generate a short-lived signed GET URL (minutes, download-scoped only). Never a long-lived or public storage URL, and never a URL that also permits upload.

### 4.4 Rate limiting (inbound)

This section covers traffic arriving at us. Our own outbound consumption of the VirusTotal API is a separate mechanism with separate limits — see §3a.

Share-link token endpoints are rate-limited per IP — an unguessable token is only unguessable if you cannot try millions of them. Password attempts on a password-protected link are separately throttled per token.

### 4.5 Audit

Every resolution attempt — success **and** failure — is written to `audit_logs` with `share_accessed` or `share_access_denied`, capturing `ip_address`, `user_agent`, and the denial reason in `metadata`.

### 4.6 Workspace management permissions

§4.1 resolves access to *files*. It says nothing about who may administer a workspace, which M1 needs. Specified in v1.3:

| Action | owner | editor | viewer |
|---|---|---|---|
| View workspace, list members | yes | yes | yes |
| Create/modify files and folders (M2+) | yes | yes | no |
| Invite, revoke invitations, remove members | yes | no | no |
| Change a member's role | yes | no | no |
| Rename workspace, change quota | yes | no | no |
| Delete workspace | yes | no | no |

Two invariants, both enforced server-side and both tested adversarially:

1. **A workspace always has at least one owner.** The last owner cannot be demoted, removed, or allowed to leave. Otherwise the workspace is orphaned and nobody can ever administer it again.
2. **A personal workspace cannot be deleted** while its user exists (Schema.md §2), and its owner cannot be removed. It may still have other members invited into it — `is_personal` governs deletion, not collaboration.

**Invitation tokens never travel in a URL.** The preview endpoint is `POST /invitations/preview` with the token in the body, not `GET /invitations/{token}` — a web server writes full request paths into its access log verbatim, and an invitation token sitting in a log is workspace access for anyone who can read that log. Found during M1 manual validation by inspecting real container logs; the log redaction filter covers the same shape as a second layer.

Invitation acceptance is bound to the invited address: a signed-in user whose email differs from the invitation's is **rejected**, never silently accepted. An invitation is a grant to one address, not a bearer token for whoever opens the link.

## 5. Notification Pipeline

- Trigger events (file shared, permission granted, quota warning, scan flagged, workspace invitation) create a `notifications` row (`channel = email`, `status = pending`).
- A Celery task picks up pending notifications and sends via Resend, updating `status` to `sent` or `failed`.
- Schema is deliberately channel-agnostic (`notifications.channel` enum) so a `websocket` channel can be added in v1.1 by adding a new delivery task, without altering the trigger-side logic or the schema.

## 6. Storage Quota Enforcement

**What the counter counts (specified in v1.4):** `storage_used_bytes` is the sum of `size_bytes` over all non-deleted `file_versions` rows in the workspace — **every retained version, not just current ones**. The storage prefix physically holds every version, so this is the only definition under which the reconciliation task below can ever agree with reality. Version history therefore consumes quota, which is why `file_versions` caps retention at 10 per file (Schema.md §7).

- Checked at `upload-init` (before any storage interaction begins) using `workspaces.storage_used_bytes + incoming_size > workspaces.storage_quota_bytes`. The provider's own per-file ceiling (`FILESHARE_MAX_FILE_SIZE_BYTES`, 50MB on the Supabase free plan) is checked at the same point, so an oversized file is rejected cleanly instead of failing partway through a multipart upload.
- **The check and the reservation happen under a row lock** on the workspace (`SELECT ... FOR UPDATE`). Without it two concurrent uploads both read the old total, both pass, and the workspace ends up over quota — a check that is not atomic with what it guards is not a check.
- `storage_used_bytes` updated transactionally on upload completion, on version pruning, and on file/version deletion, scoped to the file's `workspace_id` — never left to drift. A periodic reconciliation task (Celery beat) recomputes actual storage usage per workspace prefix against the stored counter as a consistency check, following the same "verify the artifact, don't just trust the counter" discipline used throughout prior projects.
- Soft-deleted files still occupy storage. v1 rule: `storage_used_bytes` is decremented at soft-delete time and the objects are purged by a retention sweep (30 days), so the counter reflects what the user believes they are using. Reconciliation accounts for this window rather than reporting it as drift.

## 6a. Deletion and Restore

Soft delete exists so `audit_logs.target_id` always resolves (Schema.md §10). v1.4 makes it a user-visible trash as well:

- Every delete operation stamps one `deletion_batch_id` across every row it soft-deletes — the folder, its descendant folders, their files, and those files' versions.
- **Restore revives exactly one batch.** Restoring a folder does not resurrect items the user had deleted separately beforehand, because those carry a different batch id. Without this rule, restoring a folder would silently undo unrelated earlier decisions.
- A restore is refused if it would exceed the workspace quota, or if the parent it would return to is itself deleted.
- The retention sweep purges objects 30 days after `deleted_at`; after that the rows remain (for audit) but the bytes are gone and restore is no longer offered.

## 7. Deployment Architecture

**Compose layout (decided at kickoff, 7 Sep 2026):** this project ships its **own** `docker-compose.yml` under its own Compose project name, on the same VPS as the paused n8n project — *not* merged into the n8n project's compose file. Same isolation intent as `Rules.md` §7, safer mechanics: no edit to a live stack's compose file, independent `up`/`down`/restart, and no chance of an unrelated change taking n8n's services with it. The two stacks meet only at an **external Docker network** shared with the existing reverse proxy.

Services: `fileshare-backend`, `fileshare-celery-worker`, `fileshare-celery-beat`, `fileshare-frontend`, `fileshare-postgres`, `fileshare-redis`.

Redis is a **dedicated container**, not a shared instance with a distinct DB index — Celery's broker traffic and the isolation-from-n8n requirement both argue against sharing, and the memory cost of a second Redis is negligible.

No credential or data sharing with the n8n project's services — fully isolated except for the physical VPS and the shared reverse-proxy network.

Reverse proxy / HTTPS: reuse the VPS's existing reverse proxy if one is present from the n8n deployment; otherwise stand up new routing. Confirm actual VPS state at M0/M8.

**Frontend configuration is baked in at build time, not read at runtime.** Next.js inlines every `NEXT_PUBLIC_*` variable into the JavaScript bundle when `next build` runs. `FILESHARE_PUBLIC_API_BASE_URL` is therefore passed to the frontend image as a **build argument** (`build.args`), never as a runtime `environment:` entry — a runtime entry is silently ignored by browser code. Two operational consequences: changing the API origin requires `docker compose build frontend` rather than a restart, and the image must be built with the public HTTPS origin already set, so an image built locally cannot be promoted to the VPS unchanged. Corrected after M3; see §15.

## 8. Object Storage Configuration

**Provider: Supabase Storage via its S3-compatible endpoint** (`https://<project_ref>.storage.supabase.co/storage/v1/s3`), using SigV4 presigning and boto3. A **new, separate Supabase project** — never the one used by `ecommerce-ai-platform` — for the same isolation reason this project does not share credentials with the n8n deployment.

- **Key structure:** `workspace_id/file_id/version_id/filename` (per-workspace prefix — defense-in-depth isolation, and workspace deletion becomes a single prefix delete). **Unchanged by the provider switch**, which is the point of keeping key construction provider-agnostic.
- **Two sets of credentials, different jobs.** The *service role key* administers buckets over the Storage REST API and bypasses RLS entirely — server-side only, never sent to the browser. The *S3 access key/secret* (generated in the dashboard under Storage settings) signs S3 requests and is what produces presigned URLs.
- Bucket is **private** (`public: false`); no object is reachable without a signed URL.
- A per-bucket **file size limit** is set. On the Supabase free plan the account-wide ceiling is 50MB per file, which the API enforces at `upload-init` so oversized uploads fail cleanly and early.
- Provider **version history is not relied on** for the application's version chain — that lives in `file_versions` with its own keys. (Supabase has no S3 versioning at all, so this was already the right call.)

### What Supabase does not provide, and what replaces it

| AWS S3 mechanism | Supabase equivalent |
|---|---|
| Block Public Access | the bucket's `public: false` flag |
| Bucket CORS rules | **none** — no per-bucket CORS configuration exists |
| Default SSE / KMS | encryption at rest is provider-managed, not configurable |
| Lifecycle rules (abort incomplete multipart) | **none** — the Celery beat sweep in §2 is the only protection |
| Scoped IAM policy | S3 keys are project-wide; scope comes from using a dedicated project |

The CORS gap is the one with architectural consequence: §2 step 4 depends on the browser uploading parts directly. `scripts/bootstrap_storage.py` probes a presigned `PUT` preflight against the real bucket so this is settled by evidence before M3 commits to it.

### Free-plan operating limits

50MB per file · 1GB total storage · 5GB egress/month · **project auto-pauses after 7 consecutive days without a request**. The default workspace quota is set to 500MB so quota enforcement is genuinely exercised rather than pre-empted by the provider, and a daily `storage_keepalive` beat task prevents the pause from taking a demo offline. Both revert when the project returns to AWS S3.

## 9. Security Principles (cross-cutting)

- No object ID (file, folder, share link) is ever guessable/sequential in a way that matters — UUIDs throughout, and share link tokens are cryptographically random, not derived from the file ID.
- Every state-changing action is permission-checked server-side; the frontend never being shown a button is not a substitute for a backend check.
- Malware-flagged files are never served, even to the owner, until explicitly reviewed/cleared.
- Signed URLs for storage access are short-lived (minutes, not hours) and single-purpose (upload or download, not both).
- The Supabase **service role key bypasses RLS entirely**. It is server-side only, never sent to the browser, and handled in the same class as any other provider secret (Rules.md §3).
- JWTs are delivered in httpOnly, Secure, SameSite cookies — not localStorage — so an XSS bug cannot exfiltrate a session.
- File contents and credentials never appear in application logs, at any level.

## 10. Amendment Log — v1.1 (7 Sep 2026, pre-M0)

1. **§3 rewritten** around the VirusTotal 32MB/650MB submission ceiling and the free-tier community-sharing property, which v1.0 did not account for. Hash-lookup-first is now the primary path; `scan_skipped` covers files too large to submit. Without this, "every file scanned before being available" plus "large file support" were mutually unsatisfiable.
2. **§3 access-by-scan-status table added** — v1.0 never stated whether a `pending`/`scanning` file was downloadable.
3. **§4.1 inheritance rule specified** — v1.0 said a folder grant overrides the workspace default but not whether grants cascade, or which ancestor wins. Nearest-ancestor-wins is now explicit, with most-permissive-wins explicitly rejected.
4. **§4.2 modifier semantics** aligned with the Schema.md §9 type/modifier split.
5. **§4.4 added** — an unguessable token needs brute-force protection to stay unguessable.
6. **§2 abandoned-upload sweep added** — orphaned multipart parts are a real cost and consistency leak. *(Written when AWS S3 was the provider; the sweep became load-bearing rather than supplementary in v1.2.)*
7. **§7 compose layout decided** — own stack, shared proxy network; dedicated Redis rather than a shared instance.
8. **§8 added** — S3 bucket configuration was referenced throughout but never specified.

## 11. Amendment Log — v1.2 (7 Sep 2026, during M0): storage provider

**What changed and why.** AWS account setup was blocked externally — a declined payment card, then SMS OTP verification that would not deliver. Rather than block M0, the storage provider moved to **Supabase Storage on a new, separate project**. This is a sequencing decision, not a scope cut: **AWS S3 remains the intended long-term provider**, and the v1.1 architecture above was written for it.

**The choice that limited the blast radius.** Supabase Storage offers three upload paths — standard, resumable (TUS), and S3-compatible. Driving it through the **S3-compatible endpoint** was chosen over the native TUS path because presigned multipart upload then works exactly as designed: boto3 stays, per-part presigned URLs stay, the 8MB chunk size stays, and §2 needed no structural change. TUS would have forced a fixed 6MB chunk size, abandoned the presigned-part model, and required rewriting the upload flow twice — once now, once on the way back to AWS. As it stands, returning to AWS is close to a credentials-and-endpoint change.

**What genuinely changed:**

1. **§1, §2, §3.1, §4.3, §6** — "S3" replaced with provider-neutral language where the mechanism is unchanged.
2. **§2** — browser-direct upload now carries an explicit "verify before M3" flag, because Supabase has no per-bucket CORS configuration and the documentation cannot settle whether the preflight succeeds.
3. **§2** — the abandoned-upload sweep is now the *only* protection against orphaned parts, where on S3 a lifecycle rule was a second line of defense.
4. **§6** — the provider's own per-file ceiling is enforced at `upload-init` alongside the quota check.
5. **§8 rewritten** — AWS-specific controls (Block Public Access, bucket CORS, SSE config, lifecycle, scoped IAM) replaced with what Supabase actually offers, plus an explicit table of what has no equivalent.
6. **§9** — added the service-role-key handling rule.
7. **Default workspace quota lowered 5GB → 500MB** (PRD §10) so it fits inside the 1GB free-plan account and quota enforcement is actually reachable in testing.
8. **`storage_keepalive` beat task added** — free-tier projects pause after 7 days of inactivity.

**Consequence for §3 worth noting:** with a 50MB per-file ceiling, `scan_skipped` is unreachable — every file sits far below VirusTotal's 650MB limit. The state and its code path are retained deliberately, since both become correct again the moment the project returns to AWS S3.

## 12. Amendment Log — v1.3 (8 Sep 2026, start of M1)

1. **§4.0 added.** v1.2 specified authentication in a single line. Session lifetime, refresh rotation, reuse detection, logout semantics, SameSite choice, login throttling and enumeration resistance were all undefined at the point M1 had to implement them.
2. **§4.6 added.** §4.1 defined file access resolution but nothing defined who may administer a workspace — invite, change roles, delete. M1 could not be built without it.
3. **The last-owner and personal-workspace invariants written down.** Both were implied by Schema.md §2 but never stated as rules with a defined failure mode.
4. **Invitation acceptance bound to the invited email.** Previously unstated, and the permissive reading — anyone holding the link may accept — is a real privilege-escalation path.
5. **Invitation tokens moved out of URLs** after M1 manual validation showed raw tokens in the uvicorn access log. Documented in §4.6.
6. **A deployment constraint follows from the cookie choice** (§4.0): the API and frontend must share a registrable domain, or `SameSite=Lax` will not be sent. Recorded now so M8 does not discover it late.

## 13. Amendment Log — v1.4 (8 Sep 2026, start of M2)

1. **§6 rewritten: quota counts every retained version.** v1.3 left `files.size_bytes` ("current version") and the reconciliation task ("actual usage per workspace prefix") describing two different numbers, which would have made reconciliation report drift permanently. Schema.md §7 carries the matching rule and a 10-version retention cap, without which version history could exhaust a workspace.
2. **Quota check placed under a row lock.** Previously specified as a check with no concurrency story; two simultaneous uploads would both have passed it.
3. **§6a added — deletion batches and restore.** Soft delete was defined; restore was not, and the obvious implementation would resurrect files the user had deliberately deleted before the folder that contained them.
4. **§4.1 given implementation constraints** — recursive CTE rather than per-level queries, a 32-level depth cap, cycle rejection on move, and the rule that a grant on a soft-deleted row confers nothing. The ancestor walk sits on the hottest path in the system and had no cost or termination guarantees.
5. **Upload-init/upload-complete pulled into M2** (PRD §7 amended). A file cannot exist without its bytes, so leaving upload in M3 would have left M2 unverifiable end to end. M3 becomes the drag-and-drop frontend and upload progress UI.
6. **`file_permissions` scheduled into M2.** PRD §4 promised per-file/folder roles and §4.1 depends on them, but no milestone owned them; the ancestor walk they require is the same traversal the folder tree code performs.

## 14. Amendment Log — v1.5 (9 Sep 2026, start of M3)

1. **§2a added — resumable upload.** The upload flow described how to start and finish an upload but not how to recover one interrupted half way, and no endpoint exposed which parts had landed. `ListParts` support was verified empirically against the live bucket before designing on it, rather than assumed.
2. **The browser limitation is recorded rather than papered over.** Resume requires the user to re-select the file; a `File` object cannot survive a page reload. The design says so plainly so nobody later reads "resume" as automatic.
3. **Minimal authentication UI pulled into M3.** Every M1/M2 endpoint requires a session cookie, so without login pages the upload UI could be neither used nor manually validated — the standard every prior milestone has been held to. Visual polish stays in M7.

## 15. Amendment Log — v1.6 (9 Sep 2026, after M3): frontend build-time configuration

**Found by running it, not by reading it.** After the M3 frontend image was rebuilt, tracing how `FILESHARE_PUBLIC_API_BASE_URL` actually reached the browser showed that it never did. It was declared as a runtime `environment:` entry on the `frontend` service, but Next.js inlines `NEXT_PUBLIC_*` at build time. The stack appeared correct only because two independent defaults — the compose default and the `ARG` default in `frontend/Dockerfile` — were both `http://localhost:8000`.

Nothing was broken locally, and nothing would have failed loudly at M8 either. The deployed bundle would have called `localhost:8000` — meaning each visitor's own machine — and every request would have failed in the browser with no server-side log pointing at the cause.

Changes:

- `docker-compose.yml`: `NEXT_PUBLIC_API_BASE_URL` moved from `environment:` to `build.args`. The runtime entry was **removed** rather than kept alongside; leaving it would advertise a runtime knob that does not exist.
- `docker-compose.prod.yml`: records that the image must be built with the public HTTPS origin, and that a locally built image cannot be promoted unchanged.
- `.env.example`: the variable is annotated as build-time.
- §7 above states the rule.

Verified by building the image with a value deliberately different from the default, then confirming that exact string is present in the JS actually served to the browser and that the old default appears nowhere in it. Confirming only that the container starts would not have distinguished the fix from the coincidence it replaced.

## 16. Amendment Log — v1.7 (10 Sep 2026, start of M4): scan pipeline

§3 was rewritten rather than patched, because four decisions changed its shape. The
rate-limiting reference document supplied at the start of M4 was written for **inbound**
API-key management — keys we issue to consumers, with tiers, owners and revocation. M4
needs the mirror image: outbound budget management against one provider with one key we
hold ourselves. Only the counter pattern transferred; `api_keys`, `rate_limit_rules` and
`api_usage_logs` describe a feature this project does not have, and §3a records why each
was left out.

1. **"Tasks block on the limiter" was not implementable.** The original wording had tasks
   block until budget was available. That is tolerable for the per-minute window and
   catastrophic for the per-day one: at `concurrency=2`, two tasks waiting on an exhausted
   daily budget deadlock the whole pipeline for up to 24 hours. Replaced with
   reserve-or-requeue, which is what the reference document itself recommends.
2. **Budget exhaustion keeps a file `pending`, never `scan_skipped`.** Auto-promoting it
   would make budget exhaustion a scanning bypass anyone could trigger deliberately: burn
   the day's budget with junk, then upload the payload and have it served unscanned. The
   honest state costs a wait; the convenient one costs the security boundary.
3. **File content is no longer submitted by default (§3b).** Free-tier submissions are
   shared with the VirusTotal community, so the previous design published a customer's
   private file to a public pool whenever VirusTotal had not seen it before. Now
   configurable and defaulting to hash-lookup only. This makes `scan_skipped` a common
   state again, which contradicts a knock-on note in PRD §10 — that note has been
   corrected rather than left standing.
4. **`flagged` and `scan_failed` had no exit (§3d).** Both were described as lasting
   "until an admin review clears it", but no endpoint, field or milestone owned that
   review, so M4 would have shipped two permanently inaccessible states. A minimal
   override now lands in M4, recorded alongside the status rather than overwriting it —
   marking a flagged file `clean` would claim a re-scan that never happened.

Two further corrections found while reading §3 against the code that M2 actually shipped:

5. **The checksum was trapped behind the scanning flag.** §3 computed `checksum_sha256`
   inside the scan task, so with `scanning_enabled=false` the integrity checksum was
   never computed at all. Checksums are an integrity feature and now run on every
   completed upload, independently.
6. **Step 9 described work that no longer exists.** It had the scan task chain into
   version processing to set `files.current_version_id`, `size_bytes` and `mime_type`;
   `complete_upload` has set those synchronously since M2, long before any scan runs. The
   step was removed and the actual division of labour stated.
