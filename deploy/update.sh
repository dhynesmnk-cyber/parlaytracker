#!/usr/bin/env bash
# Deploy the latest commit on $BRANCH if it hasn't been deployed yet. Run every 5 minutes by
# parlaytracker-update.timer. A failure leaves the running containers untouched and is retried.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$0")/lib.sh"

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/update.lock"
flock -n 9 || { log "another update is running"; exit 0; }

git -C "$ROOT" fetch -q origin "$BRANCH"
target=$(git -C "$ROOT" rev-parse "origin/$BRANCH")
deployed=$(cat "$STATE_DIR/deployed" 2>/dev/null || true)
[[ $target == "$deployed" ]] && exit 0

log "deploying $target (was ${deployed:-nothing})"
git -C "$ROOT" reset -q --hard "$target"
compose build
compose run --rm migrate
compose up -d --remove-orphans
echo "$target" > "$STATE_DIR/deployed"
log "deployed $target"
