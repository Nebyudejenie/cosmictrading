#!/usr/bin/env bash
# Release the pushed commit on the production VM (docs/15 §8). The VM builds the images itself,
# so nothing large crosses the owner's line. They go to staging first, which is then checked;
# with --production the same images go to production, which is checked on the VM and through
# Cloudflare Access.
#   scripts/release.sh [--production]
#   COSMIC_VM_SSH=cosmictrading-lan scripts/release.sh     # at home (default: cosmictrading)
# sudo on the VM needs no password for deploy.sh and verify.sh (the rule bootstrap.sh installs;
# the owner chose it on 2026-10-20); without that rule, run it in a terminal to type it.
# Guard rails: clean tree and HEAD pushed to origin/main; each checkout on the VM is moved to
# exactly that commit (deploy.sh --build refuses any other); production only after staging
# passed in the same run, with the images staging runs.
set -euo pipefail
prod=0
case "${1:-}" in
  "") ;;
  --production) prod=1 ;;
  *) echo "usage: $0 [--production]" >&2; exit 2 ;;
esac
host="${COSMIC_VM_SSH:-cosmictrading}"
cd "$(git rev-parse --show-toplevel)"
[ -z "$(git status --porcelain)" ] || { echo "refusing: working tree has uncommitted changes" >&2; exit 2; }
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] ||
  { echo "refusing: HEAD is not the pushed origin/main" >&2; exit 2; }
sha="$(git rev-parse HEAD)"
tag="$(git rev-parse --short=7 HEAD)"  # fixed length: two clones may abbreviate differently
cache="${XDG_CACHE_HOME:-$HOME/.cache}/cosmictrading"
mkdir -p "$cache"
exec 9>"$cache/.deploy.lock"
flock -n 9 || { echo "refusing: another release is running" >&2; exit 2; }

# One session per root, so sudo asks once. The checkout belongs to the admin; only deploy.sh and
# verify.sh run as root. Output is not filtered: through a terminal, errors arrive on stdout.
release() { # root [deploy.sh flags]
  local root="$1" flags="${2:-}"
  # shellcheck disable=SC2029  # expanded locally on purpose
  ssh -t -o ServerAliveInterval=15 "$host" "set -e; cd $root/src
    git fetch -q origin
    git checkout -q --detach $sha
    test \"\$(git rev-parse HEAD)\" = $sha || { echo \"$root/src is not at $sha\" >&2; exit 1; }
    sudo $root/src/deploy/deploy.sh $flags $tag
    sudo $root/src/deploy/vm/verify.sh"
}

echo "== staging: build $tag on $host, deploy, check"
release /srv/cosmic-staging --build
if [ "$prod" -eq 0 ]; then
  echo "== $tag is on staging; to release it: scripts/release.sh --production"
  exit 0
fi
echo "== production: deploy $tag (the images staging runs), check"
release /srv/cosmic
tokens="$HOME/.secrets/cosmictrading-access-service.json"
if [ -r "$tokens" ]; then
  echo "== public readiness through Cloudflare Access"
  hdr="$(mktemp)"; trap 'rm -f "$hdr"' EXIT; chmod 600 "$hdr"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("CF-Access-Client-Id: " + d["client_id"]); print("CF-Access-Client-Secret: " + d["client_secret"])' "$tokens" > "$hdr"
  curl -fsS --max-time 20 -H "@$hdr" https://staging.cosmictrading.bond/readyz; echo
fi
echo "== $tag released"
