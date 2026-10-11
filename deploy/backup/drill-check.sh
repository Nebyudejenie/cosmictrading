#!/bin/sh
# shellcheck shell=busybox
# Restore-drill checks (runs inside an isolated drill container, see restore-drill.sh).
# Usage: cosmic-drill-check <stem>   (backup files mounted read-only at /backups,
#        identity streamed into /run/drill/identity on a tmpfs)
set -eu
set -o pipefail
stem="$1"
B=/backups
ID=/run/drill/identity
fail() { echo "DRILL FAIL: $*" >&2; exit 1; }
ok() { echo "ok   $*"; }
q() { psql -U postgres -XAtq -v ON_ERROR_STOP=1 -d "${2:-cosmic}" -c "$1"; }  # local socket (trust) inside the drill container
field() { sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$B/$stem.manifest.json" | head -1; }

[ -s "$ID" ] || fail "no identity provided"
(cd "$B" && sha256sum -c "$stem.sha256" >/dev/null) || fail "checksum mismatch"
ok "checksums match $stem.sha256"

for role in cosmic_owner cosmic_app; do
  q "DO \$\$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$role') THEN CREATE ROLE $role NOLOGIN; END IF; END\$\$" postgres
done
q "CREATE DATABASE cosmic OWNER cosmic_owner" postgres
age --decrypt --identity "$ID" "$B/$stem.dump.age" \
  | pg_restore -U postgres --exit-on-error --dbname cosmic
ok "pg_restore completed without errors"

files=$(age --decrypt --identity "$ID" "$B/$stem.config.tar.age" | tar -tf - | grep -vc '/$')
[ "$files" -ge 1 ] || fail "configuration archive is empty"
ok "configuration archive decrypts ($files files)"

rev=$(q "SELECT version_num FROM alembic_version")
[ "$rev" = "$(field schema_revision)" ] || fail "schema revision $rev != manifest"
ok "schema revision $rev matches manifest"

verify=$(q "SELECT ok || ' ' || checked FROM audit_verify()")
[ "${verify%% *}" = "true" ] || fail "audit chain does not verify: $verify"
ok "audit chain verifies (${verify#* } events)"

head_seq=$(sed -n 's/.*"audit_head_before_dump": {"seq": \([0-9]*\).*/\1/p' "$B/$stem.manifest.json")
head_hash=$(sed -n 's/.*"audit_head_before_dump": {[^}]*"hash": "\([0-9a-f]*\)".*/\1/p' "$B/$stem.manifest.json")
if [ "$head_seq" -gt 0 ]; then
  got=$(q "SELECT hash FROM audit_events WHERE seq = $head_seq")
  [ "$got" = "$head_hash" ] || fail "audit event $head_seq differs from the pre-dump head"
fi
ok "pre-dump audit head (seq $head_seq) present and identical"

# Append-only tables (guarded by forbid_mutation) must hold at least the pre-dump rows.
for t in $(q "SELECT DISTINCT event_object_table FROM information_schema.triggers WHERE action_statement LIKE '%forbid_mutation%' ORDER BY 1"); do
  before=$(sed -n "s/.*\"$t\" *: *\([0-9]*\).*/\1/p" "$B/$stem.manifest.json" | head -1)
  now=$(q "SELECT count(*) FROM public.\"$t\"")
  [ -z "$before" ] || [ "$now" -ge "$before" ] || fail "$t has $now rows, fewer than $before before the dump"
done
ok "append-only tables hold at least their pre-dump row counts"

unbalanced=$(q "SELECT count(*) FROM (SELECT txn_id, asset FROM ledger_entries GROUP BY 1, 2 HAVING sum(amount) <> 0) x")
[ "$unbalanced" -eq 0 ] || fail "$unbalanced ledger transaction/asset groups do not balance"
ok "every ledger transaction balances per asset ($(q "SELECT count(DISTINCT txn_id) FROM ledger_entries") transactions)"

gaps=$(q "SELECT count(*) FROM (SELECT order_id FROM order_events GROUP BY 1 HAVING min(seq) <> 1 OR max(seq) <> count(*) OR count(DISTINCT seq) <> count(*)) x")
[ "$gaps" -eq 0 ] || fail "$gaps orders have non-contiguous event sequences"
orphan=$(q "SELECT count(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM order_events e WHERE e.order_id = o.id)")
[ "$orphan" -eq 0 ] || fail "$orphan orders have no events"
ok "order event sequences contiguous for $(q "SELECT count(*) FROM orders") orders"

[ "$(q "SELECT count(*) FROM audit_events")" -gt 0 ] || fail "restored audit log is empty"
if q "UPDATE audit_events SET action = action" >/dev/null 2>&1; then
  fail "audit_events accepted an UPDATE after restore"
fi
if q "TRUNCATE audit_events" >/dev/null 2>&1; then
  fail "audit_events accepted TRUNCATE after restore"
fi
ok "immutability triggers active after restore (UPDATE and TRUNCATE refused)"
echo "DRILL PASS: $stem"
