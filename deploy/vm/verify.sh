#!/usr/bin/env bash
# Read-only verification ON THE VM after deployment (B-02 acceptance; docs/12).
# Usage: deploy/vm/verify.sh   (production: /srv/cosmic/src; staging: /srv/cosmic-staging/src)
#   Shared test host (other stacks run there; no sudo):
#   COSMIC_ROOT=~/apps/cosmictrading COSMIC_SHARED_HOST=1 \
#   COSMIC_COMPOSE_OVERLAY=deploy/compose.test-server.yaml deploy/vm/verify.sh
set -uo pipefail
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
R="${COSMIC_ROOT:-$(dirname "$src")}"  # the root holding this checkout, as deploy.sh uses
shared="${COSMIC_SHARED_HOST:-0}"
fails=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; fails=$((fails + 1)); fi; }
set -a
# shellcheck disable=SC1091
. "$R/config/compose.env"
set +a
export COSMIC_SECRETS_DIR="$R/secrets" COSMIC_CONFIG_DIR="$R/config" COSMIC_BACKUP_DIR="$R/backups" COSMIC_ENV_FILE="$R/config/cosmic.env"
overlay="${COSMIC_COMPOSE_OVERLAY:-}"
dc() { docker compose -f "$src/deploy/compose.prod.yaml" ${overlay:+-f "$src/$overlay"} "$@"; }
project="$(dc config --format json 2>/dev/null | jq -r .name 2>/dev/null)"
[ -n "$project" ] || { echo "FAIL  cannot read the compose project"; exit 1; }
production=0; [ "$project" = cosmictrading ] && [ "$shared" != 1 ] && production=1
echo "checking project $project in $R"

healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' "$(dc ps -q "$1")")" = healthy ]; }
no_lan_ports() { # only sshd may listen on a non-loopback address
  ! ss -H -ltn | awk '{print $4}' | grep -v -E '^(127\.[0-9.]+(%lo)?|\[::1\]|\[::ffff:127\.[0-9.]+\]):' |
    grep -v -E ':22$' | grep -q .  # 127.0.0.0/8 is loopback (systemd-resolved listens on .53/.54)
}
no_project_ports() {
  ! docker ps --filter "label=com.docker.compose.project=$project" --format '{{.Ports}}' | grep -q -- '->'
}
boot_unit_ok() { ! systemctl is-failed --quiet cosmictrading.service; }  # enabled is not enough
restart_policies() {
  ! docker ps -q --filter "label=com.docker.compose.project=$project" |
    xargs docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' | grep -v -x unless-stopped | grep -q .
}
recent_backup() { find "$R/backups" -maxdepth 1 -name 'cosmic-*.manifest.json' -mmin -1560 | grep -q .; }
disk_ok() { [ "$(df --output=pcent /var/lib/docker | tail -1 | tr -dc 0-9)" -lt 85 ]; }
secrets_private() { # no other user may read a secret: closed directory, or closed files
  [ -z "$(find "$R/secrets" -maxdepth 0 -perm /o=rwx)" ] || ! find "$R/secrets" -type f -perm /o=rwx | grep -q .
}

check "postgres healthy" healthy postgres
check "web healthy" healthy web
check "worker healthy" healthy worker
check "readyz inside the web container" dc exec -T web cosmic probe web
check "audit chain verifies" dc exec -T worker cosmic audit verify
if [ "$shared" = 1 ]; then  # host firewall, units and other projects' ports are not ours
  check "no cosmictrading container publishes a port" no_project_ports
  check "containers restart with Docker (restart policy)" restart_policies
  check "daily backup scheduled for this user (crontab)" sh -c "crontab -l | grep -q cosmic-backup"
elif [ "$production" = 1 ]; then
  check "firewall active" sh -c "ufw status | grep -q 'Status: active'"
  check "stack starts on boot (cosmictrading.service enabled)" systemctl is-enabled cosmictrading.service
  check "cosmictrading.service has not failed" boot_unit_ok
  check "backup timer enabled" systemctl is-enabled cosmic-backup.timer
  check "backup timer running (scheduled now, not only at the next boot)" \
    systemctl is-active cosmic-backup.timer
  check "no application port listening on the LAN" no_lan_ports
else  # staging on the production VM: loopback relay only, no units, no backups
  check "no application port listening on the LAN" no_lan_ports
  check "staging portal answers on the loopback relay" \
    curl -fsS --max-time 5 "http://127.0.0.1:${COSMIC_STAGING_PORT:-8744}/readyz"
  check "staging backup timer running" systemctl is-active cosmic-staging-backup.timer
fi
if [ "$production" = 1 ] || [ "$shared" = 1 ]; then
  check "a backup from the last 26 hours exists" recent_backup
fi
check "secret files not world-readable" secrets_private
check "disk below 85% used" disk_ok
live_disabled() { dc exec -T worker cosmic golive status | grep -q 'live trading: DISABLED'; }
check "live trading disabled (no live adapter)" live_disabled
if [ "$fails" -eq 0 ]; then echo "VM VERIFIED"; else echo "$fails check(s) failed"; exit 1; fi
