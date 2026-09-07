# PRD: Secure File Sharing Platform

**Status:** Draft v1 — ready for build
**Date:** 6 September 2026
**Author:** Chief of Strategy (drafted with Rizal, before handoff to build session)

---

## 1. Executive Summary

A secure, self-hosted file storage and sharing platform — think a scoped-down Google Drive/Dropbox — built as portfolio project #9 (backend/AI shortlist item #1). Pure backend/security engineering, no AI. Chosen first from the 7-project shortlist because it reuses the most existing skills (auth/RBAC, Redis, background jobs, PostgreSQL, Docker) while adding genuinely new ones (AWS S3, chunked upload, Celery, malware scanning).

**Explicit mindset for this project (and going forward):** full, well-reasoned scope — not a rushed MVP. Every feature below was deliberately chosen with a reason, not defaulted to "fast and minimal."

## 2. Background & Strategic Context

- Comes after the AI Automation Agency (n8n) project, now paused after its first product launched on 3 marketplaces (Gumroad, n8n Markets, AutomationWorkflows.io).
- Part of an ongoing **build → package → build → package** cycle: build a portfolio project, then return to package/monetize existing work, alternating.
- From a shortlist of 7 candidate backend/AI projects (an 8th, AI Medical Assistant, was deliberately skipped due to health-domain risk). This is #1 in priority — rated highest for skill-reuse and lowest AI-domain risk.
- Reflects a mindset shift (6 Sep 2026): past projects sometimes chose minimal scope to finish fast. Going forward, scope should be justified by real reasoning, not speed alone.

## 3. Goals

- Ship a genuinely secure, production-grade file sharing platform demonstrating backend/security engineering depth — not a toy demo.
- Learn and demonstrate: AWS S3 integration, chunked large-file upload, Celery task queues, malware scanning integration, granular RBAC for collaboration.
- Reinforce (not just reuse) skills already proven: JWT auth, PostgreSQL schema design, Redis, Docker multi-service deployment.

## 4. Scope (v1 — all in, per the full-scope mindset)

### In scope
- **Auth & workspace model:** JWT auth; team/workspace model with persistent collaborator permissions (not just single-user + link sharing).
- **Permission levels:** Owner / Editor / Viewer per file or folder, RBAC-based (reusing the Users→Roles→Permissions pattern from `ecommerce-ai-platform`).
- **Folder structure:** Nested folders (tree, `parent_folder_id` self-referencing).
- **Upload:** Drag & drop, large file support via chunked/multipart upload.
- **File versioning:** Re-uploading a file with the same name creates a new version; rollback to a prior version is supported.
- **Malware scanning:** Every uploaded file scanned via VirusTotal API before being marked available; async via Celery (not blocking the upload response).
- **Storage quota:** Per-workspace storage limit (configurable per workspace), enforced before upload completes.
- **Sharing:** All 4 link types — public, private, password-protected, expiring links.
- **Activity audit log:** Every meaningful action (upload, download, share, delete, permission change) logged with actor, timestamp, target.
- **Search:** Filename/metadata/tag search only (not full-text document content search).
- **Notifications:** Email only (via Resend), for share events and relevant activity. Notification data model is designed to be WebSocket-ready for a future v1.1, without a schema rebuild.

### Explicitly out of scope (deferred, with reasons)
- **Full-text document search** — infra cost (Elasticsearch/Postgres FTS) disproportionate to value for a portfolio demo.
- **Real-time WebSocket notifications** — the project is already scope-heavy; this would add infra complexity purely for "wow factor" without a current need. Schema stays ready for it.
- **Public API for third-party integration** — not part of this project's story.

## 5. Key Decisions Log

