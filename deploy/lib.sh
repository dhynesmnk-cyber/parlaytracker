# Shared by the deploy scripts. Sourced, not run.
# shellcheck shell=bash
# Variables here are used by the scripts that source this file.
# shellcheck disable=SC2034
ROOT=${ROOT:-/opt/parlaytracker}
BRANCH=${BRANCH:-main}
STATE_DIR=/var/lib/parlaytracker
BACKUP_DIR=/var/backups/parlaytracker

# Deploy settings (ROOT, BRANCH) can be pinned in /etc/default/parlaytracker.
if [[ -f /etc/default/parlaytracker ]]; then
  # shellcheck disable=SC1091
  source /etc/default/parlaytracker
fi

compose() {
  docker compose --project-name parlaytracker --env-file "$ROOT/.env" \
    -f "$ROOT/deploy/docker-compose.yml" "$@"
}

env_value() {  # env_value NAME: the value of NAME in .env, or empty
  grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -n1 | cut -d= -f2-
}

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
