#!/usr/bin/env bash
# Nightly database backup, kept 14 days on the laptop and copied off it with rclone.
# Run by parlaytracker-backup.timer; safe to run by hand.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$0")/lib.sh"
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
file="$BACKUP_DIR/parlaytracker-$(date -u +%Y-%m-%d-%H%M).dump"
compose exec -T db pg_dump -U parlaytracker -Fc parlaytracker > "$file.part"
mv "$file.part" "$file"
log "backup written: $file ($(du -h "$file" | cut -f1))"
find "$BACKUP_DIR" -name 'parlaytracker-*.dump' -mtime +"$KEEP_DAYS" -delete

remote=$(env_value BACKUP_RCLONE_REMOTE)
if [[ -n $remote ]]; then
  rclone copy "$file" "$remote"
  log "copied to $remote"
else
  log "WARNING: BACKUP_RCLONE_REMOTE is not set, so this backup only exists on the laptop"
fi
