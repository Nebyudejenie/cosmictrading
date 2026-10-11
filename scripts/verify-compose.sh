#!/usr/bin/env bash
# docker compose for the local production-like stack (compose.prod.yaml + local-verify overlay).
# Usage: scripts/verify-compose.sh <compose args...>
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
v="$root/var/verify"
export COSMIC_IMAGE_TAG="${COSMIC_IMAGE_TAG:-local}"
export COSMIC_SECRETS_DIR="$v/secrets" COSMIC_CONFIG_DIR="$v/config" COSMIC_BACKUP_DIR="$v/backups"
export COSMIC_ENV_FILE="$v/config/cosmic.env"
COSMIC_VERIFY_UID="$(id -u)"; COSMIC_VERIFY_GID="$(id -g)"
export COSMIC_VERIFY_UID COSMIC_VERIFY_GID
exec docker compose -f "$root/deploy/compose.prod.yaml" -f "$root/deploy/compose.local-verify.yaml" "$@"
