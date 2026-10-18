#!/usr/bin/env bash
# Deploy the current commit to the staging server (docs/15 §8).
#   scripts/deploy-staging.sh            # via `ssh cosmictrading` (works from anywhere)
#   COSMIC_STAGING_SSH=cosmictrading-lan scripts/deploy-staging.sh   # faster at home
# An interrupted upload resumes where it stopped: run the script again (or let its retries run).
# Options: COSMIC_UPLOAD_KBPS (cap, KiB/s), COSMIC_UPLOAD_ATTEMPTS (default 30),
# COSMIC_BUILD_NETWORK=host (when build containers cannot resolve names).
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
cache="${XDG_CACHE_HOME:-$HOME/.cache}/cosmictrading"
mkdir -p "$cache"
exec 9>"$cache/.deploy.lock"
flock -n 9 || { echo "refusing: another deploy is running" >&2; exit 2; }
# COSMIC_UPLOAD_KBPS caps the upload (KiB/s) to leave room on a shared line, e.g. for the bridge.
attempts="${COSMIC_UPLOAD_ATTEMPTS:-30}"
[[ "$attempts" =~ ^[1-9][0-9]*$ ]] ||
  { echo "refusing: COSMIC_UPLOAD_ATTEMPTS must be a whole number above 0" >&2; exit 2; }
limit=()
if [ -n "${COSMIC_UPLOAD_KBPS:-}" ]; then
  [[ "$COSMIC_UPLOAD_KBPS" =~ ^[0-9]+$ ]] ||
    { echo "refusing: COSMIC_UPLOAD_KBPS must be a whole number of KiB/s" >&2; exit 2; }
  limit=(--bwlimit="$COSMIC_UPLOAD_KBPS")
fi
echo "== building $tag"
# COSMIC_BUILD_NETWORK=host when the build containers cannot resolve names on Docker's default
# network (seen on the workstation's native engine on 2026-10-17).
net=()
[ -z "${COSMIC_BUILD_NETWORK:-}" ] || net=(--network "$COSMIC_BUILD_NETWORK")
docker build -q "${net[@]}" -f deploy/Dockerfile -t "cosmictrading:$tag" . >/dev/null
docker build -q "${net[@]}" -t "cosmictrading-backup:$tag" deploy/backup >/dev/null
if [ "$(git rev-parse --short HEAD)" != "$tag" ] || [ -n "$(git status --porcelain)" ]; then
  echo "refusing: the checkout changed during the build" >&2; exit 1
fi
# The backup image (about 120 MB compressed) rarely changes: when deploy/backup is the same as in
# the release the server runs, that release's image is tagged on the server instead of resent.
# Probes of the server: "nothing there" is an answer, an ssh that fails is not. A rerun must
# choose the same images, or it cannot resume the same upload.
probe() {
  # shellcheck disable=SC2029  # expanded locally on purpose
  ssh -o BatchMode=yes "$host" "$1" ||
    { echo "refusing: cannot reach $host to check the running release" >&2; exit 1; }
}
running="$(probe "docker inspect --format '{{.Config.Image}}' cosmictrading-worker-1 2>/dev/null || true")"
running="${running##*:}"
images=("cosmictrading:$tag" "cosmictrading-backup:$tag")
reuse=""
backup_there=no
if [ -n "$running" ] && git cat-file -e "$running^{commit}" 2>/dev/null &&
  git diff --quiet "$running" "$tag" -- deploy/backup; then
  backup_there="$(probe "docker image inspect cosmictrading-backup:$running >/dev/null 2>&1 &&
    echo yes || echo no")"
fi
if [ "$backup_there" = yes ]; then
  images=("cosmictrading:$tag")
  reuse="$running"
fi
# The images travel as one compressed file that rsync resumes after a dropped connection (the
# Cloudflare SSH path drops on a slow line); the server checks its SHA-256 before loading it.
# The file is named by the commit and the number of images, never by image IDs: a rebuild of the
# same commit gets new IDs on an engine that records build provenance, and a rerun must resume
# the same file. Other bundles are removed only after a successful load.
bundle="cosmictrading-images-$tag-${#images[@]}.tar.zst"
incoming="apps/cosmictrading/incoming"
if [ -s "$cache/$bundle" ] && ! zstd -q -t --long=27 "$cache/$bundle" 2>/dev/null; then
  echo "   the cached $bundle is damaged; compressing again" >&2
  rm -f "$cache/$bundle"
fi
if [ ! -s "$cache/$bundle" ]; then
  echo "== compressing ${images[*]}"
  tmp="$(mktemp "$cache/$bundle.XXXXXX")"
  docker save "${images[@]}" | zstd -q -f -T0 -19 --long=27 -o "$tmp"
  mv "$tmp" "$cache/$bundle"
fi
echo "== sending ${images[*]} to $host ($(du -h "$cache/$bundle" | cut -f1))"
# One Ctrl-C stops the deploy (rsync would report it as a failed attempt and the loop would
# carry on). Until the images have loaded and deploy.sh runs, nothing on the server changes.
trap 'echo "interrupted: nothing was deployed" >&2; exit 130' INT TERM
# shellcheck disable=SC2029  # expanded locally on purpose
ssh -o BatchMode=yes "$host" "mkdir -p $incoming"
sent=""
for attempt in $(seq 1 "$attempts"); do
  rc=0
  rsync --partial --append-verify --timeout=120 --info=progress2 "${limit[@]}" \
    -e "ssh -o BatchMode=yes -o ServerAliveInterval=15" \
    "$cache/$bundle" "$host:$incoming/$bundle" || rc=$?
  case "$rc" in
    0) sent=1; break ;;
    10 | 12 | 20 | 30 | 35 | 255)
      # dropped, stalled, or stopped at the server's end (a local Ctrl-C ends in the trap)
      if [ "$attempt" -lt "$attempts" ]; then
        echo "   upload interrupted (attempt $attempt, rsync code $rc); resuming" >&2
        sleep 20
      fi ;;
    *) echo "refusing: rsync failed with code $rc, which is not a dropped connection" >&2; exit 1 ;;
  esac
done
[ -n "$sent" ] || { echo "refusing: the upload did not finish; run again to resume it" >&2; exit 1; }
sum="$(sha256sum "$cache/$bundle" | cut -d' ' -f1)"
# shellcheck disable=SC2029  # expanded locally on purpose
ssh -o BatchMode=yes "$host" "cd $incoming &&
  if ! echo '$sum  $bundle' | sha256sum -c --quiet - 2>/dev/null; then
    rm -f $bundle; echo 'refusing: the uploaded file is damaged; removed it, run again' >&2; exit 1
  fi && zstd -q -d --long=27 -c $bundle | docker load &&
  find . -maxdepth 1 -name 'cosmictrading-images-*' -delete" | tail -2
find "$cache" -maxdepth 1 -name 'cosmictrading-images-*' -delete
trap - INT TERM
if [ -n "$reuse" ]; then
  echo "== deploy/backup unchanged since $reuse: tagging its backup image as $tag"
  # shellcheck disable=SC2029  # expanded locally on purpose
  ssh -o BatchMode=yes "$host" "docker tag cosmictrading-backup:$reuse cosmictrading-backup:$tag"
fi
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
