# Architecture: Secure File Sharing Platform — System Design

**Scope note:** system/service design — how components interact, the background job pipeline, security architecture. Not data structure (`Schema.md`) or naming/conventions (`Rules.md`).

**Version:** v1.3 — authentication and workspace permissions specified at the start of M1, 8 September 2026 (see §12). Previously v1.2 (storage provider, §11) and v1.1 (kickoff, §10).

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

**Abandoned uploads:** a multipart upload that is initiated but never completed leaves orphan parts and a `file_versions` row with no object behind it. A Celery beat task sweeps both — aborting multipart uploads older than 24h and reaping the corresponding pending rows. On AWS S3 a bucket lifecycle rule would catch these as a second line of defense; **Supabase Storage has no lifecycle rules, so this sweep is the only protection** and must be implemented properly at M3.

## 3. Malware Scan Pipeline (Celery)

**The constraint that shapes this design:** the VirusTotal API caps direct file submission at 32MB (32–650MB requires a separate upload-URL endpoint; above 650MB submission is impossible), the free tier allows roughly 4 requests/minute and 500/day, and **free-tier submissions are shared with the VirusTotal community**. Sending a customer's file content to a public analysis pool is a poor property for a product named "secure file sharing." Both facts push the same direction: look up by hash first, upload bytes only when there is no other option.

`scan_tasks.scan_file_version(file_version_id)` — triggered on upload completion:

1. Stream the object from storage and compute its SHA-256; store it in `file_versions.checksum_sha256`. This doubles as the integrity checksum.
2. Set `scan_status = scanning`.
3. **Hash lookup first:** `GET /files/{sha256}`. This costs one rate-limited request, sends **no file content** to VirusTotal, and resolves the large majority of real-world uploads (any file VT has seen before). A hit returns the full engine verdict → go to step 6.
4. **On a miss (VT has never seen this file):**
   - `size <= 32MB` → submit the bytes via `POST /files`, poll the analysis, then verdict.
   - `32MB < size <= 650MB` → obtain a large-file upload URL via `GET /files/upload_url`, submit, poll, verdict.
   - `size > 650MB` → **`scan_status = scan_skipped`**, with the reason recorded in `scan_detail`. The file is served, but carries a permanent, visible "not scanned" badge in the UI (Design.md §5 — the scan badge is a core trust signal and must tell the truth, including when the truth is "we could not check this").
5. All VirusTotal calls pass through a **proactive client-side rate limiter** (Rules.md §3) — a token bucket in Redis sized to the free tier's per-minute and per-day budgets. Tasks block on the limiter rather than firing and handling 429s.
6. On a verdict: update `file_versions.scan_status` and, if this is the current version, `files.scan_status` → `clean` or `flagged`. Engine hit summary into `scan_detail`.
7. If `flagged`: the file is **not** deleted automatically (same "don't auto-delete on an uncertain signal" principle used in the n8n project's stale-SKU handling) — it is marked flagged, all access is blocked pending owner/workspace-admin review, an `audit_logs` entry (`scan_flagged`) is written, and a notification is queued.
8. If the scan itself fails (API error, timeout): `scan_status = scan_failed`, retried with exponential backoff (Celery's built-in retry). After retries are exhausted the file remains inaccessible until a successful scan or an explicit admin override.
9. On success, if this version replaces a prior current version, the task chains into version-processing: update `files.current_version_id`, `files.size_bytes`, `files.mime_type`.

**Access rule by scan status** (this is the single source of truth for whether bytes may be served):

| `scan_status` | Downloadable? |
|---|---|
| `pending`, `scanning` | No — the UI shows the file with a scanning badge, but no download URL is issued |
| `clean` | Yes |
| `scan_skipped` | Yes, with a persistent "not scanned" badge |
| `flagged` | **Never**, including to the owner, until an explicit admin review clears it |
| `scan_failed` | No, until a retry succeeds or an admin overrides |

**Version rollback interaction:** rolling back to an older version is permitted only if that target version's own `scan_status` allows access under the table above. A flagged current version does not block rollback to a clean earlier one — that is precisely the recovery path.

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

### 4.4 Rate limiting

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

- Checked at `upload-init` (before any storage interaction begins) using `workspaces.storage_used_bytes + incoming_size > workspaces.storage_quota_bytes`. The provider's own per-file ceiling (`FILESHARE_MAX_FILE_SIZE_BYTES`, 50MB on the Supabase free plan) is checked at the same point, so an oversized file is rejected cleanly instead of failing partway through a multipart upload.
- `storage_used_bytes` updated transactionally on upload completion and on file/version deletion, scoped to the file's `workspace_id` — never left to drift. A periodic reconciliation task (Celery beat) recomputes actual storage usage per workspace prefix against the stored counter as a consistency check, following the same "verify the artifact, don't just trust the counter" discipline used throughout prior projects.
- Soft-deleted files still occupy storage. v1 rule: `storage_used_bytes` is decremented at soft-delete time and the objects are purged by a retention sweep (30 days), so the counter reflects what the user believes they are using.

## 7. Deployment Architecture

**Compose layout (decided at kickoff, 7 Sep 2026):** this project ships its **own** `docker-compose.yml` under its own Compose project name, on the same VPS as the paused n8n project — *not* merged into the n8n project's compose file. Same isolation intent as `Rules.md` §7, safer mechanics: no edit to a live stack's compose file, independent `up`/`down`/restart, and no chance of an unrelated change taking n8n's services with it. The two stacks meet only at an **external Docker network** shared with the existing reverse proxy.

Services: `fileshare-backend`, `fileshare-celery-worker`, `fileshare-celery-beat`, `fileshare-frontend`, `fileshare-postgres`, `fileshare-redis`.

Redis is a **dedicated container**, not a shared instance with a distinct DB index — Celery's broker traffic and the isolation-from-n8n requirement both argue against sharing, and the memory cost of a second Redis is negligible.

No credential or data sharing with the n8n project's services — fully isolated except for the physical VPS and the shared reverse-proxy network.

Reverse proxy / HTTPS: reuse the VPS's existing reverse proxy if one is present from the n8n deployment; otherwise stand up new routing. Confirm actual VPS state at M0/M8.

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
