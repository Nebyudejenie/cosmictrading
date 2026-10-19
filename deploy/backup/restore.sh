#!/usr/bin/env bash
# Disaster-recovery restore into a NEW database on the production server. Never overwrites
# or drops an existing database. Afterwards: run the drill checks against it, then point
# COSMIC_DB_NAME at it and restart web/worker (docs/15, "Restore from backup").
#
# Usage: restore.sh <backup-dir> <stem> <target-db-name> <age-identity-file | ->
set -euo pipefail
[ $# -eq 4 ] || { echo "usage: $0 <backup-dir> <stem> <target-db> <identity|->" >&2; exit 2; }
dir="$(cd "$1" && pwd)"; stem="$2"; target="$3"; identity="$4"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose=(docker compose -f "${COMPOSE_FILE:-$here/../compose.prod.yaml}")
if [ -n "${COSMIC_COMPOSE_OVERLAY:-}" ]; then compose+=(-f "$here/../../$COSMIC_COMPOSE_OVERLAY"); fi
[[ "$target" =~ ^[a-z][a-z0-9_]{2,40}$ ]] || { echo "target db name must match ^[a-z][a-z0-9_]{2,40}$" >&2; exit 2; }
# Checks run inside the backup container: the files belong to UID 10001 (mode 0600).
# Every call before the decryption reads /dev/null, never stdin: with "-" the age identity
# arrives on stdin, and docker compose would otherwise swallow it.
"${compose[@]}" --profile backup run --rm --no-deps -T -v "$dir:/restore:ro" \
  --entrypoint sh backup -c "cd /restore && sha256sum -c '$stem.sha256'" </dev/null

exists=$("${compose[@]}" exec -T postgres psql -U postgres -XAtq -c \
  "SELECT 1 FROM pg_database WHERE datname = '$target'" </dev/null)
[ -z "$exists" ] || { echo "database $target already exists; choose a new name" >&2; exit 2; }
"${compose[@]}" exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -XAtq \
  -c "CREATE DATABASE $target OWNER cosmic_owner" </dev/null

# Decrypt on the host side of the pipe; the plaintext dump only exists in the pipe.
if [ "$identity" = "-" ]; then id_src=/dev/stdin; else id_src="$identity"; fi
"${compose[@]}" --profile backup run --rm --no-deps -T -v "$dir:/restore:ro" \
  --entrypoint sh backup -c "umask 077; cat > /tmp/identity && age -d -i /tmp/identity /restore/$stem.dump.age" < "$id_src" \
  | "${compose[@]}" exec -T postgres pg_restore -U postgres --exit-on-error --dbname "$target"
echo "restored $stem into database $target. Verify, then set COSMIC_DB_NAME=$target and restart web and worker."
