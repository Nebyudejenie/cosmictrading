#!/usr/bin/env bash
# Local production-like verification (docs/12 §7): build the images, run compose.prod.yaml
# with the local overlay, drive the real portal, kill/restart containers, back up, restore
# into an isolated container and measure resource use. Writes var/verify/run-<UTC>.log.
#
#   scripts/local-verify.sh [--fresh]
#
# --fresh removes ONLY the throwaway project "cosmictrading-verify" and its own volume.
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root" || exit 2
vc="$root/scripts/verify-compose.sh"
v="$root/var/verify"
mkdir -p "$v"
logf="$v/run-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee "$logf") 2>&1
declare -a summary=()
step() { # name command...
  local name="$1"; shift
  printf '\n== %s\n' "$name"
  local t0; t0=$(date +%s)
  if "$@"; then summary+=("PASS  $name ($(( $(date +%s) - t0 ))s)")
  else summary+=("FAIL  $name ($(( $(date +%s) - t0 ))s)"); fi
}

build() {
  docker build -q -f deploy/Dockerfile -t cosmictrading:local . &&
  docker build -q -t cosmictrading-backup:local deploy/backup
}
fresh() {
  "$vc" --profile backup down -v --remove-orphans
  rm -f "$v/driver-state.json"
}
owner() {
  local pw out
  pw="$(cat "$v/owner-password")"
  out=$(printf 'owner\nVerification Owner\n%s\n%s\n' "$pw" "$pw" |
    "$vc" run --rm -T --no-deps --entrypoint cosmic migrate admin create-owner 2>&1 | grep -iv "warning\|password")
  echo "$out"
  [[ "$out" == *"created"* || "$out" == *"already exists"* ]]
}
backup() {
  "$vc" --profile backup run --rm --no-deps backup &&
  "$vc" --profile backup run --rm --no-deps backup-prune
}
drill() {
  local stem
  stem=$(find "$v/backups" -maxdepth 1 -name 'cosmic-*.manifest.json' -printf '%f\n' | sort | tail -1)
  stem="${stem%.manifest.json}"
  [ -n "$stem" ] || { echo "no backup found"; return 1; }
  ls -l "$v/backups/$stem".*
  deploy/backup/restore-drill.sh "$v/backups" "$stem" "$v/drill-identity.txt"
}
plaintext_check() { # no unencrypted dump may exist in the backup directory
  ! find "$v/backups" -type f ! -name '*.age' ! -name '*.sha256' ! -name '*.manifest.json' | grep -q .
}
stats() {
  local ids
  mapfile -t ids < <(docker ps --filter name=cosmictrading-verify -q)
  for i in 1 2 3 4 5 6; do  # six samples 10 s apart (single samples catch healthcheck spikes)
    echo "-- sample $i"
    docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}' \
      "${ids[@]}"
    [ "$i" -lt 6 ] && sleep 10
  done
  docker system df --format 'table {{.Type}}\t{{.Size}}' | head -5
  docker image ls --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E '^cosmictrading'
  docker exec cosmictrading-verify-postgres-1 psql -U postgres -XAtc \
    "SELECT 'database size: ' || pg_size_pretty(pg_database_size('cosmic'))"
}

step "build images" build
step "prepare var/verify (throwaway secrets)" ./scripts/local-verify-init.sh
[ "${1:-}" = "--fresh" ] && step "remove previous verification project" fresh
step "start stack (compose up --wait)" "$vc" up -d --wait --wait-timeout 300
step "create owner via CLI" owner
step "portal flow: login, MFA, paper trade, approval, kill switch" \
  uv run python scripts/local_verify.py --phase portal
step "AT-20 chaos: SIGKILL worker after approvals" \
  uv run python scripts/local_verify.py --phase chaos --iterations "${CHAOS_ITERATIONS:-4}"
step "restart: docker compose restart with resting orders" \
  uv run python scripts/local_verify.py --phase restart
step "audit chain verifies (CLI in worker container)" "$vc" exec -T worker cosmic audit verify
step "encrypted backup + retention" backup
step "no plaintext in the backup directory" plaintext_check
step "restore drill into isolated container (AT-19)" drill
step "resource usage" stats

printf '\n== summary\n'
printf '%s\n' "${summary[@]}"
printf 'log: %s\n' "${logf#"$root"/}"
! printf '%s\n' "${summary[@]}" | grep -q '^FAIL'
