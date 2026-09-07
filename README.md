# Secure File Sharing Platform

Secure, self-hosted file storage and sharing for teams — workspaces with persistent
Owner/Editor/Viewer roles, nested folders, chunked large-file upload, malware
scanning, file versioning, four kinds of share link, and a full audit trail.

Portfolio project #9. Design documents are the source of truth and live in [docs/](docs/):
[PRD](docs/PRD.md) · [Schema](docs/Schema.md) · [Architecture](docs/Architecture.md) ·
[Rules](docs/Rules.md) · [Design](docs/Design.md).

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI (Python 3.12), SQLAlchemy 2.0 async, Alembic |
| Frontend | Next.js 16 (App Router, TypeScript, Tailwind 4) |
| Database | PostgreSQL 16 |
| Jobs | Celery + Redis (worker and beat) |
| Object storage | Supabase Storage via its S3-compatible endpoint (signed URLs only — the API never proxies file bytes). AWS S3 is the long-term target; see PRD §10 |
| Scanning | VirusTotal API (hash lookup first, upload only when necessary) |
| Email | Resend |

## Local development

Requires Docker Desktop, Python 3.12, and Node 22+.

```bash
cp .env.example .env          # then fill in the blanks
python -c "import secrets; print(secrets.token_urlsafe(64))"   # FILESHARE_JWT_SECRET

docker compose up -d          # postgres, redis, backend, worker, beat, frontend
```

Then check `http://localhost:8000/health/ready` — it probes Postgres, Redis, a live
Celery worker, and the storage bucket on every request. The frontend at
`http://localhost:3000` renders the same checks.

Running the backend directly instead of in Docker:

```bash
cd backend
python -m venv .venv && ./.venv/Scripts/pip install -r requirements-dev.txt   # Windows
./.venv/Scripts/python -m pytest
./.venv/Scripts/python -m alembic upgrade head
./.venv/Scripts/python -m uvicorn app.main:app --reload
```

### Storage bucket

Needs a **new, separate Supabase project** — do not reuse another project's.

```bash
cd backend
python scripts/bootstrap_storage.py            # read-only report, changes nothing
python scripts/bootstrap_storage.py --apply    # create + configure (idempotent)
```

`--apply` creates a private bucket with a per-file size limit. The check additionally
probes whether the S3 protocol is enabled, whether multipart upload is accepted, and
whether CORS permits browser-direct upload — the last of which decides the M3 upload
path and cannot be answered from documentation.

## Deployment

Own Compose stack on the shared VPS, joined to the existing reverse-proxy network:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The n8n project on the same VPS shares nothing with this stack but the machine and that
proxy network — separate database, separate Redis, separate credentials
([Architecture §7](docs/Architecture.md)).

## Milestones

See [docs/Milestones.md](docs/Milestones.md) for current status.
