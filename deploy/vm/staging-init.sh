#!/usr/bin/env bash
# Create the STAGING root on the production VM, next to /srv/cosmic (docs/15 §8). Idempotent.
# DRY RUN BY DEFAULT; changes the system only with --apply (as root, after bootstrap.sh).
# Never overwrites an existing file. Staging gets fresh secrets of its own, and the live tokens
# (tunnel, Telegram, HFM bridge, Binance, AI) stay EMPTY: staging never takes production's
# traffic, Telegram updates or broker connections.
#
#   sudo deploy/vm/staging-init.sh [--apply]       (from /srv/cosmic/src)
#   then: sudo /srv/cosmic-staging/src/deploy/deploy.sh <tag>
set -euo pipefail
APPLY=0; [ "${1:-}" = --apply ] && APPLY=1
S=/srv/cosmic-staging
P=/srv/cosmic
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
run() { printf '+ %s\n' "$*"; if [ "$APPLY" -eq 1 ]; then "$@"; fi; }
write_new() { # dest content — create once with root:10001 0640, never overwrite
  if [ -e "$1" ]; then echo "= $1 exists (kept)"; return; fi
  printf '+ write %s\n' "$1"
  if [ "$APPLY" -eq 1 ]; then
    printf '%s\n' "$2" > "$1.tmp" && chmod 0640 "$1.tmp" && chown root:10001 "$1.tmp" && mv "$1.tmp" "$1"
  fi
}
new_secret() { # name generator — create once, never print, never overwrite
  local f="$S/secrets/$1"
  if [ -e "$f" ]; then echo "= secret $1 exists (kept)"; return; fi
  printf '+ generate secret %s\n' "$1"
  if [ "$APPLY" -eq 1 ]; then (umask 077; $2 > "$f"); chown root:10001 "$f"; chmod 0440 "$f"; fi
}
if [ "$APPLY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then echo "--apply must run as root" >&2; exit 1; fi
getent group 10001 >/dev/null || { echo "run deploy/vm/bootstrap.sh first (group 10001)" >&2; exit 1; }
owner="${SUDO_USER:-root}"

echo "# 1. directories and checkout"
run install -d -m 0755 -o root -g root "$S"
run install -d -m 0750 -o root -g 10001 "$S/config" "$S/secrets"
run install -d -m 0750 -o 10001 -g 10001 "$S/backups"
[ -e "$S/backups/restore-drills.jsonl" ] ||  # appended by a drill run as the admin
  run install -m 0660 -o 10001 -g 10001 /dev/null "$S/backups/restore-drills.jsonl"
if [ ! -d "$S/src/.git" ]; then
  run install -d -m 0755 -o "$owner" -g "$owner" "$S/src"
  run sudo -u "$owner" git clone -q "$(git -C "$here/../.." remote get-url origin)" "$S/src"
fi

echo "# 2. secrets (fresh; live tokens empty)"
pw() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n'; }
key() { head -c 32 /dev/urandom | base64 | tr -d '\n'; }
new_secret db_superuser_password pw
new_secret db_owner_password pw
new_secret db_app_password pw
new_secret secret_key key
new_secret metrics_token pw
for empty in groq_api_key gemini_api_key openrouter_api_key telegram_bot_token tunnel_token \
             binance_testnet_api_key binance_testnet_private_key hfm_bridge_token; do
  new_secret "$empty" true
done

echo "# 3. configuration"
write_new "$S/config/cosmic.env" 'COSMIC_ENV=STAGING
COSMIC_DB_NAME=cosmic
COSMIC_ALLOWED_HOSTS=["127.0.0.1","localhost"]
COSMIC_COOKIE_SECURE=true
COSMIC_DEBUG=false
COSMIC_LOG_LEVEL=INFO
COSMIC_LOG_JSON=true
COSMIC_MAX_AUTHORITY_MODE=PAPER_TRADING
COSMIC_AI_POLICY=FREE_CLOUD_AI_ONLY
COSMIC_TELEGRAM_APPROVAL_REQUIRES_TOTP=true
COSMIC_PAPER_FAULT_INJECTION=false
COSMIC_MARKET_DATA_ENABLED=true
COSMIC_RETIRED_VENUES=["COINBASE","BITSTAMP"]
COSMIC_DISK_WARNING_FREE_PCT=15
COSMIC_DISK_CRITICAL_FREE_PCT=10'
write_new "$S/config/compose.env" "# Staging on the production VM: never set COMPOSE_PROJECT_NAME or the tunnel/telegram profiles.
COSMIC_IMAGE_TAG=
COMPOSE_PROFILES=
COSMIC_COMPOSE_OVERLAY=deploy/compose.staging.yaml
COSMIC_SECRETS_DIR=$S/secrets
COSMIC_CONFIG_DIR=$S/config
COSMIC_BACKUP_DIR=$S/backups
COSMIC_ENV_FILE=$S/config/cosmic.env
COSMIC_EDGE_SUBNET=10.203.64.32/27
COSMIC_EDGE_CLOUDFLARED_IP=10.203.64.42
COSMIC_EDGE_WEB_IP=10.203.64.52
COSMIC_STAGING_PORT=8744"
if [ -e "$S/config/backup-recipients.txt" ]; then
  echo "= $S/config/backup-recipients.txt exists (kept)"
elif [ -s "$P/config/backup-recipients.txt" ]; then  # the owner's age PUBLIC key
  run install -m 0640 -o root -g 10001 "$P/config/backup-recipients.txt" "$S/config/backup-recipients.txt"
else
  echo "  note: put the owner's age public key in $S/config/backup-recipients.txt"
fi

echo "# 4. daily staging backup (03:30 UTC), so the staging worker's backup check holds"
unit="[Unit]
Description=COSMICTRADING QUANT staging backup
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=$S/src
EnvironmentFile=$S/config/compose.env
ExecStart=$S/src/deploy/backup/backup.sh
Nice=15"
timer='[Unit]
Description=Daily COSMICTRADING QUANT staging backup (03:30 UTC)

[Timer]
OnCalendar=*-*-* 03:30:00 UTC
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target'
for pair in "cosmic-staging-backup.service:$unit" "cosmic-staging-backup.timer:$timer"; do
  name="${pair%%:*}"; content="${pair#*:}"
  if [ -e "/etc/systemd/system/$name" ]; then echo "= $name exists (kept)"; continue; fi
  printf '+ write /etc/systemd/system/%s\n' "$name"
  [ "$APPLY" -eq 0 ] || printf '%s\n' "$content" > "/etc/systemd/system/$name"
done
run systemctl daemon-reload
run systemctl enable --now cosmic-staging-backup.timer  # enabled alone waits for a reboot

cat <<NEXT

Next: sudo $S/src/deploy/deploy.sh <tag>   (images built on this VM; no pre-deploy backup the
first time). Owner account: docker compose run with both compose files, docs/15 §8.
NEXT
[ "$APPLY" -eq 1 ] || echo "# DRY RUN — nothing was changed. Re-run with --apply as root."
