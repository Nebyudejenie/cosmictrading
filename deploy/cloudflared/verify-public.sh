#!/usr/bin/env bash
# Verify a public deployment layer by layer (docs/12 §5). Read-only HTTP/DNS checks.
# A deployment is called "deployed" only when every check passes.
#
# Usage: verify-public.sh <portal-hostname>
#   Optional: CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET_FILE for an Access service token
#   to check the application behind Access.
set -uo pipefail
host="${1:?usage: $0 <portal-hostname>}"
fails=0
check() { local name="$1"; shift; if "$@"; then echo "PASS  $name"; else echo "FAIL  $name"; fails=$((fails + 1)); fi; }

dns_cname() { dig +short CNAME "$host" | grep -q 'cfargotunnel\.com\.$' || dig +short "$host" | grep -q .; }
proxied() { curl -sI "https://$host/" | grep -qi '^server: cloudflare'; }
tls_ok() { curl -s -o /dev/null --max-time 15 "https://$host/"; }
access_gate() { # unauthenticated → Access login redirect, never the app
  local code loc
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://$host/approvals")
  loc=$(curl -s -o /dev/null -w '%{redirect_url}' "https://$host/approvals")
  [[ "$code" =~ ^30[1-3]$ && "$loc" == *cloudflareaccess.com* ]]
}
internal_hidden() { # never served publicly (404 from ingress, or Access in front)
  local code; code=$(curl -s -o /dev/null -w '%{http_code}' "https://$host/internal/metrics")
  [[ "$code" == 404 || "$code" =~ ^30[1-3]$ ]]
}
app_ready() {
  if [ -z "${CF_ACCESS_CLIENT_ID:-}" ] || [ ! -r "${CF_ACCESS_CLIENT_SECRET_FILE:-}" ]; then
    echo "      (skipped: no Access service token)"; return 0
  fi
  curl -s --fail -o /dev/null \
    --header @<(printf 'CF-Access-Client-Id: %s\nCF-Access-Client-Secret: %s\n' \
      "$CF_ACCESS_CLIENT_ID" "$(tr -d '\n' < "$CF_ACCESS_CLIENT_SECRET_FILE")") \
    "https://$host/readyz"
}

check "DNS resolves through Cloudflare (tunnel CNAME)" dns_cname
check "served by Cloudflare edge" proxied
check "TLS handshake and certificate valid" tls_ok
check "Cloudflare Access challenges unauthenticated requests" access_gate
check "/internal/* not publicly served" internal_hidden
check "application /readyz behind Access (service token)" app_ready
echo "On the VM also run: ss -ltnp  → no application port listening on the LAN interface."
if [ "$fails" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$fails check(s) failed"; exit 1; fi
