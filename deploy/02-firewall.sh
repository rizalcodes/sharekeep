#!/usr/bin/env bash
# Admit 80 and 443 only from Cloudflare, and SSH from anywhere (Architecture.md §7, M8b).
#
# This is part of the client-address fix from M8a, not hardening on top of it. The origin
# address is public: anything that can reach port 443 directly presents a request that
# never passed through Cloudflare, and can invent the whole X-Forwarded-For chain — which
# would hand it any client address it liked and undo every per-IP limit (§4.4).
#
# THE SECOND SUBTLETY (found when Caddy could not reach Let's Encrypt): DOCKER-USER hangs
# off the FORWARD chain, which carries container EGRESS as well as ingress. A rule matching
# "--dports 80,443" with no interface therefore drops every outbound HTTPS call a container
# makes - ACME, Supabase, VirusTotal, Resend - while looking like an inbound rule. Every
# rule below is pinned to the public interface with -i, so it can only match traffic
# arriving from outside. Host traffic (apt, docker pull) never traverses FORWARD at all,
# which is why the host looked healthy while containers had no internet.
#
# THE SUBTLETY: ufw does not filter traffic to ports a container published. Docker writes
# its own DNAT and FORWARD rules, and that path never traverses ufw's INPUT chain, so a
# ufw "deny 443" with a published port is a rule that looks right and blocks nothing. The
# rules that matter for Caddy therefore go in DOCKER-USER, which Docker consults first and
# never flushes. ufw still matters for anything listening on the host itself.
#
# Run as root or as a sudo user - both work. Safe to re-run: idempotent throughout.
set -euo pipefail

# Works whether this runs as root (Contabo's default login) or as a sudo user. Under
# `set -u` a bare ${USER} is an unbound variable in root's non-login shell, which would
# abort on the first line rather than on anything meaningful.
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO=sudo
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"

say() { printf '\n== %s ==\n' "$1"; }

V4_URL=https://www.cloudflare.com/ips-v4
V6_URL=https://www.cloudflare.com/ips-v6
SSH_PORT="${SSH_PORT:-22}"

# Where the last good answer is kept. At boot this script runs again, and the network may
# not be usable yet: fetching is attempted, the cache is the fallback, and if neither can
# supply a list the chain is closed rather than left open (see below).
CACHE_DIR=/etc/fileshare
CACHE_V4="$CACHE_DIR/cloudflare-ips.v4"
CACHE_V6="$CACHE_DIR/cloudflare-ips.v6"

# The floor is per family, because the two lists are not the same size: Cloudflare
# publishes 15 IPv4 ranges and only 7 IPv6 ones. A single floor of 10 rejected every
# healthy IPv6 fetch and sent the whole script down the fail-closed path with the network
# working perfectly — which would have closed 80/443 on a site that had never opened.
# Found by running it against the real endpoints, not by reading it.
plausible() { [ "$(printf '%s' "$1" | grep -c '/')" -ge "$2" ]; }

fetch_with_retries() {
  # Three attempts: at boot, DNS and routing often arrive a second or two after the unit.
  for attempt in 1 2 3; do
    if body=$(curl -fsS --max-time 20 "$1" 2>/dev/null) && plausible "$body" "$2"; then
      printf '%s' "$body"
      return 0
    fi
    sleep 5
  done
  return 1
}

say "Fetching Cloudflare's published ranges"
if V4=$(fetch_with_retries "$V4_URL" 10) && V6=$(fetch_with_retries "$V6_URL" 5); then
  echo "fetched: $(echo "$V4" | wc -l) IPv4, $(echo "$V6" | wc -l) IPv6"
  $SUDO mkdir -p "$CACHE_DIR"
  printf '%s\n' "$V4" | $SUDO tee "$CACHE_V4" >/dev/null
  printf '%s\n' "$V6" | $SUDO tee "$CACHE_V6" >/dev/null
elif [ -s "$CACHE_V4" ]; then
  V4=$(cat "$CACHE_V4")
  V6=$(cat "$CACHE_V6" 2>/dev/null || true)
  echo "WARNING: could not reach Cloudflare; using the cached list from $CACHE_V4"
  echo "cached: $(echo "$V4" | wc -l) IPv4, $(echo "$V6" | wc -l) IPv6"
else
  # Fail CLOSED. The alternative — exiting before any rule is installed — leaves 80/443
  # open to the whole internet, which is the opposite of what this script advertises, and
  # it would happen silently at boot. A site that is unreachable is loud and fixable; an
  # origin quietly exposed is neither.
  echo "ERROR: no Cloudflare list and no cache. Closing 80/443 entirely." >&2
  $SUDO iptables -N DOCKER-USER 2>/dev/null || true
  # Pinned to the public interface like every other rule here: closing the site is the
  # intent, cutting the containers off from the internet is not.
  CLOSE_IF=$(ip route show default | awk '{print $5; exit}')
  $SUDO iptables -A DOCKER-USER ${CLOSE_IF:+-i "$CLOSE_IF"} -p tcp -m multiport --dports 80,443 \
    -m comment --comment fileshare-cf-drop -j DROP
  exit 1
fi

say "ufw: SSH in, everything else out by default"
$SUDO apt-get install -y -qq ufw >/dev/null
$SUDO ufw --force default deny incoming
$SUDO ufw --force default allow outgoing
# Added BEFORE enabling, so enabling cannot end this session.
$SUDO ufw allow "${SSH_PORT}/tcp" comment 'SSH'
$SUDO ufw --force enable
$SUDO ufw status verbose | sed -n '1,12p'

