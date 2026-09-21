# Deploying sharekeep.xyz (M8b)

Every step here is run **by the developer on the VPS**; nothing in this repository has an
account on that host (Rules.md §7). Each script is idempotent, prints what it did, and
prints nothing secret — so its output can be pasted back for review.

Target: Contabo, Ubuntu 24.04, `158.220.109.109`. Found fresh at M8b — no Docker, nothing
on 80/443, no n8n containers — so this project stands up its own reverse proxy
(Architecture.md §7, §23).

    client → Cloudflare (proxy ON) → Caddy (deploy/proxy) → fileshare-frontend / -backend

---

## 0. Before touching the VPS

**a. Push this repository somewhere the VPS can clone from.** From the project root:

    gh repo create sharekeep --private --source=. --remote=origin --push

`.env` is gitignored, so no secret travels with it. Then add a **read-only deploy key**:
on the VPS run `ssh-keygen -t ed25519 -C sharekeep-deploy -f ~/.ssh/sharekeep_deploy`,
and paste `~/.ssh/sharekeep_deploy.pub` into the repo's Settings → Deploy keys, **without**
write access.

**b. Generate the backup key pair on your own machine** — not on the VPS, which is the
whole point:

    age-keygen -o ~/.age/fileshare-backup.key

Keep that file. Its `# public key: age1...` line is what goes into the VPS `.env` as
`FILESHARE_BACKUP_AGE_RECIPIENT`. Without the private key a backup cannot be read, by us
or by anyone who takes the server.

**c. Supabase:** create two private buckets, `fileshare-prod` and `fileshare-backups`,
with the same per-file size limit as the development bucket.

**d. Resend:** add the domain `mail.sharekeep.xyz`, put the DKIM/SPF/MX records it gives
you into Cloudflare DNS, and wait for it to verify.

**e. Cloudflare:** set SSL/TLS mode to **Full** for now. It becomes **Full (strict)** in
step 4, once Caddy actually holds a certificate — set strict too early and every request
fails with a 526 while the certificate is still being issued.

---

## 1. Docker

    ./deploy/01-install-docker.sh

Installs Docker CE and the Compose plugin from Docker's own repository, adds you to the
`docker` group, creates the external `proxy` network, and installs `age`.

**Log out and back in afterwards** (or `newgrp docker`) so the group membership applies.

## 2. Firewall

    ./deploy/02-firewall.sh

Admits SSH, and admits 80/443 **only from Cloudflare's published ranges**. This is part of
the client-address fix from M8a rather than hardening on top of it: the origin address is
public, and anything that can reach 443 directly can forge the whole `X-Forwarded-For`
chain and hand itself any client address it likes (Architecture.md §4.4).

The rules go in Docker's `DOCKER-USER` chain, because **ufw does not filter traffic to
ports a container published** — a `ufw deny 443` there looks correct and blocks nothing.
A systemd unit re-applies them after Docker on every boot.

## 3. The code and its configuration

    sudo mkdir -p /opt/fileshare && sudo chown "$USER" /opt/fileshare
    GIT_SSH_COMMAND='ssh -i ~/.ssh/sharekeep_deploy' \
      git clone git@github.com:<you>/sharekeep.git /opt/fileshare
    cd /opt/fileshare
    cp deploy/env.production.example .env && chmod 600 .env
    ${EDITOR:-nano} .env

Fill in every blank. Generate secrets **on this host** and never reuse a development
value:

    openssl rand -base64 48   # FILESHARE_JWT_SECRET
    openssl rand -base64 32   # FILESHARE_POSTGRES_PASSWORD

The API refuses to start in production when a setting is wrong in a way that would fail
quietly later — a localhost origin, insecure cookies, `TRUSTED_PROXY_HOPS` still 0, the
development mail redirect, Resend's sandbox sender, a short JWT secret. A mistake here
therefore shows up as a container that will not come up, naming the variable.

## 4. The proxy, and the certificate

    cd /opt/fileshare/deploy/proxy
    echo "CADDY_ACME_EMAIL=you@example.com" > .env
    docker compose up -d
    docker compose logs -f caddy      # watch for "certificate obtained successfully"

Caddy takes a Let's Encrypt certificate over HTTP-01, which Cloudflare forwards to the
origin even while proxying. The upstreams do not exist yet, so the hosts will answer 502
until step 5 — that is expected; what matters here is the certificate.

**Once the certificate is in the log, set Cloudflare SSL/TLS mode to Full (strict).**

## 5. The application

    cd /opt/fileshare
    ./deploy/03-deploy.sh

Builds the images (the frontend bundle inlines the public origins at build time, which is
why it is built here and not promoted from a laptop), starts Postgres and Redis, applies
migrations explicitly, brings up the rest, and reads `/health/ready` from inside the
network. This is also the update path: `git pull && ./deploy/03-deploy.sh`.

## 6. Backups

    ./deploy/04-backup.sh              # one run now
    ./deploy/04-backup.sh --install    # nightly timer at 03:20 UTC

Dumps Postgres with the container's own `pg_dump` (so client and server versions always
match), encrypts it to your age public key before it leaves the host, copies it to the
`fileshare-backups` bucket, and keeps the newest seven locally.

## 7. Prove the backup restores

    ssh vps 'cd /opt/fileshare && ./deploy/05-restore-drill.sh' < ~/.age/fileshare-backup.key

Streams the private key in on stdin — never written to the VPS, never visible in `ps` —
decrypts the newest backup, restores it into a scratch database, compares row counts with
the live one table by table, and drops the scratch database afterwards. Fewer rows than
live is expected (the dump is older); more is a failure.

---

## What M8c then verifies from outside

- `https://sharekeep.xyz` and `https://api.sharekeep.xyz` serve over TLS; `www` redirects.
- `https://158.220.109.109` does **not** answer — the firewall holds.
- The audit log records the real visitor address, not Cloudflare's edge — which is the
  `TRUSTED_PROXY_HOPS=2` arithmetic tested against a real request rather than reasoned
  about.
- Security headers, CSP with a nonce, cookies `Secure` and `__Host-` prefixed.
- A CSRF refusal, and a forged `X-Forwarded-For` failing to buy fresh rate-limit budget.
- An invitation reaching a second person's real inbox.
- The restore drill, and the worker heartbeat under scan load.
