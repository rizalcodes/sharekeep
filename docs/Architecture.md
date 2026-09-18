# Architecture: Secure File Sharing Platform — System Design

**Scope note:** system/service design — how components interact, the background job pipeline, security architecture. Not data structure (`Schema.md`) or naming/conventions (`Rules.md`).

**Version:** v1.14 — the VPS as found, the proxy and Cloudflare, at the start of M8b, 18 September 2026 (see §23). Previously v1.13 (corrections from building M8a, §22). Previously v1.12 (production deployment, §21). Previously v1.11 (readiness semantics, §20). Previously v1.10 (search, version rollback and the trash, §19). Previously v1.9 (audit-log reading and the notification pipeline, §18). Previously v1.8 (share-link resolution, §17). Previously v1.7 (scan pipeline, §16). Previously v1.6 (frontend build-time configuration, §15). Previously v1.5 (resumable upload, §14). Previously v1.4 (M2 content model, §13). Previously v1.3 (M1 auth, §12), v1.2 (storage provider, §11) and v1.1 (kickoff, §10).

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
7. If `flagged`: the file is **not** deleted automatically (same "don't auto-delete on an uncertain signal" principle used in the n8n project's stale-SKU handling). It is marked flagged, all access is blocked, an `audit_logs` entry (`scan_flagged`) is written, and a notification is queued to the uploader and every workspace owner (section 5a). The only exit is an explicit admin override — §3d.
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

Without an exit, `flagged` and an exhausted-retry `scan_failed` are permanent dead ends: the file is inaccessible to everyone, including its owner, forever. A workspace **owner** may therefore override a specific version. (Owner is the most privileged of the three roles — viewer, editor, owner — and there is no separate admin role; earlier drafts of this section said "owner or admin", which implied one.)

**The override does not rewrite history.** `scan_status` stays `flagged` or `scan_failed`; the override is recorded separately (`scan_override_at`, `scan_override_by`, `scan_override_reason` — Schema.md §7) and is what grants access. Setting the status to `clean` instead would be a lie: nothing re-scanned the file and nothing pronounced it safe. The UI must keep showing that the file was flagged **and** that a named person chose to release it.

Every override is written to `audit_logs` with the new `scan_override` action, including who, when, and the stated reason. An override applies to one version only — it does not carry to a later upload of the same file.

**Access rule by scan status** (the single source of truth for whether bytes may be served):

| `scan_status` | Downloadable? |
|---|---|
| `pending`, `scanning` | No — the UI shows the file with a scanning or "queued for scanning" badge, but no download URL is issued |
| `clean` | Yes |
| `scan_skipped` | Yes, with a persistent "not scanned" badge |
| `flagged` | **No** — including to the uploader — unless an owner override is recorded on that version (§3d) |
| `scan_failed` | No, until a retry succeeds or an owner override is recorded |

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
- **The client must refresh, and must not race itself (found at M8a).** This mechanism existed from M1 and the frontend never called it: fifteen minutes after signing in, when the browser discards the access cookie, every action failed until a reload sent the person back to sign-in, and the 14-day refresh token went unused. The client now refreshes once on a 401 and repeats the request. Because reuse detection above revokes *every* session, two refreshes racing with the same cookie would sign the person out everywhere, so the refresh is serialised: one shared promise per page, and the Web Locks API across tabs. A tab that waited sends the cookie the first tab already rotated, which is valid.
- **Logout** revokes the refresh row, so it genuinely ends the session rather than clearing a cookie while the token stays valid.
- **`SameSite=Lax`**, not `Strict`: a `Strict` cookie is withheld on the cross-site navigation that clicking an emailed invitation link produces, which would break invitation acceptance. Lax blocks cross-site POST, which is the CSRF case that matters.
- **Deployment constraint this creates:** a Lax cookie is only sent when the API and frontend share a registrable domain. `app.example.com` + `api.example.com` works; two unrelated domains would force `SameSite=None` and reopen CSRF. M8 must deploy them as one site. **Resolved at M8:** the frontend is served at `https://sharekeep.xyz` and the API at `https://api.sharekeep.xyz` (`www` redirects to the apex). Auth cookies stay host-only on the API host; the frontend never reads a cookie server-side (verified at M8).
- **CSRF token (specified in v1.12, M8).** Lax blocks a cross-site POST in the browser, but not a request from another subdomain of the same site, and not a top-level GET. Every **unsafe method** (`POST`, `PUT`, `PATCH`, `DELETE`) on the API therefore requires `X-CSRF-Token`, compared in constant time against an httpOnly `fileshare_csrf` cookie. The token is 32 bytes of CSPRNG output, returned in the JSON body of sign-in, sign-up and refresh and from `GET /api/v1/auth/csrf`; the frontend holds it in memory and fetches it again after a reload. **Not bound to the session**, as first specified: there is no identifier present on every request to bind it to — the access cookie is discarded by the browser after fifteen minutes and the refresh cookie is scoped to `/auth` — so binding would have failed exactly when a session needed refreshing. What defeats a planted cookie instead is the **`__Host-` prefix**, which a browser refuses to honour with a `Domain` attribute, so no sibling subdomain can set it; and the token is **replaced at every sign-in and sign-out**, which is what login CSRF needs (verified at M8a). `GET /auth/csrf` also issues a **pre-session** token, so sign-in and sign-up are protected against login CSRF. No exemptions: the public share page is this frontend too, so it fetches a token like any other page. A refusal is 403 with a stated reason.
- **No state-changing GET.** Issuing a download URL writes an audit entry, which a cross-site top-level navigation could otherwise forge in a victim's name; at M8 it becomes a `POST`. The share preview stays `GET`: it records only denials, and forging one requires already holding a real token.
- **Sign-up is gated in production (v1.12, M8).** When `FILESHARE_ENVIRONMENT=production`, sign-up succeeds only for an address on `FILESHARE_SIGNUP_ALLOWLIST` (comma-separated, may be empty) or with a valid `invitation_token`, which already fixes the email to the invitation's address. The refusal says sign-up is by invitation; it does not reveal whether an address is registered, keeping the enumeration rule below. Local and staging stay open.
- **Login throttling:** per-IP and per-email, in Redis. §4.4 requires this for share links; the same reasoning applies to a password endpoint, and `login_failed` already exists in the audit enum.
- **Enumeration:** login failures are indistinguishable whether the email exists or not, and signup does not reveal whether an address is already registered through a differing response.

### 4.1 Effective role resolution (authenticated user)

**Membership is a precondition, checked first** (v1.10). A grant refines a member's role; it never admits a non-member. Enforced at both layers — the grant routes refuse a non-member, and the resolver denies one regardless of any grant row — because §4.8's argument that search needs no per-row permission filter rests on it.

Then, resolved in strict precedence order — **first match wins**, more specific always beats less specific:

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
- `private`: requester must be authenticated AND match `allowed_user_id`. A private link therefore grants access to a file **without** workspace membership — that is the point of sharing — and the grant is to exactly one named user, not to anyone who happens to hold the token.

**Modifiers apply on top of any type** (Schema.md §9): if `expires_at` is set it is enforced regardless of type; if `hashed_password` is set the password is required regardless of type; if `max_downloads` is set, `download_count < max_downloads` is enforced. So a password-protected link that also expires enforces both. Validation must therefore *accept* combinations like `public` + `expires_at`; rejecting them would recreate the single-enum reading that Schema §9 v1.1 exists to correct.

Applied to every link regardless of type: `revoked_at IS NULL`, and the underlying file's `deleted_at IS NULL`.

**The token is stored hashed** (v1.8). `share_links.token_hash` holds SHA-256 of the raw token; the raw value is shown once at creation and never persisted, matching what M1 already does for refresh tokens and invitations. A leaked database, backup, or read-only injection therefore yields no working share links. The cost is stated rather than hidden: **a link cannot be copied again later.** The owner sees it once; recovering it means creating a new link and revoking the old one.

**Scan gate, and the one place it deliberately differs from §3.** A share link resolves only if the file's current version is downloadable under §3 — *except* that an owner's scan override does **not** carry to share links. A flagged version released by an override stays unreachable through every link type.

This is a second, stricter rule rather than a reuse of `scan.version_is_downloadable`, and it is written here explicitly so it is not mistaken for accidental duplication. The reasoning: an override is a workspace owner accepting a risk **for their own workspace**. Letting that same decision feed a public link turns "I accept this risk" into "strangers accept this risk on my word", and the people bearing it never see the flag. The override remains what it is for members; it simply does not become a distribution channel. `shares.version_is_shareable` is the single implementation of the stricter rule, and it is defined in terms of the §3 function so the two cannot drift apart silently.

**A link tracks the file, not a fixed version.** `share_links.file_id` targets the file, so a link always serves the current version. Uploading a new version changes what a previously-shared link delivers, and if that new version is `pending` the link correctly stops working until its scan resolves. This is the same behaviour a reader expects from Drive or Dropbox, but it means a link is not a promise about specific bytes.

**`max_downloads` counts resolutions, not completed downloads.** The bytes are fetched straight from object storage (Rules.md §3 — the API never proxies file content), so a completed download is not an event this system can observe. The counter increments when a signed URL is issued, under a row lock so two simultaneous requests at `max_downloads - 1` cannot both succeed. Two consequences, both stated in the UI rather than papered over: a visitor who requests the link and never finishes the download still consumes one, and a single issued URL can be replayed until it expires. The field is presented to users as a limit on link *uses*, because that is what it is.

### 4.3 Serving the bytes

On successful resolution, generate a short-lived signed GET URL, download-scoped only. Never a long-lived or public storage URL, and never a URL that also permits upload.

**Share links use their own, shorter TTL** (`signed_share_url_ttl_seconds`, default 60s) rather than the 300s used for authenticated downloads. The reason is revocation: once a signed URL has been handed out, revoking the share link cannot recall it, so the TTL is the width of the window in which a revoked link still works. Sixty seconds is close to free, because the TTL governs when the transfer must *start* — a download already in flight is not interrupted when the signature expires. **The remaining window is a real property of presigned URLs, not something this design eliminates**, and it is documented rather than implied away.

**Verified against the live bucket on 11 September 2026**, because §4.3 makes claims that are the provider's to keep, not ours:

- `ResponseContentDisposition` **is** honoured: the response carries `attachment; filename="…"`, so "download-only" is a fact rather than a hope.
- Supabase returns `content-type: text/plain` regardless of the `ContentType` the object was stored with. A shared `.html` or `.svg` therefore will not execute in the visitor's browser even if the disposition header were absent — a second, independent layer. The corollary is that `files.mime_type` is metadata for our own UI and **not** what storage serves.
- A `PUT` against a download URL is refused with 403, so the signature really is GET-scoped.
- An expired signature is refused with HTTP **400** (not 403), which is what error mapping must expect.
- The signed URL discloses the whole storage key — workspace, file and version UUIDs plus the original filename — to the recipient. Low severity, since every workspace endpoint independently refuses non-members, but a share recipient is by definition outside the workspace, so it is recorded rather than left unnoticed. Removing it would mean proxying bytes through the API, which Rules.md §3 rules out for good reasons.

### 4.4 Rate limiting (inbound)

This section covers traffic arriving at us. Our own outbound consumption of the VirusTotal API is a separate mechanism with separate limits — see §3a.

Share-link token endpoints are rate-limited per IP — an unguessable token is only unguessable if you cannot try millions of them. Password attempts on a password-protected link are separately throttled per token, so one link under attack cannot be unlocked by brute force and cannot be used to exhaust an unrelated visitor's IP budget either.

**Which address is the client (specified in v1.12, M8, after a demonstrated bypass).** Behind a reverse proxy the TCP peer is the proxy, so the client address has to come from `X-Forwarded-For`, and the *leftmost* entry is whatever the client chose to send: a proxy appends to that header, it does not replace it. Until M8 the code took the leftmost entry. Demonstrated against the share-token limiter: one fixed claimed address was refused at attempt 31 as designed; a different claimed address per request made **80 attempts with no refusal**, even with a proxy-style entry appended after it. That defeated every per-IP limit and let a visitor write any IP they liked into the audit log. The fix trusts exactly `FILESHARE_TRUSTED_PROXY_HOPS` proxies: the client is the entry that many positions from the right, and with the default of `0` the header is ignored and the peer address is used. Production sets it to the number of proxies actually in front of the API, confirmed on the VPS rather than assumed; a DNS-level proxy such as Cloudflare would be a second hop.

Both limiters use `core.rate_limit`, which **fails open** on a Redis outage. That is the right trade for inbound throttling — a Redis problem must not take down sharing for everyone — and it is the opposite of the outbound budget in §3a, which fails closed because there the failure mode is spending someone else's quota. The two are different mechanisms for that reason.

### 4.5 Audit

Every resolution attempt against a share link that **exists** is written to `audit_logs` with `share_accessed` or `share_access_denied`, capturing `ip_address`, `user_agent`, and the denial reason in `metadata`. That includes every interesting failure: right token and wrong password, expired, revoked, download limit reached, wrong user on a private link, or a file whose scan status withholds it.

**A token matching no link writes no audit row** (v1.8). The volume there is entirely attacker-controlled: brute-forcing tokens would otherwise append unbounded rows to our own audit table, and since the inbound limiter fails open, a Redis outage would remove the only bound. There is also nothing truthful to write — no `workspace_id` to attribute it to and no `target_id` that resolves. Unknown-token attempts are instead visible in the rate limiter and the application log, which is where high-volume noise belongs. The audit table keeps its forensic value precisely by not being writable at will by anyone who can guess a URL shape.

`share_created` and `share_revoked` are audited with the acting user, as workspace-scoped events.

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

**An owner may lower their workspace's quota, never raise it past the platform's**
(v1.10, decided at M7). The quota lives on the workspace (PRD §8), but a limit its own
owner can raise without bound is not a limit: before M7 an owner could set any value,
including one far beyond the 1 GB bucket every workspace shares. The accepted range is
from current usage up to `default_storage_quota_bytes`; raising the platform ceiling is a
configuration change, not a workspace setting. Found when designing the settings page,
since that is what would have exposed it.

**A member can leave a workspace** (v1.10). Invariant 1 below already spoke of an owner
not being "allowed to leave", but nothing let anyone leave: a person invited into a
workspace had no way out except asking an owner to remove them. `POST
/workspaces/{id}/leave` removes the caller's own membership, subject to both invariants.

Two invariants, both enforced server-side and both tested adversarially:

1. **A workspace always has at least one owner.** The last owner cannot be demoted, removed, or allowed to leave. Otherwise the workspace is orphaned and nobody can ever administer it again.
2. **A personal workspace cannot be deleted** while its user exists (Schema.md §2), and its owner cannot be removed. It may still have other members invited into it — `is_personal` governs deletion, not collaboration.

**Invitation tokens never travel in a URL.** The preview endpoint is `POST /invitations/preview` with the token in the body, not `GET /invitations/{token}` — a web server writes full request paths into its access log verbatim, and an invitation token sitting in a log is workspace access for anyone who can read that log. Found during M1 manual validation by inspecting real container logs; the log redaction filter covers the same shape as a second layer.

**The emailed invitation link carries its token in the URL fragment** (`/invitations#token=…`), specified in v1.9 after M6 was first marked done. The first M6 implementation put it in the query string — which is exactly what the rule above forbids, since the frontend server and the VPS reverse proxy log a query as faithfully as a path — and pointed at a page that did not exist, so every invitation email linked to a 404. A fragment is never sent to any server, so no access log on any hop can record it. The acceptance page reads the fragment, removes it from the address bar at once so it does not linger in history or on a shared screen, holds it in `sessionStorage` for the length of a sign-in or sign-up, and only ever sends it to the API in a POST body. The signup form shows the invited address locked rather than editable, because the server takes the address from the invitation and ignores the typed one — an editable field would let someone believe they had registered a different address.

Invitation acceptance is bound to the invited address: a signed-in user whose email differs from the invitation's is **rejected**, never silently accepted. An invitation is a grant to one address, not a bearer token for whoever opens the link.

### 4.7 Reading the audit log

Specified in v1.9 (M6). §4.6 enumerated who may *administer* a workspace but said nothing
about who may *read* its history, and the permissive reading is a privacy leak rather than
a convenience: `audit_logs` holds the IP address and user agent of every share-link
visitor, plus the full activity pattern of every member.

**Workspace owners only.** Editors and viewers are refused 403, not 404 — the endpoint's
existence is not a secret, only its contents. This matches §4.6, where everything
administrative belongs to the owner alone.

Account-level events (`login`, `login_failed`, `logout`) carry `workspace_id = NULL` and
therefore appear in **no** workspace log. They are a property of the account, not of a
workspace; surfacing them to a workspace owner would tell them when a collaborator signed
in to work that has nothing to do with them. A per-account session history is a separate
surface, and not in v1.

**The log is read as history, never as a live join.** Rendering `target_id` by looking the
row up at read time would make an append-only table effectively mutable — renaming a file
would silently rewrite what the log says was downloaded last week. Every write therefore
denormalises a human-readable label into `metadata` at the moment of the event, and the
reader displays that stored label. Rows written before this rule was stated (M1–M5) do not
all carry one; the interface says the name was not recorded rather than inventing one or
resolving it live.

Pagination is **keyset on `(created_at, id)` descending**, not offset. The table is
append-only and time-ordered, so a cursor is both cheaper and stable under concurrent
writes, where an offset shifts every time a new row lands. The `(workspace_id, created_at)`
index added in M1 serves exactly this query.

### 4.8 Search (specified in v1.10, M7)

PRD §4 scoped search to "filename/metadata/tag" and ruled out document contents. The
architecture had no section on it at all; this is that section.

**What is searched.** The typed query matches the file **name or any of its tags**,
case-insensitively, by substring — decided at M7b, because §4.8 first said the query
matched only the name, which would have meant typing "invoice" found nothing for a file
named `q3.pdf` tagged `invoice`, while PRD §4 promises "filename/metadata/tag search".
The **tag filter** stays exact on the normalised tag, so precise narrowing is still
possible; the query is the forgiving way in, the filter is the precise one.

**Several tag filters mean AND** (M7b): selecting `invoice` and `2026` returns files
carrying both. Every other control on that page narrows, and a filter that widened the
result set as it was added would be the one surprise among them. **Metadata**, which the PRD never defined, is defined here as five
filters, all combinable with each other and with the text query:

| Filter | Source | Notes |
|---|---|---|
| Type | `files.mime_type`, mapped to a category (document, spreadsheet, presentation, image, video, audio, archive, other) | A category, not a raw mime string: nobody searches for `application/vnd.openxmlformats-officedocument.wordprocessingml.document`. The mapping lives in one server-side function so the filter and the result label cannot disagree. |
| Size | `files.size_bytes` | The **current** version's size, which is what a reader means by "how big is it". |
| Modified | `files.updated_at` range | |
| Uploaded by | `files.created_by` | Any member of the workspace. |
| Scan status | `files.scan_status` | Lets an owner find everything flagged or waiting in one step. |

Folders match by name too, so a search for "invoices" finds the folder as well as files
called that. Every result carries its location as a breadcrumb, because a result list that
does not say *where* each file lives forces the reader to open each one to find out.

**Folders drop out as soon as a file-only filter is used** (M7b). A folder has no type,
no size and no scan status, so it can neither pass nor fail those filters. Returning it
anyway would put two different rules in one list without showing which applied to what —
a folder surviving a "flagged only" search reads as a claim about the folder. The result
page says that those filters apply to files, rather than leaving the absence to be
noticed.

**Who sees what.** Search is scoped to the active workspace and requires membership, the
same dependency every workspace endpoint uses. That is sufficient, and the reason is worth
stating rather than assuming: roles are only `viewer`, `editor` and `owner`, so an explicit
grant can narrow a member's role on an item but can never hide it — every member resolves
to at least `viewer` on every file (§4.1). Visibility in this system is therefore
membership, and search needs no per-row permission join. If a "no access" role is ever
added, this section is the first thing that must change.

This argument depended on a rule the code did not enforce until M7: that a grant can only
be held by a member (§4.1, Schema §8). Before M7 the grant route accepted any user, and the
resolver honoured a grant without checking membership. Nothing could use it, because every
workspace endpoint refused non-members first — but search must not become the first
endpoint that honours one.

**What is never returned.** Soft-deleted files and folders; files whose containing folder
chain includes a deleted folder; and files with no completed upload (`current_version_id
IS NULL`), which are uploads in flight rather than files.

**Matching.** `ILIKE '%term%'` cannot use a B-tree index, so the name column gets a trigram
GIN index (`pg_trgm`, shipped with the official Postgres image). Tag filtering uses the GIN
index on `files.tags` that has existed since M2. Results are ordered by relevance — exact
name match, then prefix, then substring — and then by `updated_at`, and paged by keyset
like the audit log. The query string is capped at 200 characters and the `%`/`_`
wildcards in it are escaped: a user typing `50%` is searching for those characters, not
asking the database to match everything.

### 4.9 Version rollback (specified in v1.10, M7)

PRD §4 promised "rollback to a prior version". It was never built: `current_version_id`
was only ever assigned at upload completion, and PRD §10b's note that M2 "delivered
versions … and restore" referred to restoring from the trash. The audit action that trash
restore writes is named `version_restored`, which is how the two were confused.

**Restoring version *k* copies it forward as a new version.** The object is copied
server-side (`CopyObject`, verified against the live bucket before this was written: the
copy is byte-identical and keeps its content type; `UploadPartCopy` also works for objects
above the single-request copy limit), and a new `file_versions` row becomes current with
`restored_from_version_id` pointing at *k*. History stays linear and is never rewritten;
"current" is always the newest version; and a share link, which tracks the file, serves
the restored content from then on.

Consequences stated rather than discovered:

- **It consumes quota and a retention slot**, exactly as re-uploading the same bytes
  would. Refused before any copy if the quota cannot absorb it.
- **The scan verdict is reused, not re-requested.** The content is identical, so the
  checksum is too, and the verdict cache (§3c) answers at zero VirusTotal cost. A copy of
  a version that is `flagged` is refused: rollback must not be a way to launder a flagged
  file into a new version that looks unscanned.
- **Requires editor**, like any upload. Audited as `version_rolled_back`, distinct from
  `version_restored` (trash), with both version numbers in the metadata.

## 5. Notification Pipeline

Trigger events create a `notifications` row (`status = pending`); a Celery task delivers it
through Resend and moves it to `sent` or `failed`. The schema stays channel-agnostic
(`notifications.channel`) so a `websocket` channel can be added in v1.1 with a new delivery
task, and no schema change or trigger-side edit.

**Queueing joins the caller's transaction; sending does not.** The row is added to the
transaction that caused the event, so an action that rolls back cannot leave behind a
notification claiming it happened — the same rule `audit.record` follows. Delivery is
dispatched only after that transaction commits.

### 5a. What triggers what, and who receives it (specified in v1.9)

| Event | Recipient | Reasoning |
|---|---|---|
| `workspace_invitation` | the invited address | The only channel by which an invitation can reach someone who has no account yet. M1 returned the raw token in the API response as an explicit stopgap; this replaces it. |
| `file_shared` | the named recipient of a **private** link | Public, password-protected and expiring links have no recipient. Emailing the person who just pressed "create link" is noise, not a notification, so those three send nothing. |
| `scan_flagged` | the uploader **and** every workspace owner | Deduplicated when the uploader is an owner. The uploader needs to know their file is withheld; the owners need to know because §3d makes them the only people who can release it. Notifying one without the other leaves either the decision-maker uninformed or the affected user mystified. |
| `permission_granted` | the user who gained access | Sent when a file- or folder-level grant is created or **raised**, not when it is lowered or revoked. Being told you lost access is a different message, and v1 does not send it. |
| `storage_quota_warning` | every workspace owner | Fires when usage crosses `storage_quota_warning_ratio` (0.9) **upward** — once per crossing, not on every upload after it. |

**A quota warning is an edge, not a state.** The ratio sat in the configuration from M0 and
was never read by anything. Wiring it naively to "usage ≥ 90%" would email the owners on
every upload for the rest of the workspace's life. The trigger is the *transition*: a
notification is queued only when usage before the reservation was below the ratio and usage
after it is at or above. Deleting files back under the threshold re-arms it.

### 5b. Delivery, and what `sent` actually means

The Resend key in use is a **restricted, send-only** key: `GET /emails/{id}` returns 401,
as do `/domains` and `/api-keys`. We cannot read back whether a message was delivered,
opened or bounced.

`status = sent` therefore means **"the provider accepted it"**, and nothing more. The
provider's message id is stored in `provider_message_id` so that a delivery question can be
answered by a human from the Resend dashboard — the column exists precisely because our own
record cannot answer it. Overstating this would repeat the mistake §4.2 avoided when it
named the share-link counter "uses" rather than "downloads": the interface must not promise
an accounting the architecture cannot deliver.

Failures are separated by kind, because they demand different things:

- **Transient** (429, 5xx, connection failure) — retried with backoff. `attempts` and
  `last_error` live on the row rather than only inside the Celery retry, so a worker
  restart does not lose the count and the sweep can pick the row up again.
- **Permanent** (422, and the 403s in §5c) — recorded `failed` with the provider's own
  wording in `last_error`, and **never retried**. Retrying a refusal that is a property of
  the configuration only burns free-tier budget.
- **Not configured** — no API key at all. The row stays `pending` rather than being marked
  failed, because nothing was wrong with the notification. This is the same distinction
  `virustotal.py` draws between a transient error and a missing key.

**Delivery is idempotent at the row.** A task that succeeds at the provider and then dies
before committing would otherwise send again on retry. `sent_at` is the guard: a row that
already carries one is never sent twice, and the claim is taken under a row lock.

### 5c. The sandbox sender, and why mail is redirected in development

Verified against the live API before designing on top of it, because none of this is
inferable from the documentation for *this* account:

- Sending from an unverified domain is refused **403**. `noreply@example.com` — what the
  configuration actually shipped with — would have failed every single send. The sandbox
  sender `onboarding@resend.dev` is accepted.
- Without a verified domain the **only** permitted recipient is the account owner's own
  address; anyone else is refused 403, with that address named in the error. A notification
  to any other user is therefore a permanent failure until a domain is verified.
- Resend's simulator addresses (`delivered@resend.dev`, `bounced@resend.dev`) are accepted
  despite not being the owner, which is the only reason the delivery and failure paths are
  testable at all.
- Six sends with no pause drew no 429, though the documented ceiling is 2/second. The
  limiter is built to the documented figure rather than the observed one.

Because of that recipient restriction, `notification_redirect_to` exists: when set, every
message goes to that address instead, and the rendered body carries a banner naming the
recipient it was actually for. Without it, the only notification that could ever succeed is
one already addressed to the account owner, and five of the six trigger paths would never be
exercised end to end. It is **off unless explicitly set**, and the fact that a message was
redirected is recorded on the row — a redirected send is never mistaken for a real one.

**Production sender (v1.12, M8).** Production sends from `notifications@mail.sharekeep.xyz`, on a sending subdomain verified in Resend (SPF, DKIM and MX records at Porkbun), and `notification_redirect_to` is unset. A subdomain rather than the apex keeps any sending-reputation damage away from the domain the product is served from. Email links are built from `FILESHARE_FRONTEND_ORIGIN=https://sharekeep.xyz`; a production deployment still pointing it at `localhost` would send every recipient to their own machine, so the production settings check refuses to start with a non-HTTPS or localhost origin.

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

## 6b. The trash (specified in v1.10, M7)

§6a promised a user-visible trash; there was no endpoint to list it. `GET
/workspaces/{id}/trash` returns deletion batches, newest first — one entry per delete
operation, not per row, because restore works per batch and a list of individual rows
would offer choices the restore cannot honour. Each entry names what was deleted (the
folder or file the user actually chose), how many items it holds, who deleted it and when,
and whether it can still be restored — refused once the retention sweep has purged the
bytes, or when the parent it would return to is itself deleted, stating which.

Visible to editors and owners, the roles that can delete. A viewer cannot restore
anything, so a list of things they cannot act on would be noise.

**The retention sweep §6a describes did not exist.** `soft_delete_retention_days` had
been in the configuration since M2 with no reader. Deleting released quota at once, but
the bytes stayed in the bucket forever — so real bucket usage drifted above what quota
accounted for, on a free plan with a 1 GB ceiling. A daily beat task now deletes the
objects of versions deleted more than 30 days ago and stamps `file_versions.purged_at`,
which is what makes the sweep idempotent and lets restore refuse cleanly: a batch with
any purged version says the files are gone for good rather than reviving rows whose
bytes no longer exist.

## 6c. Deleting a workspace (specified in v1.10, M7)

Deleting a workspace removes its rows outright — it is the one delete in this system that
is not soft — and until M7 it left **every object in storage with nothing left to find
them by**: the retention sweep looks for `file_versions` rows, and the cascade had just
deleted them. Worse than the leak §6b fixed, because that one is at least discoverable.

PRD §8 chose per-workspace key prefixes (`workspace_id/file_id/version_id/filename`)
partly for "simpler workspace-deletion cleanup". That is what this is. After the rows
are committed, a Celery task walks the workspace's prefix and deletes each object.

**Objects are deleted one request at a time.** Verified against the live bucket:
paginated `list_objects_v2` under a prefix works, and `delete_objects` — S3's batch
delete — is **refused** by Supabase. So a workspace with a thousand retained versions
costs a thousand requests, which is why this is a background task and not part of the
request that returns 204.

The residual risk is stated rather than designed away: if the task and its retries all
fail, those objects are orphaned with no row referring to them. The task is idempotent
(deleting an absent key succeeds on S3), so a re-run is always safe.

## 7. Deployment Architecture

**Compose layout (decided at kickoff, 7 Sep 2026):** this project ships its **own** `docker-compose.yml` under its own Compose project name, on the same VPS as the paused n8n project — *not* merged into the n8n project's compose file. Same isolation intent as `Rules.md` §7, safer mechanics: no edit to a live stack's compose file, independent `up`/`down`/restart, and no chance of an unrelated change taking n8n's services with it. The two stacks meet only at an **external Docker network** shared with the existing reverse proxy.

Services: `fileshare-backend`, `fileshare-celery-worker`, `fileshare-celery-beat`, `fileshare-frontend`, `fileshare-postgres`, `fileshare-redis`.

Redis is a **dedicated container**, not a shared instance with a distinct DB index — Celery's broker traffic and the isolation-from-n8n requirement both argue against sharing, and the memory cost of a second Redis is negligible.

No credential or data sharing with the n8n project's services — fully isolated except for the physical VPS and the shared reverse-proxy network.

Reverse proxy / HTTPS: reuse the VPS's existing reverse proxy if one is present from the n8n deployment; otherwise stand up new routing. Confirm actual VPS state at M0/M8.

**The VPS as found (v1.14, M8b, 18 Sep 2026).** Every earlier version of this section assumed this project would join an existing reverse proxy belonging to the paused n8n deployment, meeting it at an external Docker network. That is not the machine we have. The Contabo host (Ubuntu 24.04, 4 vCPU, 7.8 GB RAM, 94 GB free) had **no Docker installed, nothing listening on 80 or 443, and no n8n containers**. There is no proxy to join, so this project stands one up — and the isolation intent survives the change by keeping it *separable*: Caddy runs from its own Compose project under `deploy/proxy/`, on an external `proxy` network that `docker-compose.prod.yml` already expects, so a future stack (n8n returning, or anything else) joins that network without touching this one's compose file.

**Cloudflare sits in front (v1.14, M8b).** `sharekeep.xyz` uses Cloudflare nameservers with the proxy enabled on the apex, `api` and `www` records, so the request chain is client → Cloudflare → Caddy → container. Three consequences, each settled rather than assumed:

- **`FILESHARE_TRUSTED_PROXY_HOPS = 2`.** Cloudflare writes the visitor's address into `X-Forwarded-For` and Caddy appends Cloudflare's edge address, so the client is the second entry from the right (§4.4). Verified against a real request in M8c rather than reasoned about here.
- **The origin is firewalled to Cloudflare's ranges, and that is part of the §4.4 fix rather than hardening on top of it.** The origin address is public, so anything able to reach port 443 directly can present a request that never passed through Cloudflare and invent as many `X-Forwarded-For` entries as it likes — which would hand it whatever client address it wanted and undo the per-IP limits M8a restored. `ufw` therefore admits 80 and 443 only from Cloudflare's published ranges, and SSH separately.
- **TLS terminates twice, and the origin leg is real TLS.** Caddy obtains and renews its own Let's Encrypt certificate over HTTP-01, which Cloudflare forwards to the origin even while proxying, and Cloudflare's SSL mode is **Full (strict)**. Flexible mode was rejected outright: it would carry session cookies and file bytes unencrypted over the last leg while the browser still showed a padlock, which is the exact class of dishonesty this product exists to avoid.

**Hosts (v1.12, M8):** `sharekeep.xyz` to `fileshare-frontend:3000`; `api.sharekeep.xyz` to `fileshare-backend:8000`; `www.sharekeep.xyz` permanently redirects to the apex. TLS terminates at the proxy, and HSTS is sent for both hosts. The frontend carries a Content-Security-Policy with a **per-request nonce**, minted in `proxy.ts` (Next.js renamed the middleware convention at 16) with the root layout forcing dynamic rendering — a page rendered at build time has no request to take a nonce from, and its hydration script would then be refused by its own policy. `connect-src` names only the app, the API host and the storage origin: upload parts go browser-direct to storage (§2 step 4), while a download is a top-level navigation to a signed URL, which `connect-src` does not govern. The storage origin therefore joins the API origin as a frontend **build argument**.

What the policy defends and what it does not, established by running it at M8a: an injected inline event handler does not run and the page cannot reach an origin the policy does not name, while `'strict-dynamic'` — which Next needs to load its own chunks — deliberately trusts a script created by another script. The vector that matters here is injected markup, because this product renders file and folder names other people chose. `'unsafe-eval'` is absent in production. `style-src-attr 'unsafe-inline'` remains for two progress bars that set their width through a style attribute, which cannot execute script.

**Who operates the VPS (v1.12, M8).** The developer runs every step on the VPS; Claude prepares each one as a checked script in `deploy/` and verifies the result from outside: DNS, TLS, headers, cookies, CORS, the readiness endpoint, a real browser. No agent account exists on the VPS. An account able to run Docker there is root over the n8n project's containers and volumes too, which the isolation rule above exists to prevent.

**Backups (v1.12, M8).** Postgres is dumped nightly (`pg_dump` custom format) and seven days are kept on the VPS. The newest dump is encrypted to a public key before it leaves the VPS and copied to a private `fileshare-backups` bucket; the private key is held off the VPS, so neither a compromised VPS nor the storage keys can read a backup. Stored *files* are not duplicated (the free tier has no room), but the database is what makes them reachable: without it every object is an orphan, as the local database loss at M7 demonstrated. **A backup counts only once it has been restored:** M8's verification restores the latest dump into an empty database and compares row counts with the live one.

**Frontend configuration is baked in at build time, not read at runtime.** Next.js inlines every `NEXT_PUBLIC_*` variable into the JavaScript bundle when `next build` runs. `FILESHARE_PUBLIC_API_BASE_URL` is therefore passed to the frontend image as a **build argument** (`build.args`), never as a runtime `environment:` entry — a runtime entry is silently ignored by browser code. Two operational consequences: changing the API origin requires `docker compose build frontend` rather than a restart, and the image must be built with the public HTTPS origin already set, so an image built locally cannot be promoted to the VPS unchanged. Corrected after M3; see §15.

### 7a. Readiness semantics (specified in v1.11, M7c)

`/health/ready` touches Postgres, Redis, Celery and object storage on every request and never reports from cache. Its verdict is `ready` only when all four answer; otherwise `degraded`, returned with **HTTP 503** so a reverse proxy or orchestrator can act on it. `/status` is the signed-out page that renders it (Design.md §5).

The scan budget travels **beside** `checks`, never inside it. A spent free-tier allowance delays scans without making the service unready, and folding it into the verdict would take the whole API out of rotation over a quota that resets at midnight (§3a).

**Finding from M7c, resolved at M8 (below): the Celery check had the flaw the budget was kept out to avoid.** It reports `ok` from `celery_app.control.ping(timeout=2.0)`, which a *healthy but busy* worker does not answer in time: observed at M7c with thirteen scan tasks queued, where the page read `degraded` and the endpoint returned 503 for a worker that was working correctly and reported `ok` moments later. If the VPS's proxy or a monitor health-checks this endpoint, ordinary scanning load can therefore pull the API out of rotation. Three candidate fixes, to be decided at M8 rather than during a design pass: raise the ping timeout, report worker liveness from the worker's own heartbeat (Celery's inspect/ping is not the only source), or report the worker beside `checks` the way the budget is — liveness that matters for serving requests is Postgres and storage, not whether a background worker can reply within two seconds.

**Correction (M8a).** The M7c finding above attributed that `degraded` reading to a *busy* worker missing the ping. Tested at M8a and not reproduced: thirteen uploads queued, sampled once a second for 90 seconds with the old `ping(timeout=2.0)` running beside the new endpoint, and the old ping was answered 41 times out of 41. The likelier cause is what the M7c transcript shows minutes earlier — the Redis container, which is the worker's broker, had just been force-recreated. The decision below stands on its own reasoning rather than on that diagnosis.

**Resolution (v1.12, M8).** The verdict, and the 503, cover Postgres, Redis and object storage: what serving a request needs. Redis stays in, since completing an upload enqueues work on it. The Celery worker is reported **beside** `checks`, like the scan budget, from a heartbeat: a thread in the worker's own main process writes a Redis key every 15 seconds with a 45-second expiry, independent of whether the pool is busy. `/status` shows the worker as alive or not and how long ago it was last seen, so a dead worker is still plainly visible; it simply no longer takes the API out of rotation, because uploads and downloads keep working while scans wait. Verified on the live stack at M8a rather than only in tests: with the worker stopped for 50 seconds the endpoint stayed `200 ready` and reported the worker as not running once its heartbeat expired, then alive again after restart. A clean `docker compose stop` initially left the key behind — the `worker_shutdown` handler never ran before the process exited — so `worker_shutting_down`, which fires at the start of a warm shutdown, removes it too.

## 8. Object Storage Configuration

**Provider: Supabase Storage via its S3-compatible endpoint** (`https://<project_ref>.storage.supabase.co/storage/v1/s3`), using SigV4 presigning and boto3. A **new, separate Supabase project** — never the one used by `ecommerce-ai-platform` — for the same isolation reason this project does not share credentials with the n8n deployment.

- **Key structure:** `workspace_id/file_id/version_id/filename` (per-workspace prefix — defense-in-depth isolation, and workspace deletion becomes a single prefix delete). **Unchanged by the provider switch**, which is the point of keeping key construction provider-agnostic.
- **Two sets of credentials, different jobs.** The *service role key* administers buckets over the Storage REST API and bypasses RLS entirely — server-side only, never sent to the browser. The *S3 access key/secret* (generated in the dashboard under Storage settings) signs S3 requests and is what produces presigned URLs.
- Bucket is **private** (`public: false`); no object is reachable without a signed URL.
- **One bucket per environment (v1.12, M8).** Production uses `fileshare-prod`; local development keeps `fileshare-files`; backups go to `fileshare-backups`. Key construction is unchanged, so this is configuration only. The reason is destructive rather than tidy: an object is an orphan *relative to one database*, and at the end of M7 the bucket held 90 objects the local database did not know. Sharing a bucket, a cleanup run from either environment would delete the other's files. The separation is of data, not credentials (S3 keys are project-wide), and all three buckets share the free plan's 1 GB.
- **Storage CORS was re-probed for the production origins at M8:** a presigned `PUT` preflight answers `access-control-allow-origin: *` for `https://sharekeep.xyz`, so the domain change cannot break browser-direct upload.
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

## 17. Amendment Log — v1.8 (11 Sep 2026, start of M5): share links

§4.2–§4.5 were rewritten rather than annotated, because four decisions changed the rules
rather than adding to them.

1. **Tokens are stored hashed (`token_hash`), not raw.** M1 already established this for
   refresh tokens and invitations: 48 bytes of CSPRNG output, persisted as SHA-256, shown
   once. Schema §9 had specified a raw `token` column, which would have made a database
   leak hand over a working link to every file ever shared — and would have been the only
   bearer token in the system not protected that way. The cost is real and is now stated
   in §4.2 rather than discovered later: a link cannot be copied again after creation.
2. **A scan override does not extend to share links.** M4 gave a workspace owner a way to
   release a flagged file, deliberately recorded beside the status rather than over it.
   Reusing §3's answer verbatim here would have let that release become a public
   distribution channel — "I accept this risk" silently becoming "strangers accept this
   risk on my word", with no flag visible to them. Share links therefore apply a stricter
   rule, and §4.2 says so explicitly so it is not read as accidental duplication.
3. **Only attempts against links that exist are audited.** §4.5 required auditing every
   attempt. For a token matching nothing there is no `workspace_id` to attribute and no
   `target_id` that resolves — and the volume is attacker-controlled, so brute-forcing
   would append unbounded rows to our own audit table with the inbound limiter (which
   fails open) as the only bound. Unknown tokens now go to the rate limiter and the
   application log instead. Every *interesting* failure — right token, wrong password;
   expired; revoked; limit reached; wrong user — is still audited in full.
4. **Share links get their own, shorter signed-URL TTL** (60s against 300s). Once a
   signed URL is issued, revoking the link cannot recall it, so the TTL *is* the
   revocation window. Shortening it costs almost nothing because the TTL governs when a
   transfer must start, not how long it may run. The residual window is documented as a
   property of presigned URLs rather than implied away.

Three further points were specified because the resolution path is meaningless without
them, and all three are stated in §4.2:

5. **`max_downloads` counts link resolutions, not completed downloads.** The bytes come
   straight from storage, so a finished download is not an observable event here. The
   counter increments when a URL is issued, under a row lock so two simultaneous
   requests at the limit cannot both pass, and the UI calls it a limit on link *uses*.
6. **A link tracks the file, not a version.** It always serves the current version, so a
   new upload changes what an already-shared link delivers, and a `pending` new version
   correctly makes the link stop working until its scan resolves.
7. **Type and modifiers must combine.** Validation accepts `public` + `expires_at` and
   similar; rejecting those would restore exactly the single-enum reading that Schema §9
   v1.1 was written to correct.

§4.3 additionally records what was **verified against the live bucket** rather than
assumed, since those claims are the provider's to keep: the download disposition is
honoured, Supabase serves `text/plain` regardless of stored content type (so shared HTML
cannot execute — a second independent layer, and a sign that `files.mime_type` is our own
metadata, not what storage serves), a `PUT` against a download URL is refused 403, an
expired signature returns 400 rather than 403, and the signed URL discloses the full
storage key to the recipient.

## 18. Amendment Log — v1.9 (11 Sep 2026, start of M6): audit reading and notifications

§5 was rewritten rather than annotated. It had described the pipeline in three bullets,
which was enough to build against until the provider was actually probed; four of the
decisions below contradict what those bullets implied. §4.7 is new.

1. **Who may read the audit log was never specified.** §4.6 answered who may administer a
   workspace and stopped there, and the permissive default is not a minor convenience:
   the table holds the IP address and user agent of every share-link visitor plus every
   member's activity pattern. Now owners only, refused 403 rather than 404.
2. **Account-level events belong to no workspace log.** `login`, `login_failed` and
   `logout` carry `workspace_id = NULL`, so they are invisible in any workspace's history
   by construction rather than by filtering. Surfacing them would tell an owner when a
   collaborator signed in to unrelated work.
3. **The log is read as history, not resolved live.** Displaying `target_id` by looking it
   up now would make an append-only table mutable in effect — a rename would rewrite what
   the log says happened last week. Labels are denormalised into `metadata` at write time.
   Rows from M1–M5 predate the rule and do not all carry one; the reader says the name was
   not recorded rather than inventing it.
4. **`sent` means "the provider accepted it", not "it was delivered."** The restricted
   send-only key cannot read a message's status back at all (`GET /emails/{id}` → 401).
   The original three-bullet §5 implied the status field tracked delivery. It cannot, so
   it is defined down to what is knowable, and `provider_message_id` is stored so a human
   can answer the question from the dashboard.
5. **Failures are separated into transient, permanent and not-configured**, with retry
   state (`attempts`, `last_error`) on the row rather than only inside the Celery retry —
   otherwise a worker restart loses the count and no sweep can recover the row. Retrying a
   permanent refusal would only burn free-tier budget. This mirrors the distinction
   `virustotal.py` already draws.
6. **Delivery is idempotent at the row**, guarded by `sent_at` under a row lock. Without
   it, a task that succeeded at the provider and died before committing would send again
   on retry — and an unwanted duplicate email cannot be recalled.
7. **A quota warning is an edge, not a state.** `storage_quota_warning_ratio` had sat in
   the configuration since M0 without a single reader. The obvious wiring — notify while
   usage ≥ 90% — would email the owners on every upload forever. It fires on the upward
   crossing only, and deleting back under the threshold re-arms it.
8. **Recipients are named per event** (§5a), because §5 listed trigger events without ever
   saying who receives them, and for three of the five the answer is not obvious. In
   particular `file_shared` fires only for a **private** link: the other three types have
   no recipient, and emailing whoever just created the link is noise.
9. **Mail is redirected in development** via `notification_redirect_to`, because the
   sandbox permits exactly one recipient. Off unless set, and recorded on the row so a
   redirected send is never counted as a real one.

§5c records what was **verified against the live Resend API** rather than assumed, since
these are the provider's properties and not ours: an unverified sending domain is refused
403 — which the shipped `noreply@example.com` would have hit on every send — the sandbox
permits only the account owner as recipient (403, naming that address), the simulator
addresses `delivered@resend.dev` and `bounced@resend.dev` are exempt from that rule and so
are the only way the delivery and failure paths can be exercised, and six sends with no
pause drew no 429 despite a documented 2/second ceiling. The limiter follows the
documented figure, not the observed absence of one.

**Added after M6 was first marked done (12 Sep 2026).** Item 10 was found by re-checking
the finished milestone rather than by any test, and is recorded here rather than folded
silently into the items above:

10. **The invitation email linked to a page that did not exist, with its token in the
    query string.** The suite asserted the token was *in* the email and the live run
    proved Resend *accepted* it; nothing followed the link. Following it gave a 404, and
    the query string contradicted §4.6's own rule that an invitation token never travels
    in a URL a server can log. The link now uses the URL fragment, which no server ever
    receives, and §4.6 states how the acceptance page handles it.

## 19. Amendment Log — v1.10 (12 Sep 2026, start of M7): search, rollback, trash

Written after mapping every backend endpoint against what the frontend calls. The finding
that shaped M7 is recorded in PRD §10e: most of v1's features had an API and no interface.
Five things in *this* document were missing or wrong, and are fixed before any code:

1. **Search had no section.** §4.8 now defines it, including the "metadata" the PRD
   named and never defined, and the argument for why membership is sufficient scoping.
2. **Grants could be held by non-members.** Schema §8 said a grant is "to a specific
   workspace member"; the grant route accepted any user and the resolver honoured the
   grant without checking membership. Inert until now, because every workspace endpoint
   refused non-members first. §4.1 now makes membership a precondition at both layers —
   the route refuses a non-member, and the resolver returns "denied" for one regardless
   of grants — so search's safety argument does not rest on one of them alone.
3. **Version rollback was promised and never built.** §4.9. Copy-forward, decided by the
   developer over a pointer move; server-side copy was probed against the live bucket
   first.
4. **The trash could not be seen.** §6a promised it; §6b adds the list.
5. **Grants could not be listed, and folder grants could not be revoked at all.** A
   folder-level grant, once made, was permanent. That is a security defect rather than a
   missing convenience: narrowing a grant is how access is taken away. Listing and
   revoking are now specified alongside creation, for files and folders alike.
7. **Quota could be raised by the workspace's own owner without bound** (§4.6). Now
   lower-only, decided by the developer.
8. **Leaving a workspace was implied and missing** (§4.6).
9. **Deleting a workspace orphaned every object it held** (§6c). Found while building
   the settings page that offers the button.
6. **The retention sweep was documented and never built** (§6b). Found while specifying
   the trash, because "whether it can still be restored" depends on it.

## 20. Amendment Log — v1.11 (15 Sep 2026, during M7c): readiness semantics

**§7a added.** The readiness endpoint had never been specified beyond its implementation, and the page rendering it had never been looked at in a browser since M0 — which is how it kept announcing itself as the M0 scaffold through seven milestones, and how the overall verdict it computes went unrendered. M7c specifies what the endpoint means, records that the scan budget is deliberately outside the verdict, and records the open finding that the Celery check reports `degraded` for a healthy-but-busy worker. Nothing about the endpoint's behaviour changed at M7c; the page and the specification changed to match it.

## 21. Amendment Log — v1.12 (17 Sep 2026, start of M8): production deployment

Eight decisions (PRD §10g) and one defect, all specified before any M8 code.

- **§4.0**: hosts resolved (`sharekeep.xyz`, `api.sharekeep.xyz`); the CSRF token specified; the download URL moves from `GET` to `POST`; sign-up gated in production.
- **§4.4**: the client address is taken a configured number of hops from the right of `X-Forwarded-For`. The leftmost-entry implementation was shown to defeat every per-IP limit (80 attempts, no refusal) and to let a visitor choose the IP recorded in the audit log.
- **§5c**: production sender on a verified sending subdomain, redirect off.
- **§7**: hosts, CSP, who operates the VPS, and nightly encrypted backups with a restore drill. Backups were in no document before this.
- **§7a**: the M7c Celery finding resolved; the worker is reported beside the verdict from a heartbeat.
- **§8**: one bucket per environment; storage CORS re-probed for the production origins.

## 22. Amendment Log — v1.13 (18 Sep 2026, during M8a): what building it changed

M8a implemented §21's decisions. Three entries changed because the code disagreed with the specification, and one because a diagnosis did not survive testing.

- **§4.0** — the CSRF token is **not** session-bound as specified. No identifier is present on every request to bind it to, so binding would have failed at exactly the moment a session needed refreshing. A `__Host-` prefixed cookie (which no sibling subdomain can set) plus rotation at sign-in and sign-out provides what the binding was for.
- **§4.0** — the client-side refresh was added, and the finding that prompted it recorded: the frontend had never called `/auth/refresh`, so sessions effectively lasted fifteen minutes. Serialised across tabs, because reuse detection revokes every session.
- **§7** — the CSP is specified as built, including the two limits `'strict-dynamic'` imposes and why they are acceptable here.
- **§7a** — the M7c claim that a busy worker misses the ping was **not reproduced** and is corrected. The design decision is unaffected.

## 23. Amendment Log — v1.14 (18 Sep 2026, start of M8b): the VPS, the proxy, Cloudflare

The diagnostic contradicted an assumption every version of §7 had carried since kickoff.

- **§7** — the VPS is fresh: no Docker, nothing on 80/443, no n8n stack, so there is no existing reverse proxy to join. Caddy is stood up in its own Compose project on the external `proxy` network, which keeps the separability §7 and Rules §7 were written for.
- **§7** — Cloudflare's proxy is enabled, fixing `FILESHARE_TRUSTED_PROXY_HOPS` at 2, and making the origin firewall part of §4.4's fix: a request that bypasses Cloudflare can forge the whole `X-Forwarded-For` chain.
- **§7** — origin TLS is Let's Encrypt via HTTP-01 through Cloudflare, with SSL mode Full (strict).
