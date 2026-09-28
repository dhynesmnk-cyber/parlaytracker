#!/usr/bin/env bash
# docker compose for ParlayTracker, with the right project, file and .env. Examples:
#   sudo /opt/parlaytracker/deploy/compose.sh logs -f worker
#   sudo /opt/parlaytracker/deploy/compose.sh up -d        # after editing .env
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$0")/lib.sh"
compose "$@"
