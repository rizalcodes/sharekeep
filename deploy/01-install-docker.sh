#!/usr/bin/env bash
# Install Docker Engine and the Compose plugin on Ubuntu 24.04, and create the shared
# proxy network (Architecture.md §7).
#
# Run as root or as a sudo user - both work. Safe to re-run: every step checks first.
set -euo pipefail

# Works whether this runs as root (Contabo's default login) or as a sudo user. Under
# `set -u` a bare ${USER} is an unbound variable in root's non-login shell, which would
# abort on the first line rather than on anything meaningful.
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO=sudo
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"

say() { printf '\n== %s ==\n' "$1"; }

say "Checking the host"
. /etc/os-release
echo "OS      : ${PRETTY_NAME}"
echo "Kernel  : $(uname -r)"
echo "CPUs    : $(nproc)   RAM: $(free -h | awk '/^Mem:/{print $2}')   Free disk: $(df -h / | awk 'NR==2{print $4}')"
if [ "${VERSION_ID:-}" != "24.04" ]; then
  echo "WARNING: written for Ubuntu 24.04; continuing anyway."
fi

if command -v docker >/dev/null 2>&1; then
  say "Docker already installed"
  docker --version
  docker compose version
else
  say "Installing Docker from Docker's own apt repository"
  # Not Ubuntu's docker.io package: that one lags, and the Compose plugin ships from here.
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq ca-certificates curl gnupg git
  $SUDO install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    $SUDO curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    $SUDO chmod a+r /etc/apt/keyrings/docker.asc
  fi
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker
  docker --version
  docker compose version
fi

say "Docker group membership"
# Membership lets a user run Docker without sudo - which is root-equivalent on this host.
# That is why deployment runs as you and no agent has an account here (Rules.md 7).
if [ "$(id -u)" -eq 0 ]; then
  echo "running as root: nothing to add"
elif id -nG "$RUN_USER" | tr ' ' '
' | grep -qx docker; then
  echo "$RUN_USER is already a member"
else
  $SUDO usermod -aG docker "$RUN_USER"
  echo "added $RUN_USER - log out and back in (or run: newgrp docker) first"
fi

say "Creating the shared proxy network"
if $SUDO docker network inspect proxy >/dev/null 2>&1; then
  echo "network 'proxy' already exists"
else
  $SUDO docker network create proxy
  echo "created network 'proxy'"
fi

say "Installing age, for encrypting database dumps before they leave this host"
if command -v age >/dev/null 2>&1; then
  echo "age already installed: $(age --version)"
else
  $SUDO apt-get install -y -qq age
  echo "installed: $(age --version)"
fi

say "Done"
echo "Paste back the output above, then run 02-firewall.sh."
