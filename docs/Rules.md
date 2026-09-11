# Rules: Secure File Sharing Platform — Conventions

**Scope note:** working rules and conventions — naming, credential handling, file/folder structure, git. Not data structure (`Schema.md`) or system design (`Architecture.md`).

---

## 1. Naming Conventions (code)

- Python: snake_case for variables/functions, PascalCase for classes, matching the pattern used in `ecommerce-ai-platform`.
- API routes: RESTful, plural nouns (`/files`, `/folders`, `/workspaces`), consistent with prior projects.
- Database: per `Schema.md` — snake_case, plural tables.

## 2. Credential & Environment Variable Naming

Prefix all env vars `FILESHARE_` to avoid collisions with other projects running on the same shared VPS (n8n project, and its own `AGENCY_`-prefixed vars).

- `FILESHARE_AWS_ACCESS_KEY_ID`
- `FILESHARE_AWS_SECRET_ACCESS_KEY`
- `FILESHARE_AWS_S3_BUCKET`
- `FILESHARE_VIRUSTOTAL_API_KEY`
- `FILESHARE_RESEND_API_KEY`
- `FILESHARE_DATABASE_URL`
- `FILESHARE_REDIS_URL`
- `FILESHARE_JWT_SECRET`
- `FILESHARE_RESEND_FROM_EMAIL`
- `FILESHARE_NOTIFICATION_REDIRECT_TO`

`.env` is never committed. `.env.example` (placeholder values only) is the tracked version.

**A placeholder that the provider will reject is worse than an empty value** (added at M6). `FILESHARE_RESEND_FROM_EMAIL` shipped as `noreply@example.com`, which reads like a harmless default but is refused 403 by Resend on every single send — an unverified sending domain. `.env.example` therefore carries the sandbox sender that actually works, with the condition for changing it stated inline. The same applies to any credential-adjacent default: if the real service would reject it, it belongs in the file as a working value or not at all.

**`NEXT_PUBLIC_*` variables are build-time, not runtime.** Next.js inlines them into the client bundle during `next build`. Any such value must reach the frontend image through `build.args`, never a runtime `environment:` entry — the latter is silently ignored by browser code and fails without an error. Changing one requires rebuilding the image, not restarting the container. (Corrected after M3; see `Architecture.md` §15.)

## 3. Credential Handling Rules

- Storage credentials (Supabase service role key and S3 access key/secret), VirusTotal API key, Resend API key: loaded from environment only, never hardcoded, never logged (even at debug level — file contents and credentials must never appear in application logs).
- **Bearer tokens the system issues are stored hashed, never raw** (added at M5, after Schema §9 had specified a raw column for share links). Refresh tokens, invitation tokens and share-link tokens are 48 bytes of CSPRNG output persisted as SHA-256 — SHA-256 rather than Argon2 because there is no human-chosen secret to brute force and lookup must stay indexable. The raw value is returned once and never again. Any new token type follows the same rule, and the user-visible consequence (a token cannot be re-displayed) is surfaced at the point of creation rather than discovered later.
- The **Supabase service role key bypasses Row Level Security entirely**. It is server-side only and must never reach the browser, a client bundle, or a `NEXT_PUBLIC_` variable.
- Object storage access uses **signed URLs** for all client-facing file access (upload and download) — the backend never proxies raw file bytes through itself for large files, and the client never receives a raw/unsigned storage URL or provider credentials.
- **Email is an outbound copy of our data, so it carries only what the recipient is already entitled to** (added at M6). A notification names the workspace, the file and the actor; it never embeds a share-link token, an invitation token, a signed storage URL, or file contents. An invitation email carries its token because the token *is* the invitation and there is no other channel, and that is the single exception. Notification bodies are never written to the application log for the same reason file contents are not.
- **A restricted, send-only provider key is preferred over a full-access one** (added at M6). The Resend key in use cannot list domains, read API keys, or read a message's delivery status. That costs us delivery visibility — which is why `notifications.status = sent` is defined as "the provider accepted it" rather than overstated — and it is the right trade: a leaked sending key can send mail, while a leaked full key exposes the account.
- VirusTotal API calls are rate-limited client-side (reusing the previously-designed `api_keys`/`rate_limit_rules`-style pattern) to stay within the free-tier request budget — do not just retry-on-429 without a proactive limiter.

## 4. File/Folder Convention (project structure)

Mirrors the structure used in prior shipped projects. *(Corrected at M6: the tree below had drifted, naming a single `scanning.py` and a `version_tasks.py` that were never built that way. A provider client is kept separate from the domain logic that uses it — `virustotal.py` beside `scan.py`, `resend.py` beside `notifications.py` — because the client is the part that gets probed, stubbed and replaced.)*

Repo root on the dev machine is `d:\Secure File Sharing Platform` (the five design docs move into `docs/` at M0).

```
secure-file-sharing/
  docs/
    PRD.md
    Schema.md
    Rules.md
    Architecture.md
    Design.md
  backend/
    app/
      core/              # config, security, db session, dependencies
      models/            # SQLAlchemy models, one module per entity group
      schemas/           # Pydantic request/response models
      routers/
      services/
        storage.py        # signed URL generation, multipart sessions (provider-agnostic surface)
        virustotal.py     # the VirusTotal HTTP client, nothing else
        vt_budget.py      # outbound request budget (fail-closed, Architecture §3a)
        scan.py           # scan domain logic: verdicts, overrides, downloadability
        resend.py         # the Resend HTTP client, nothing else
        notifications.py  # queueing and rendering; delivery rules in Architecture §5
        audit.py          # append-only audit writes (Architecture §4.5, §4.7)
        permissions.py    # the single permission-resolution path (Architecture §4)
      tasks/             # Celery tasks
        scan_tasks.py
        content_tasks.py
        notification_tasks.py
        maintenance_tasks.py   # quota reconciliation, orphan multipart sweep
    alembic/             # migrations
    tests/
  frontend/
    (Next.js app, structure mirrors ecommerce-ai-platform's frontend layout)
  docker-compose.yml       # This project's OWN stack, own Compose project name, on the shared VPS (see §7)
  .env.example
  .gitignore
```

## 5. Git Convention

- Branches: `feature/m<milestone-number>-<short-desc>` (e.g. `feature/m3-chunked-upload`), matching the milestone table in `PRD.md`.
- Commits: imperative mood, short subject, milestone tag optional in body.

## 6. Testing & Verification Discipline (carried over from all prior projects)

- Verify the actual artifact/behavior, not just logic on paper, before marking any milestone done.
- Manual validation is required at each milestone boundary — automated tests passing is necessary but not sufficient.
- Any security-sensitive path (permission check, signed URL generation, share link access) must be tested against a deliberately adversarial case (e.g. attempting to access another user's file by guessing/incrementing an ID), not just the happy path.

## 7. Deployment Convention

*(Amended 7 Sep 2026 at kickoff — the original wording said "same `docker-compose.yml`", which would have meant editing a live stack's compose file. Same isolation intent, safer mechanics.)*

- This project ships its **own** `docker-compose.yml` under its own Compose project name, running on the **same VPS** as the n8n project — not merged into n8n's compose file or containers. Services: backend, Celery worker, Celery beat, frontend, and this project's own Postgres and Redis.
- The two stacks meet only at a shared **external Docker network** with the existing reverse proxy.
- Document clearly in `Architecture.md` which resources are shared with the n8n deployment (the VPS itself, the reverse-proxy network) versus fully separate (databases, Redis, credentials) — no credential or data sharing between the two projects.
