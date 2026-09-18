#!/usr/bin/env bash
# Admit 80 and 443 only from Cloudflare, and SSH from anywhere (Architecture.md §7, M8b).
#
# This is part of the client-address fix from M8a, not hardening on top of it. The origin
# address is public: anything that can reach port 443 directly presents a request that
# never passed through Cloudflare, and can invent the whole X-Forwarded-For chain — which
# would hand it any client address it liked and undo every per-IP limit (§4.4).
#
# THE SUBTLETY: ufw does not filter traffic to ports a container published. Docker writes
# its own DNAT and FORWARD rules, and that path never traverses ufw's INPUT chain, so a
# ufw "deny 443" with a published port is a rule that looks right and blocks nothing. The
# rules that matter for Caddy therefore go in DOCKER-USER, which Docker consults first and
# never flushes. ufw still matters for anything listening on the host itself.
#
# Run as a normal user with sudo. Safe to re-run: idempotent throughout.
set -euo pipefail

say() { printf '\n== %s ==\n' "$1"; }

V4_URL=https://www.cloudflare.com/ips-v4
V6_URL=https://www.cloudflare.com/ips-v6
SSH_PORT="${SSH_PORT:-22}"

say "Fetching Cloudflare's published ranges"
V4=$(curl -fsS "$V4_URL")
V6=$(curl -fsS "$V6_URL")
echo "$(echo "$V4" | wc -l) IPv4 ranges, $(echo "$V6" | wc -l) IPv6 ranges"
# A truncated or hijacked answer here would lock the site away from its own CDN, so refuse
# anything that does not look like the real list.
if [ "$(echo "$V4" | wc -l)" -lt 10 ]; then
  echo "ERROR: that is not Cloudflare's list. Aborting without touching the firewall." >&2
  exit 1
fi

say "ufw: SSH in, everything else out by default"
sudo apt-get install -y -qq ufw >/dev/null
sudo ufw --force default deny incoming
sudo ufw --force default allow outgoing
# Added BEFORE enabling, so enabling cannot end this session.
sudo ufw allow "${SSH_PORT}/tcp" comment 'SSH'
sudo ufw --force enable
sudo ufw status verbose | sed -n '1,12p'

say "DOCKER-USER: 80 and 443 from Cloudflare only"
# Docker creates DOCKER-USER itself; create it if this runs before Docker ever has.
sudo iptables -N DOCKER-USER 2>/dev/null || true
# Flush only our own marked rules, so re-running does not stack duplicates and does not
# disturb anything else in the chain.
sudo iptables -S DOCKER-USER \
  | grep -- '--comment fileshare-cf' \
  | sed 's/^-A/-D/' \
  | while read -r rule; do
      # shellcheck disable=SC2086
      sudo iptables ${rule}
    done || true

for cidr in $V4; do
  sudo iptables -I DOCKER-USER -p tcp -s "$cidr" -m multiport --dports 80,443 \
    -m comment --comment fileshare-cf -j RETURN
done
# Anything else reaching those ports is a request that skipped Cloudflare.
sudo iptables -A DOCKER-USER -p tcp -m multiport --dports 80,443 \
  -m comment --comment fileshare-cf-drop -j DROP

if command -v ip6tables >/dev/null 2>&1; then
  sudo ip6tables -N DOCKER-USER 2>/dev/null || true
  sudo ip6tables -S DOCKER-USER \
    | grep -- '--comment fileshare-cf' \
    | sed 's/^-A/-D/' \
    | while read -r rule; do
        # shellcheck disable=SC2086
        sudo ip6tables ${rule}
      done || true
  for cidr in $V6; do
    sudo ip6tables -I DOCKER-USER -p tcp -s "$cidr" -m multiport --dports 80,443 \
      -m comment --comment fileshare-cf -j RETURN
  done
  sudo ip6tables -A DOCKER-USER -p tcp -m multiport --dports 80,443 \
    -m comment --comment fileshare-cf-drop -j DROP
fi

echo "IPv4 rules now in DOCKER-USER:"
sudo iptables -S DOCKER-USER | sed -n '1,6p'
echo "  ... $(sudo iptables -S DOCKER-USER | grep -c 'fileshare-cf') fileshare rules total"

say "Making them survive a reboot"
# A systemd unit rather than iptables-persistent: it re-applies AFTER docker starts, so
# ordering between Docker's own chain setup and these rules cannot matter.
UNIT=/etc/systemd/system/fileshare-firewall.service
sudo tee "$UNIT" >/dev/null <<UNIT_EOF
[Unit]
Description=Restrict container ports 80/443 to Cloudflare ranges (fileshare)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=$(readlink -f "$0")
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT_EOF
sudo systemctl daemon-reload
sudo systemctl enable fileshare-firewall.service >/dev/null
echo "enabled fileshare-firewall.service (re-runs this script after Docker on boot)"

say "Done"
echo "Verification belongs in M8c: https://sharekeep.xyz must work while"
echo "https://158.220.109.109 must not answer at all."
