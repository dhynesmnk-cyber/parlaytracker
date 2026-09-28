#!/usr/bin/env bash
# One screen of health: containers, versions, timers, backups, data sources, Tailscale.
set -uo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$0")/lib.sh"

echo "== Containers"; compose ps
echo; echo "== Code"
echo "deployed: $(cat "$STATE_DIR/deployed" 2>/dev/null || echo never)"
git -C "$ROOT" log -1 --format='checked out: %h %s (%cr)'
echo; echo "== Timers"; systemctl list-timers 'parlaytracker-*' --no-pager
echo; echo "== Latest backups"
find "$BACKUP_DIR" -name 'parlaytracker-*.dump' -printf '%TY-%Tm-%Td %TH:%TM  %s bytes  %f\n' \
  2>/dev/null | sort -r | head -3
echo; echo "== Data sources"
compose exec -T db psql -U parlaytracker -d parlaytracker -c \
  "SELECT source, state, failure_kind, last_success_at, last_failure_at, last_error
     FROM source_health ORDER BY source"
echo; echo "== Tailscale Serve"; tailscale serve status
