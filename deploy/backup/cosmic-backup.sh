#!/bin/sh
# shellcheck shell=sh disable=SC3040  # runs under BusyBox ash, which supports pipefail
# Encrypted logical backup (runs inside the backup container; ADR-019).
#   - pg_dump streams straight into age: no plaintext dump ever touches disk.
#   - Output set: <stem>.dump.age, <stem>.config.tar.age, <stem>.sha256, <stem>.manifest.json
#   - Secrets are never archived (/config holds non-secret configuration only).
set -eu
set -o pipefail
umask 077

OUT=/backups
RECIPIENTS=/config/backup-recipients.txt
[ -s "$RECIPIENTS" ] || { echo "backup: $RECIPIENTS is missing or empty (age public key)" >&2; exit 2; }
PGPASSWORD="$(cat /run/secrets/db_owner_password)"
export PGPASSWORD

stamp=$(date -u +%Y%m%dT%H%M%SZ)
stem="cosmic-$stamp"
tmp="$OUT/.partial-$stem"
mkdir "$tmp"
trap 'rm -rf "$tmp"' EXIT

q() { psql -XAtq -v ON_ERROR_STOP=1 -c "$1"; }

# Facts recorded *before* the dump. The dump is a later consistent snapshot, so these are
# lower bounds: the restored audit chain must contain this exact head.
rev=$(q "SELECT version_num FROM alembic_version")
head_seq=$(q "SELECT coalesce(max(seq), 0) FROM audit_events")
head_hash=$(q "SELECT coalesce((SELECT hash FROM audit_events WHERE seq = $head_seq), '')")
counts=$(q "SELECT coalesce(json_object_agg(t, n ORDER BY t), '{}'::json) FROM (
  SELECT c.relname AS t,
         (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]::text::bigint AS n
  FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
  WHERE s.nspname = 'public' AND c.relkind = 'r') x")

pg_dump --format=custom --compress=6 --no-password \
  | age --encrypt --recipients-file "$RECIPIENTS" --output "$tmp/$stem.dump.age"
tar -C /config -cf - . \
  | age --encrypt --recipients-file "$RECIPIENTS" --output "$tmp/$stem.config.tar.age"

(cd "$tmp" && sha256sum "$stem.dump.age" "$stem.config.tar.age" > "$stem.sha256")
dump_bytes=$(stat -c %s "$tmp/$stem.dump.age")
cfg_bytes=$(stat -c %s "$tmp/$stem.config.tar.age")
dump_sha=$(cut -d' ' -f1 < "$tmp/$stem.sha256" | sed -n 1p)
cfg_sha=$(cut -d' ' -f1 < "$tmp/$stem.sha256" | sed -n 2p)

cat > "$tmp/$stem.manifest.json" <<JSON
{
  "stem": "$stem",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "database": "$PGDATABASE",
  "schema_revision": "$rev",
  "pg_dump_version": "$(pg_dump --version)",
  "age_version": "$(age --version)",
  "audit_head_before_dump": {"seq": $head_seq, "hash": "$head_hash"},
  "row_counts_before_dump": $counts,
  "files": [
    {"name": "$stem.dump.age", "bytes": $dump_bytes, "sha256": "$dump_sha"},
    {"name": "$stem.config.tar.age", "bytes": $cfg_bytes, "sha256": "$cfg_sha"}
  ],
  "encryption": "age (X25519 recipients from backup-recipients.txt)"
}
JSON

mv "$tmp"/* "$OUT"/
echo "backup: wrote $stem (dump ${dump_bytes} bytes, audit head seq ${head_seq})"
