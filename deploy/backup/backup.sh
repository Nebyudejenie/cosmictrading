#!/usr/bin/env bash
# Host-side backup run (cosmic-backup.service, daily). Takes an encrypted backup, then applies
# retention (14 daily / 8 weekly / 6 monthly). Prune only runs after a successful backup.
# Environment: COSMIC_IMAGE_TAG (required), COMPOSE_FILE (default: this repo's compose.prod.yaml).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose=(docker compose -f "${COMPOSE_FILE:-$here/../compose.prod.yaml}")
if [ -n "${COSMIC_COMPOSE_OVERLAY:-}" ]; then compose+=(-f "$here/../../$COSMIC_COMPOSE_OVERLAY"); fi
compose+=(--profile backup)
: "${COSMIC_IMAGE_TAG:?set COSMIC_IMAGE_TAG}"

"${compose[@]}" run --rm --no-deps backup
"${compose[@]}" run --rm --no-deps backup-prune
