#!/usr/bin/env bash
# Monthly restore drill (AT-19, runbook §5), run from the owner's workstation, where the private
# key lives. It copies the newest encrypted production backup set from the VM, runs
# deploy/backup/restore-drill.sh on it here (a throwaway container without network), appends the
# result to restore-drills.jsonl next to the backups on the server (the worker reads it), and
# deletes the local copy. Nothing on the server is changed except that one appended line.
#
# Usage: scripts/drill-backups.sh [--negative] [age-identity-file]
#   default identity: ~/.secrets/cosmictrading-backup-age.key
#   --negative: also prove the drill can fail. A copy with one flipped byte in the dump and a
#   freshly generated wrong key must both be refused; the result line records it.
# Environment: COSMIC_SSH (default cosmictrading), COSMIC_REMOTE_BACKUPS (default
#   /srv/cosmic/backups), COSMIC_IMAGE_TAG (the backup image tag to drill with; default: the tag
#   production's worker runs). The VM builds the images, so a missing backup image is built here
#   from that commit's deploy/backup (COSMIC_BUILD_NETWORK=host if its build cannot resolve names).
set -euo pipefail
negative=0
if [ "${1:-}" = "--negative" ]; then negative=1; shift; fi
host="${COSMIC_SSH:-cosmictrading}"
remote="${COSMIC_REMOTE_BACKUPS:-/srv/cosmic/backups}"
key="${1:-$HOME/.secrets/cosmictrading-backup-age.key}"
here="$(cd "$(dirname "$0")/.." && pwd)"
[ -r "$key" ] || { echo "cannot read the age identity $key" >&2; exit 2; }

ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=25 "$host" "$@"; }

stem="$(ssh_ "ls -1 $remote" | sed -n 's/^\(cosmic-[0-9]\{8\}T[0-9]\{6\}Z\)\.manifest\.json$/\1/p' | sort | tail -1)"
[ -n "$stem" ] || { echo "no backup set found on $host:$remote" >&2; exit 1; }
tag="${COSMIC_IMAGE_TAG:-$(ssh_ "docker inspect --format '{{.Config.Image}}' cosmictrading-worker-1" | sed 's/.*://')}"
if ! docker image inspect "cosmictrading-backup:$tag" >/dev/null 2>&1; then
  git -C "$here" cat-file -e "$tag^{commit}" 2>/dev/null || git -C "$here" fetch -q origin
  git -C "$here" cat-file -e "$tag^{commit}" 2>/dev/null \
    || { echo "commit $tag is unknown here; cannot build its backup image" >&2; exit 2; }
  echo "building cosmictrading-backup:$tag from that commit's deploy/backup"
  net=()
  [ -z "${COSMIC_BUILD_NETWORK:-}" ] || net=(--network "$COSMIC_BUILD_NETWORK")
  git -C "$here" archive --format=tar "$tag:deploy/backup" \
    | docker build -q "${net[@]}" -t "cosmictrading-backup:$tag" - >/dev/null
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
ssh_ "tar -C $remote -cf - $stem.dump.age $stem.config.tar.age $stem.sha256 $stem.manifest.json" \
  | tar -C "$work" -xf -
started="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
t0=$(date +%s)
if COSMIC_IMAGE_TAG="$tag" "$here/deploy/backup/restore-drill.sh" "$work" "$stem" "$key" \
  | tee "$work/drill.log"; then
  result=PASS
else
  result=FAIL
fi
checks=$(grep -c '^ok ' "$work/drill.log" || true)
refused=""
if [ "$negative" = 1 ]; then
  drill() { COSMIC_IMAGE_TAG="$tag" "$here/deploy/backup/restore-drill.sh" "$@" >"$work/neg.log" 2>&1; }
  mkdir "$work/tampered"
  cp "$work/$stem".* "$work/tampered/"
  size=$(stat -c %s "$work/tampered/$stem.dump.age")
  printf 'x' | dd of="$work/tampered/$stem.dump.age" bs=1 seek=$(( size / 2 )) conv=notrunc 2>/dev/null
  if drill "$work/tampered" "$stem" "$key"; then
    echo "NEGATIVE CHECK FAILED: a tampered dump passed the drill" >&2; result=FAIL
  else refused="tampered"; fi
  docker run --rm --network none --entrypoint age-keygen "cosmictrading-backup:$tag" 2>/dev/null >"$work/wrong.key"
  if drill "$work" "$stem" "$work/wrong.key"; then
    echo "NEGATIVE CHECK FAILED: the drill passed with a wrong key" >&2; result=FAIL
  else refused="$refused,wrong-key"; fi
  echo "negative checks refused: ${refused#,}"
fi
line=$(printf '{"stem":"%s","drilled_at":"%s","result":"%s","checks":%d,"where":"workstation","duration_s":%d,"image":"%s","refused":"%s"}' \
  "$stem" "$started" "$result" "$checks" "$(( $(date +%s) - t0 ))" "$tag" "${refused#,}")
printf '%s\n' "$line" | ssh_ "cat >> $remote/restore-drills.jsonl"
echo "$result: $stem ($checks checks passed) recorded in $host:$remote/restore-drills.jsonl"
[ "$result" = PASS ]