say "DOCKER-USER: 80 and 443 from Cloudflare only, INBOUND only"
# The interface the default route leaves by. Rules are pinned to it so they cannot match
# a container's own outbound connections, which arrive on a docker bridge instead.
EXT_IF=$(ip route show default | awk '{print $5; exit}')
if [ -z "$EXT_IF" ]; then
  echo "ERROR: no default route, so the public interface cannot be identified." >&2
  exit 1
fi
echo "public interface: $EXT_IF"
# Docker creates DOCKER-USER itself; create it if this runs before Docker ever has.
$SUDO iptables -N DOCKER-USER 2>/dev/null || true
# Flush only our own marked rules, so re-running does not stack duplicates and does not
# disturb anything else in the chain. This also clears the boot-time DROP the unit installs
# before calling this script, which is why that rule carries the same marker.
$SUDO iptables -S DOCKER-USER \
  | grep -- '--comment fileshare-cf' \
  | sed 's/^-A/-D/' \
  | while read -r rule; do
      # shellcheck disable=SC2086
      $SUDO iptables ${rule}
    done || true

for cidr in $V4; do
  $SUDO iptables -I DOCKER-USER -i "$EXT_IF" -p tcp -s "$cidr" -m multiport --dports 80,443 \
    -m comment --comment fileshare-cf -j RETURN
done
# Anything else arriving on the public interface for those ports skipped Cloudflare.
$SUDO iptables -A DOCKER-USER -i "$EXT_IF" -p tcp -m multiport --dports 80,443 \
  -m comment --comment fileshare-cf-drop -j DROP

if command -v ip6tables >/dev/null 2>&1; then
  $SUDO ip6tables -N DOCKER-USER 2>/dev/null || true
  $SUDO ip6tables -S DOCKER-USER \
    | grep -- '--comment fileshare-cf' \
    | sed 's/^-A/-D/' \
    | while read -r rule; do
        # shellcheck disable=SC2086
        $SUDO ip6tables ${rule}
      done || true
  for cidr in $V6; do
    $SUDO ip6tables -I DOCKER-USER -i "$EXT_IF" -p tcp -s "$cidr" -m multiport --dports 80,443 \
      -m comment --comment fileshare-cf -j RETURN
  done
  $SUDO ip6tables -A DOCKER-USER -i "$EXT_IF" -p tcp -m multiport --dports 80,443 \
    -m comment --comment fileshare-cf-drop -j DROP
fi

echo "IPv4 rules now in DOCKER-USER:"
$SUDO iptables -S DOCKER-USER | sed -n '1,6p'
echo "  ... $($SUDO iptables -S DOCKER-USER | grep -c 'fileshare-cf') fileshare rules total"

say "Telling Caddy which proxy to believe"
# Caddy only preserves an incoming X-Forwarded-For when the request came from an address
# in trusted_proxies; otherwise it REPLACES the header with the peer's address. Observed,
# not assumed: without this, the backend received a single entry (Cloudflare's edge) and
# recorded that as the visitor, so every audit row showed a Cloudflare address and every
# per-IP limit counted the whole world as one visitor. Generated here so the firewall and
# the proxy cannot disagree about who Cloudflare is (Architecture 4.4).
TRUSTED_CONF="$CACHE_DIR/cloudflare-trusted-proxies.conf"
{
  printf 'trusted_proxies static'
  for cidr in $V4 $V6; do printf ' %s' "$cidr"; done
  printf '\n'
} | $SUDO tee "$TRUSTED_CONF" >/dev/null
echo "wrote $TRUSTED_CONF ($(wc -w < "$TRUSTED_CONF") entries)"
echo "  reload Caddy to pick it up: cd deploy/proxy && docker compose up -d --force-recreate caddy"

say "Making them survive a reboot"
SCRIPT_PATH=$(readlink -f "$0")
IPTABLES=$(command -v iptables || echo /usr/sbin/iptables)
# A systemd unit rather than iptables-persistent: it re-applies AFTER docker starts, so
# ordering between Docker's own chain setup and these rules cannot matter.
UNIT=/etc/systemd/system/fileshare-firewall.service
$SUDO tee "$UNIT" >/dev/null <<UNIT_EOF
[Unit]
Description=Restrict container ports 80/443 to Cloudflare ranges (fileshare)
# network-online as well as docker: this script fetches Cloudflare's ranges, and at boot
# the network is often a moment behind the unit. It falls back to its cache and then to
# closing the ports, but waiting first means the common case needs neither.
Wants=network-online.target
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
# Closed FIRST, reopened for Cloudflare by the script below. The boot this was written
# after failed with 203/EXEC - systemd could not execute the script at all - and the
# result was not "fail closed" but no rules whatever: the promise only held while the
# script ran. These two lines hold it even when it does not. The "-" prefix lets them
# fail harmlessly when the chain is already as they want it.
ExecStartPre=-$IPTABLES -N DOCKER-USER
ExecStartPre=-$IPTABLES -I DOCKER-USER -i $EXT_IF -p tcp -m multiport --dports 80,443 -m comment --comment fileshare-cf-bootdrop -j DROP
# Invoked through bash rather than directly: a clone that lost the executable bit (git
# records mode, and a chmod on Windows does not reach the index) would otherwise break
# this again.
ExecStart=/bin/bash $SCRIPT_PATH
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT_EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable fileshare-firewall.service >/dev/null
echo "enabled fileshare-firewall.service (re-runs this script after Docker on boot)"

say "Done"
echo "Cached ranges: $CACHE_V4"
echo "Verification belongs in M8c: https://sharekeep.xyz must work while"
echo "https://158.220.109.109 must not answer at all."
