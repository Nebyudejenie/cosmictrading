#!/usr/bin/env bash
# Read-only verification ON THE VM after deployment (B-02 acceptance; docs/12).
# Usage: sudo deploy/vm/verify.sh      (from /srv/cosmic/src)
set -uo pipefail
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
R=/srv/cosmic
fails=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; fails=$((fails + 1)); fi; }
set -a
# shellcheck disable=SC1091
. "$R/config/compose.env"
set +a
export COSMIC_SECRETS_DIR="$R/secrets" COSMIC_CONFIG_DIR="$R/config" COSMIC_BACKUP_DIR="$R/backups" COSMIC_ENV_FILE="$R/config/cosmic.env"
dc() { docker compose -f "$src/deploy/compose.prod.yaml" "$@"; }

healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' "cosmictrading-$1-1")" = healthy ]; }
no_lan_ports() { # only sshd may listen on a non-loopback address
  ! ss -H -ltn | awk '{print $4}' | grep -v -E '^(127\.0\.0\.1|\[::1\]|\[::ffff:127\.0\.0\.1\]):' | grep -v -E ':22$' | grep -q .
}
recent_backup() { find "$R/backups" -maxdepth 1 -name 'cosmic-*.manifest.json' -mmin -1560 | grep -q .; }
disk_ok() { [ "$(df --output=pcent /var/lib/docker | tail -1 | tr -dc 0-9)" -lt 85 ]; }
secrets_private() { ! find "$R/secrets" -type f -perm /o=rwx | grep -q .; }

check "postgres healthy" healthy postgres
check "web healthy" healthy web
check "worker healthy" healthy worker
check "readyz inside the web container" dc exec -T web cosmic probe web
check "audit chain verifies" dc exec -T worker cosmic audit verify
check "firewall active" sh -c "ufw status | grep -q 'Status: active'"
check "stack starts on boot (cosmictrading.service enabled)" systemctl is-enabled cosmictrading.service
check "backup timer enabled" systemctl is-enabled cosmic-backup.timer
check "no application port listening on the LAN" no_lan_ports
check "a backup from the last 26 hours exists" recent_backup
check "secret files not world-readable" secrets_private
check "disk below 85% used" disk_ok
check "live trading disabled (no live adapter)" sh -c "docker compose -f '$src/deploy/compose.prod.yaml' exec -T worker cosmic golive status | grep -q 'live trading: DISABLED'"
if [ "$fails" -eq 0 ]; then echo "VM VERIFIED"; else echo "$fails check(s) failed"; exit 1; fi
