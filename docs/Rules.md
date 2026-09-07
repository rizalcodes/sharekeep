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

`.env` is never committed. `.env.example` (placeholder values only) is the tracked version.

## 3. Credential Handling Rules

- AWS credentials, VirusTotal API key, Resend API key: loaded from environment only, never hardcoded, never logged (even at debug level — file contents and credentials must never appear in application logs).
- S3 access uses **signed URLs** for all client-facing file access (upload and download) — the backend never proxies raw file bytes through itself for large files, and the client never receives a raw/unsigned S3 URL or AWS credentials.
- VirusTotal API calls are rate-limited client-side (reusing the previously-designed `api_keys`/`rate_limit_rules`-style pattern) to stay within the free-tier request budget — do not just retry-on-429 without a proactive limiter.

## 4. File/Folder Convention (project structure)

Mirrors the structure used in prior shipped projects.

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
        storage.py        # S3 signed URL generation, multipart sessions
        scanning.py       # VirusTotal integration + rate limiter
        notifications.py  # Resend email sending
        permissions.py    # the single permission-resolution path (Architecture §4)
      tasks/             # Celery tasks
        scan_tasks.py
        version_tasks.py
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
