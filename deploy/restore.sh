#!/usr/bin/env bash
# Replace the database with a backup. Stops the app while restoring.
#   sudo /opt/parlaytracker/deploy/restore.sh /var/backups/parlaytracker/parlaytracker-....dump
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$0")/lib.sh"

dump=${1:?usage: restore.sh BACKUP_FILE [--yes]}
[[ -f $dump ]] || { echo "no such file: $dump" >&2; exit 1; }
if [[ ${2:-} != --yes ]]; then
  read -rp "This replaces ALL current data with $dump. Type 'restore' to go on: " answer
  [[ $answer == restore ]] || { echo "cancelled"; exit 1; }
fi

log "stopping the app"
compose stop web worker
compose exec -T db dropdb -U parlaytracker --if-exists parlaytracker
compose exec -T db createdb -U parlaytracker parlaytracker
compose exec -T db pg_restore -U parlaytracker -d parlaytracker --no-owner < "$dump"
log "restored $dump; starting the app"
compose up -d
