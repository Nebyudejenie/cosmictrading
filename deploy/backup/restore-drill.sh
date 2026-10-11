#!/usr/bin/env bash
# Restore drill (ADR-019, AT-19): restore a backup set into a throwaway, network-less container
# and verify checksums, schema revision, the audit hash chain, the pre-dump audit head,
# append-only row counts and immutability triggers. Production is never touched.
#
# Usage: restore-drill.sh <backup-dir> <stem> <age-identity-file | ->
#   "-" reads the identity from stdin, so the private key never has to be stored on the VM.
# Environment: COSMIC_BACKUP_IMAGE (default cosmictrading-backup:${COSMIC_IMAGE_TAG:-local})
set -euo pipefail
[ $# -eq 3 ] || { echo "usage: $0 <backup-dir> <stem> <identity-file|->" >&2; exit 2; }
dir="$(cd "$1" && pwd)"; stem="$2"; identity="$3"
image="${COSMIC_BACKUP_IMAGE:-cosmictrading-backup:${COSMIC_IMAGE_TAG:-local}}"
for f in dump.age config.tar.age sha256 manifest.json; do
  [ -f "$dir/$stem.$f" ] || { echo "missing $dir/$stem.$f" >&2; exit 2; }
done
name="cosmic-restore-drill-$(date -u +%Y%m%d%H%M%S)-$$"
pw="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"

cleanup() { docker rm -f -v "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# --network none: the drill database cannot reach or be reached by anything.
docker run -d --name "$name" --network none \
  -e POSTGRES_PASSWORD="$pw" \
  --tmpfs /run/drill:size=1m,mode=0700 \
  -v "$dir:/backups:ro" \
  "$image" postgres >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$name" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$name" pg_isready -h 127.0.0.1 -U postgres >/dev/null

if [ "$identity" = "-" ]; then
  docker exec -i "$name" sh -c 'umask 077; cat > /run/drill/identity'
else
  docker exec -i "$name" sh -c 'umask 077; cat > /run/drill/identity' < "$identity"
fi
start=$(date +%s)
docker exec "$name" cosmic-drill-check "$stem"
echo "drill duration: $(( $(date +%s) - start ))s (container $name removed on exit)"