| Area | Decision | Reasoning |
|---|---|---|
| Storage provider | AWS S3 (not reused Supabase Storage) | Industry-standard, more transferable skill, not yet represented in portfolio |
| Chunk upload | Full v1 scope, not deferred | Core to "large file" support, and a genuinely new skill |
| Share links | All 4 types in v1 | No reason strong enough to cut any given the full-scope mindset |
| Ownership model | Team/workspace with persistent collaborators | More realistic than single-user-only; reuses proven RBAC pattern |
| Malware scanning | VirusTotal API, included in v1 | Makes "Secure" in the name literal; ties directly into the previously-designed API rate-limiting reference (VirusTotal's free tier is tightly rate-limited) |
| Storage quota | Included in v1 | Low implementation cost, high realism (every real storage platform has this) |
| Folder structure | Nested (tree) | Standard, realistic, low extra cost given the self-referencing FK pattern is already used elsewhere in the project |
| Audit log | Full v1 | Directly reinforces the "Secure" positioning |
| Permission granularity | Owner/Editor/Viewer (not binary) | Makes the audit log meaningful; matches real platform expectations |
| Versioning | Included in v1 | Realism; low relative cost via a self-referencing version chain |
| Notifications | Email-only v1, WebSocket-ready schema | Explicit scope-control decision — don't add complexity that isn't currently needed, but don't block it later either |
| Search | Filename/metadata only | Full-text search infra cost ruled disproportionate |
| Background jobs | Celery + Redis (not APScheduler) | New skill worth learning; suits task-chaining needs (scan → version processing) better than APScheduler |
| Malware provider | VirusTotal (not self-hosted ClamAV) | Avoids extra server/infra; free tier sufficient; reuses the rate-limiting reference design |
| Deployment | Reuse the existing n8n project's VPS (new Docker service), not Railway | Zero marginal cost (VPS already paid for) vs. Railway's time-limited free trial; better suited to Celery's persistent worker process; no revenue yet from the Gumroad product, so cost control matters |

## 6. Non-Functional Requirements

- **Security first:** every file access path (direct download, share link, API) must validate ownership/permission before serving data. No file should ever be accessible by guessing/incrementing an ID.
- **Signed URLs** for all S3 object access — never expose raw S3 URLs or credentials to the client.
- **Auditability:** every state-changing action logged, consistent with the audit-log requirement above.
- **Cost control:** all third-party services (S3, VirusTotal, Resend) used within free-tier limits during this build phase; no paid infra added without an explicit decision.
- **Testing discipline:** same standard as all prior projects — verify the actual artifact/behavior, not just logic on paper; manual validation before any milestone is marked done.

## 7. Milestone Breakdown (proposed — refine in Architecture.md / Milestones.md during build)

| Milestone | Deliverable |
|---|---|
| M0 | Project scaffold (FastAPI backend, Next.js frontend, Docker Compose on the shared VPS, Celery+Redis wired, S3 bucket configured) |
| M1 | Auth + workspace + RBAC (Owner/Editor/Viewer) |
| M2 | Core file/folder CRUD + nested folder structure + storage quota enforcement |
| M3 | Upload pipeline: drag & drop + chunked/multipart upload to S3 |
| M4 | Malware scanning (Celery + VirusTotal) + file versioning |
| M5 | Sharing: all 4 link types + password protection + expiration |
| M6 | Audit log + email notifications (Resend) |
| M7 | Search/filter, frontend polish, design pass |
| M8 | Deployment to the shared VPS, end-to-end verification |

## 8. Decisions Finalized at Kickoff (7 Sep 2026, prior to M0)

- **S3 bucket structure:** per-workspace prefix — `workspace_id/file_id/version_id/filename`. Chosen for defense-in-depth isolation (matches the "no guessable object ID" principle) and simpler workspace-deletion cleanup.
- **Workspace creation:** automatic, 1 personal workspace per user on signup (role = owner). Users may create additional workspaces manually later.
- **Storage quota:** 5GB default, **tracked and configurable at the workspace level** (`workspaces.storage_quota_bytes` / `storage_used_bytes`), not per-user — since a user may belong to multiple workspaces with independent limits. (This corrected an earlier draft inconsistency where quota fields were mistakenly placed on `users` in Schema.md — fixed prior to M0.)

## 9. Decisions Finalized at Build Kickoff (7 Sep 2026, second round — during doc review, still prior to M0)

Four decisions taken after a full read of all five documents surfaced conflicts that could not be resolved from the docs alone.

| Area | Decision | Reasoning |
|---|---|---|
| Malware scan strategy | **Hash-lookup first; skip files too large to submit** | VirusTotal caps submission at 32MB direct / 650MB via upload-URL, and free-tier submissions are shared with the VT community. Looking up SHA-256 first costs one request, sends no file content, and resolves most real uploads. Files above the ceiling get an explicit `scan_skipped` state with a visible "not scanned" badge — the alternative was either lying in the UI or making large files permanently inaccessible, which would have gutted the large-file feature. See Architecture §3. |
| Compose layout | **Own `docker-compose.yml`, own Compose project, shared external proxy network** | Same VPS and same isolation intent as Rules §7, but no edit to the live n8n stack's compose file and independent lifecycle control. A wording deviation from Rules §7, not an intent deviation; Rules §7 updated to match. |
| Redis | **Dedicated container, not a shared instance with a separate DB index** | Celery broker traffic plus the isolation-from-n8n requirement both argue against sharing; a second Redis costs almost nothing. |
| Doc corrections | **Amend Schema.md and Architecture.md before M0**, not per-milestone | Docs are source of truth; building M0 against known-wrong docs would compound. Nine schema issues and eight architecture gaps fixed — see the amendment logs in Schema.md §14 and Architecture.md §10. |

**Documented gaps closed in the same pass** (all were genuine omissions, not scope changes): a circular FK that made file inserts impossible, an audit-log action enum that could not express a failed access attempt, tags promised in §4 but absent from the schema, no workspace invitation path, no soft delete, share links unable to combine password + expiry, and undefined folder permission inheritance.

## 10. Next Steps

1. ~~Draft `Schema.md`, `Rules.md`, `Architecture.md`, and `Design.md`.~~ Done.
2. ~~Bring all 4 documents to a new build session (Claude Code) along with this PRD.~~ Done.
3. ~~Doc review and amendment pass.~~ Done (7 Sep 2026).
4. Begin M0.
