#!/usr/bin/env bash
# Deploy the current commit to the staging server (docs/15 §8).
#   scripts/deploy-staging.sh            # via `ssh cosmictrading` (works from anywhere)
#   COSMIC_STAGING_SSH=cosmictrading-lan scripts/deploy-staging.sh   # faster at home
# Guard rails: clean tree, HEAD pushed to origin/main, tag fixed before building and checked
# again afterwards (a build must never be labelled with a different commit).
set -euo pipefail
host="${COSMIC_STAGING_SSH:-cosmictrading}"
cd "$(git rev-parse --show-toplevel)"
[ -z "$(git status --porcelain)" ] || { echo "refusing: working tree has uncommitted changes" >&2; exit 2; }
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] ||
  { echo "refusing: HEAD is not the pushed origin/main" >&2; exit 2; }
tag="$(git rev-parse --short HEAD)"
echo "== building $tag"
docker build -q -f deploy/Dockerfile -t "cosmictrading:$tag" . >/dev/null
docker build -q -t "cosmictrading-backup:$tag" deploy/backup >/dev/null
[ "$(git rev-parse --short HEAD)" = "$tag" ] && [ -z "$(git status --porcelain)" ] ||
  { echo "refusing: the checkout changed during the build" >&2; exit 1; }
echo "== streaming images to $host"
docker save "cosmictrading:$tag" "cosmictrading-backup:$tag" | gzip -1 |
  ssh -o BatchMode=yes "$host" 'gunzip | docker load' | tail -2
echo "== deploying"
# shellcheck disable=SC2029  # $tag is expanded locally on purpose
ssh -o BatchMode=yes "$host" "cd ~/apps/cosmictrading/src && git fetch -q && git checkout -q $tag &&
  COSMIC_ROOT=\$HOME/apps/cosmictrading deploy/deploy.sh $tag" |
  grep -E '^# |backup:|deploy:|audit chain|live trading'
echo "== server checks"
ssh -o BatchMode=yes "$host" 'cd ~/apps/cosmictrading/src && COSMIC_ROOT=$HOME/apps/cosmictrading \
  COSMIC_SHARED_HOST=1 COSMIC_COMPOSE_OVERLAY=deploy/compose.test-server.yaml deploy/vm/verify.sh' | tail -1
tokens="$HOME/.secrets/cosmictrading-access-service.json"
if [ -r "$tokens" ]; then
  echo "== public readiness through Cloudflare Access"
  hdr="$(mktemp)"; trap 'rm -f "$hdr"' EXIT; chmod 600 "$hdr"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("CF-Access-Client-Id: " + d["client_id"]); print("CF-Access-Client-Secret: " + d["client_secret"])' "$tokens" > "$hdr"
  curl -s --max-time 20 -H "@$hdr" https://staging.cosmictrading.bond/readyz; echo
fi
echo "== $tag deployed"
