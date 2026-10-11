#!/usr/bin/env bash
# READ-ONLY inventory of a Cloudflare zone's DNS records, taken BEFORE any tunnel hostname is
# added (docs/12 §5, directive: "existing records have been verified"). Only HTTP GET is used.
#
# Usage: dns-inventory.sh <zone-name> <api-token-file> [output.tsv]
#   The token needs only Zone:Read + DNS:Read. It is read from a file and passed to curl via
#   a header file descriptor, so it never appears in argv, the process list or shell history.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: $0 <zone> <token-file> [out.tsv]" >&2; exit 2; }
zone="$1"; token_file="$2"; out="${3:-dns-inventory-$zone-$(date -u +%Y%m%dT%H%M%SZ).tsv}"
[ -r "$token_file" ] || { echo "cannot read $token_file" >&2; exit 2; }
api=https://api.cloudflare.com/client/v4
get() { # GET only — this script never sends POST, PUT, PATCH or DELETE
  curl --fail --silent --show-error --request GET \
    --header @<(printf 'Authorization: Bearer %s\n' "$(tr -d '\n' < "$token_file")") \
    --header 'Accept: application/json' "$api$1"
}
zone_json=$(get "/zones?name=$zone")
zone_id=$(jq -r '.result[0].id // empty' <<<"$zone_json")
[ -n "$zone_id" ] || { echo "zone $zone not visible to this token" >&2; exit 1; }
jq -r '.result[0] | "zone \(.name) status=\(.status) nameservers=\(.name_servers | join(","))"' <<<"$zone_json"

page=1; : > "$out"
printf 'type\tname\tcontent\tproxied\tttl\tcomment\n' >> "$out"
while :; do
  body=$(get "/zones/$zone_id/dns_records?per_page=100&page=$page")
  jq -r '.result[] | [.type, .name, .content, (.proxied|tostring), (.ttl|tostring), (.comment // "")] | @tsv' <<<"$body" >> "$out"
  total=$(jq -r '.result_info.total_pages' <<<"$body")
  [ "$page" -ge "$total" ] && break
  page=$((page + 1))
done
echo "$(($(wc -l < "$out") - 1)) records written to $out"
echo "Before adding <portal-host>: confirm no record with that name exists (CNAME/A/AAAA)."
