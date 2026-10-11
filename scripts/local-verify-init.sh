#!/usr/bin/env bash
# Prepare var/verify/ for the local production-like stack: throwaway random secrets, a
# non-secret env file and an age key pair for the restore drill. Idempotent; never prints
# a secret. Everything here is gitignored and disposable.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
v="$root/var/verify"
mkdir -p "$v/config" "$v/secrets" "$v/backups"
chmod 0700 "$v" "$v/backups"
chmod 0711 "$v/secrets"
rnd() { head -c "$1" /dev/urandom | base64 | tr -d '/+=\n'; }
put() { # name value — create once, readable by the container user (throwaway only)
  local f="$v/secrets/$1"
  [ -e "$f" ] || { printf '%s' "$2" > "$f"; chmod 0444 "$f"; }
}
put db_superuser_password "$(rnd 32)"
put db_owner_password "$(rnd 32)"
put db_app_password "$(rnd 32)"
put secret_key "$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
put metrics_token "$(rnd 32)"
for optional in groq_api_key gemini_api_key openrouter_api_key telegram_bot_token tunnel_token; do
  put "$optional" ""   # empty = NOT_CONFIGURED
done
[ -e "$v/owner-password" ] || { (umask 077; rnd 24 > "$v/owner-password"); }

if [ ! -e "$v/config/cosmic.env" ]; then
  cat > "$v/config/cosmic.env" <<ENV
COSMIC_ENV=PAPER_TRADING
COSMIC_DB_NAME=cosmic
COSMIC_ALLOWED_HOSTS=["127.0.0.1","localhost"]
COSMIC_COOKIE_SECURE=true
COSMIC_LOG_LEVEL=INFO
COSMIC_LOG_JSON=true
COSMIC_MAX_AUTHORITY_MODE=PAPER_TRADING
COSMIC_MARKET_DATA_ENABLED=true
ENV
fi
image="cosmictrading-backup:${COSMIC_IMAGE_TAG:-local}"
if [ ! -e "$v/drill-identity.txt" ]; then
  (umask 077; docker run --rm --entrypoint age-keygen "$image" > "$v/drill-identity.txt" 2>/dev/null)
fi
docker run --rm -i --entrypoint age-keygen "$image" -y < "$v/drill-identity.txt" > "$v/config/backup-recipients.txt"
echo "var/verify prepared (secrets generated, values not shown)"
