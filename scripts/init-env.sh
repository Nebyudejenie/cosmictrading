#!/usr/bin/env bash
# Create a local .env with random development secrets. Never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  echo ".env already exists; leaving it unchanged."
  exit 0
fi
umask 077
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
key() { python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'; }
cat > .env <<ENV
# Local development settings (generated $(date -u +%Y-%m-%dT%H:%M:%SZ)). Not for production.
COSMIC_ENV=LOCAL_DEVELOPMENT
COSMIC_DB_HOST=127.0.0.1
COSMIC_DB_PORT=55442
COSMIC_DB_NAME=cosmic
COSMIC_DB_SUPERUSER_PASSWORD=$(rand)
COSMIC_DB_OWNER_PASSWORD=$(rand)
COSMIC_DB_APP_PASSWORD=$(rand)
COSMIC_SECRET_KEY=$(key)
COSMIC_METRICS_TOKEN=$(rand)
COSMIC_COOKIE_SECURE=false
COSMIC_LOG_JSON=false
COSMIC_WEB_PORT=8742
ENV
echo "Created .env with random local secrets (mode 0600)."
