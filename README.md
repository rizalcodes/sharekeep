# 🔐 Secure File Sharing Platform — Enterprise-Grade File Sharing, Built From Scratch

![Next.js](https://img.shields.io/badge/Next.js-14-000000?style=flat-square&logo=next.js&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-Python_3.12-009688?style=flat-square&logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Supabase-3ECF8E?style=flat-square&logo=supabase&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-Redis-37814A?style=flat-square&logo=celery&logoColor=white)
![VirusTotal](https://img.shields.io/badge/Security-VirusTotal-394EFF?style=flat-square&logo=virustotal&logoColor=white)
![Tests](https://img.shields.io/badge/Backend_Tests-412_passing-2ea44f?style=flat-square)
![Live](https://img.shields.io/badge/Status-Live_in_Production-2ea44f?style=flat-square)

**[Live Demo →](https://sharekeep.xyz)**

---

## ❓ Problem

Most "file sharing" portfolio projects are a thin wrapper around an S3 bucket: upload a file, get a link, done. That skips every problem that actually makes file sharing hard — a shared link is an open door unless something scans what comes through it, limits who can walk in, and remembers who did.

## 💡 Solution

A self-hosted Google-Drive-style platform that treats sharing as the hard problem it is: every uploaded file is scanned for malware before anyone else can touch it, every share link carries an explicit, combinable access policy (not just "on" or "off"), and every access is logged. Then it's deployed the way a real production app has to be — behind a reverse proxy, with the firewall, TLS, and spoofing protections that a "just run it on a VPS" tutorial usually skips.

---

## ✨ Features

- 📁 **Full file management** — folders, versioning, per-file/folder permissions, workspace-level storage quotas, drag-and-drop chunked uploads with progress, retry, and resume
- 🛡️ **Malware scanning on every upload** — VirusTotal integration, hash-lookup-first to conserve API quota, processed async via Celery so uploads never block on a scan
- 🔗 **4 share-link types, combinable** — public, private (invite-only), password-protected, and time-expiring links can be mixed on the same file (e.g. password + expiring)
- 🔑 **Auth done properly** — JWT with httpOnly cookies, Argon2id password hashing, refresh-token rotation with reuse detection (a stolen refresh token gets detected and revoked, not silently accepted)
- 👥 **Workspaces + RBAC** — Owner/Editor/Viewer roles per workspace, invitation flow, auto-created personal workspace on signup
- 📜 **Audit logging** — every file access, share, and permission change is logged (sign-in/out excluded on purpose, to avoid over-collecting), with real visitor IPs correctly resolved behind Cloudflare
- 📧 **Email notifications** — Resend-powered alerts for shares, invitations, and access events
- 🔍 **Search & tags** — find files across workspaces without digging through folders

---

## 🔒 Production Hardening

This wasn't "deploy and hope." Every item below was a real bug found and fixed, not a checklist copied from a blog post:

| Risk | Fix |
|---|---|
| Spoofed `X-Forwarded-For` bypassing per-IP rate limits | Validated trusted-proxy hop count, config shared between Caddy and the app |
| Reverse proxy silently overwriting (not appending) the real client IP | Explicit `trusted_proxies` config in Caddy — caught after a full day of audit logs recording Cloudflare's edge IP instead of the real visitor |
| CSRF on state-changing requests | Double-submit token with `__Host-`-prefixed cookies, rotated on sign-in/sign-out |
| Firewall blocking outbound traffic, not just inbound | Scoped `DOCKER-USER` iptables rules to the external interface only — an unscoped rule silently broke Caddy's own Let's Encrypt renewals |
| Open signup allowing anyone to register | Allowlist-gated signup for the public demo |
| File downloads exposed as bare `GET` links | Moved to signed `POST` downloads |

Deployed on a hardened Contabo VPS (Ubuntu 24.04) behind Cloudflare (Full-strict SSL) and Caddy (auto Let's Encrypt), with UFW + iptables restricting ports 80/443 to Cloudflare's IP ranges only, and encrypted (`age`) off-host database backups.

---

## 🛠️ Tech Stack

| Layer | Tool | Purpose |
|---|---|---|
| Frontend | Next.js + TypeScript | Upload UI, workspace/file browser, share management |
| Backend | FastAPI + SQLAlchemy | REST API, auth, RBAC, share-link logic |
| Database | PostgreSQL | Users, workspaces, files/versions, permissions, audit log |
| Storage | Supabase Storage (S3-compatible) | File blobs, behind a swappable storage abstraction layer |
| Background jobs | Celery + Redis | Async malware scanning, email dispatch |
| Malware scanning | VirusTotal API | Hash-lookup-first scanning on every upload |
| Email | Resend | Share/invitation/access notifications |
| Reverse proxy / TLS | Caddy + Let's Encrypt | Auto-HTTPS, trusted-proxy IP resolution |
| CDN / Edge | Cloudflare | Proxying, DNS, DDoS protection |
| Hosting | Contabo VPS (Docker Compose) | Self-managed production deployment |

---

## 📡 Architecture

```
Browser (uploader / viewer)
            │
            ▼
   Cloudflare (proxy, DNS, SSL)
            │
            ▼
   Caddy (reverse proxy, auto-TLS,
          trusted X-Forwarded-For)
            │
            ▼
   Next.js frontend  ◄──────┐
            │                │ REST + JWT (httpOnly cookie)
            ▼                │
   FastAPI backend ──────────┘
   ├── Auth — JWT + refresh rotation,
   │   reuse detection
   ├── RBAC — workspace role checked
   │   per route
   ├── Share-link engine — policy
   │   resolved per link, not per file
   └── Audit log — every access,
       share, and permission change
            │
     ┌──────┴───────┐
     ▼              ▼
PostgreSQL     Celery worker (Redis)
(metadata,     ├── VirusTotal scan
 permissions,  │   (hash-lookup-first)
 audit log)    └── Resend email
     │
     ▼
Supabase Storage (file blobs)
```

A file isn't downloadable by anyone until it clears the scan queue — the share-link policy and the scan status are both checked on every download request, not just at upload time.

---

## ⚡ Quick Start

```bash
git clone https://github.com/rizalcodes/sharekeep.git
cd sharekeep
```

**Backend:**

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
cp .env.example .env                             # fill in DATABASE_URL, JWT_SECRET, SUPABASE_*, VIRUSTOTAL_API_KEY, RESEND_API_KEY
alembic upgrade head
uvicorn app.main:app --reload
```

**Worker (separate terminal):**

```bash
celery -A app.worker worker --loglevel=info
```

**Frontend:**

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

---

## 📁 Project Structure

```
sharekeep/
├── backend/
│   ├── app/
│   │   ├── core/            # config, security, RBAC
│   │   ├── models/           # SQLAlchemy models
│   │   ├── routers/            # auth, files, shares, workspaces, audit
│   │   ├── services/             # storage abstraction, VirusTotal, email
│   │   └── worker/                 # Celery tasks (scan, notifications)
│   └── alembic/                      # migrations
├── frontend/
│   └── app/                            # Next.js App Router pages
├── deploy/                               # VPS provisioning, firewall, Caddy, backups
└── PRD.md · Schema.md · Architecture.md · Rules.md    # spec docs, locked before implementation
```

---

## 👤 Author

**Rizal**

[![Portfolio](https://img.shields.io/badge/Portfolio-rizalcodes.github.io-0A66C2?style=flat-square)](https://rizalcodes.github.io)
[![GitHub](https://img.shields.io/badge/GitHub-rizalcodes-181717?style=flat-square&logo=github)](https://github.com/rizalcodes)
[![Twitter/X](https://img.shields.io/badge/X-@rizalcodes_-000000?style=flat-square&logo=x)](https://x.com/rizalcodes_)

---

*Anyone can wire up an upload button. The harder part is making sure what comes out the other end of that link is safe, permissioned correctly, and traceable — that's what this one is actually about.*
