#!/usr/bin/env bash
# Install Docker Engine and the Compose plugin on Ubuntu 24.04, and create the shared
# proxy network (Architecture.md §7).
#
# Run as a normal user with sudo. Safe to re-run: every step checks first.
set -euo pipefail

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
  sudo apt-get update -qq
  sudo apt-get install -y -qq ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
  fi
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
  docker --version
  docker compose version
fi

say "Adding ${USER} to the docker group"
# Group membership means this user can run Docker without sudo — which is root-equivalent
# on this host. That is why the deployment runs as you and no agent has an account here
# (Rules.md §7).
if id -nG "${USER}" | tr ' ' '\n' | grep -qx docker; then
  echo "already a member"
else
  sudo usermod -aG docker "${USER}"
  echo "added — log out and back in (or run: newgrp docker) before the next script"
fi

say "Creating the shared proxy network"
if sudo docker network inspect proxy >/dev/null 2>&1; then
  echo "network 'proxy' already exists"
else
  sudo docker network create proxy
  echo "created network 'proxy'"
fi

say "Installing age, for encrypting database dumps before they leave this host"
if command -v age >/dev/null 2>&1; then
  echo "age already installed: $(age --version)"
else
  sudo apt-get install -y -qq age
  echo "installed: $(age --version)"
fi

say "Done"
echo "Paste back the output above, then run 02-firewall.sh."
