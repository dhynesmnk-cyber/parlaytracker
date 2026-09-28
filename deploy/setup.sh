#!/usr/bin/env bash
# Set up (or repair) ParlayTracker on the laptop. Safe to run again. See deploy/README.md.
#
#   sudo bash setup.sh                  # production, follows main
#   sudo BRANCH=my-branch bash setup.sh # follow another branch (e.g. before a PR is merged)
set -euo pipefail

REPO_URL=${REPO_URL:-https://github.com/dhynesmnk-cyber/parlaytracker.git}
ROOT=${ROOT:-/opt/parlaytracker}
BRANCH=${BRANCH:-main}

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu ]] || echo "warning: only tested on Ubuntu 24.04 LTS"

step "Packages"
apt-get update -q
apt-get install -y -q ca-certificates curl git openssl rclone

step "Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin
fi
systemctl enable --now docker

step "Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "Sign this laptop in to Tailscale: open the link below on any device."
  tailscale up
fi

step "Keep the laptop awake with the lid closed"
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/parlaytracker.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
echo "Lid setting applies after the next reboot. Also turn on 'power on after AC loss'"
echo "in the BIOS if the laptop has it."

step "Code ($BRANCH)"
if [[ -d $ROOT/.git ]]; then
  git -C "$ROOT" fetch -q origin "$BRANCH"
  git -C "$ROOT" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$ROOT"
fi
printf 'ROOT=%s\nBRANCH=%s\n' "$ROOT" "$BRANCH" > /etc/default/parlaytracker
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"

step "Settings ($ROOT/.env)"
if [[ ! -f $ROOT/.env ]]; then
  password=$(openssl rand -hex 24)
  if [[ -t 0 ]]; then
    read -rp "Time zone for showing times [Europe/London]: " tz
    read -rp "Tailscale logins allowed in, comma-separated (see README): " logins
    read -rsp "Odds API key (Enter to add later): " odds; echo
    read -rsp "OpenRouter API key for screenshots (Enter to add later): " qwen; echo
  fi
  umask 077
  sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$password|" \
      -e "s|^DISPLAY_TZ=.*|DISPLAY_TZ=${tz:-Europe/London}|" \
      -e "s|^ALLOWED_LOGINS=.*|ALLOWED_LOGINS=${logins:-}|" \
      -e "s|^ODDS_API_KEY=.*|ODDS_API_KEY=${odds:-}|" \
      -e "s|^QWEN_API_KEY=.*|QWEN_API_KEY=${qwen:-}|" \
      "$ROOT/deploy/env.example" > "$ROOT/.env"
fi
chown root:root "$ROOT/.env"
chmod 600 "$ROOT/.env"
if [[ -z $(env_value ALLOWED_LOGINS) ]]; then
  echo "ALLOWED_LOGINS is empty, so nobody can use the app yet."
  echo "Edit $ROOT/.env, then run this script again."
fi

step "Build and start"
mkdir -p "$STATE_DIR" "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
compose build
compose up -d --remove-orphans
git -C "$ROOT" rev-parse HEAD > "$STATE_DIR/deployed"

step "Timers: updates every 5 minutes, backup nightly"
install -m 0644 "$ROOT"/deploy/systemd/parlaytracker-*.service \
  "$ROOT"/deploy/systemd/parlaytracker-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now parlaytracker-update.timer parlaytracker-backup.timer

step "Tailscale Serve"
tailscale serve --bg http://127.0.0.1:8501
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null http://127.0.0.1:8501/_stcore/health && break
  sleep 2
done
curl -fsS -o /dev/null http://127.0.0.1:8501/_stcore/health || die "the web app didn't start: \
run deploy/status.sh"
tailscale serve status

step "Done"
echo "Open the https://…ts.net address above on a phone that has Tailscale switched on."
