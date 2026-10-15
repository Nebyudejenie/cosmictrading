#!/usr/bin/env bash
# Monthly restore drill (AT-19, runbook §5), run from the owner's workstation, where the private
# key lives. It copies the newest encrypted backup set from the staging server, runs
# deploy/backup/restore-drill.sh on it here (a throwaway container without network), appends the
# result to restore-drills.jsonl next to the backups on the server (the worker reads it), and
# deletes the local copy. Nothing on the server is changed except that one appended line.
#
# Usage: scripts/drill-staging.sh [age-identity-file]
#   default identity: ~/.secrets/cosmictrading-backup-age.key
# Environment: COSMIC_SSH (default cosmictrading), COSMIC_REMOTE_BACKUPS (default
#   apps/cosmictrading/backups, relative to the remote home), COSMIC_IMAGE_TAG (the backup image
#   tag to drill with; default: the tag the server's worker runs, which deploy-staging.sh built here)
set -euo pipefail
host="${COSMIC_SSH:-cosmictrading}"
remote="${COSMIC_REMOTE_BACKUPS:-apps/cosmictrading/backups}"
key="${1:-$HOME/.secrets/cosmictrading-backup-age.key}"
here="$(cd "$(dirname "$0")/.." && pwd)"
[ -r "$key" ] || { echo "cannot read the age identity $key" >&2; exit 2; }

ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=25 "$host" "$@"; }

stem="$(ssh_ "ls -1 $remote" | sed -n 's/^\(cosmic-[0-9]\{8\}T[0-9]\{6\}Z\)\.manifest\.json$/\1/p' | sort | tail -1)"
[ -n "$stem" ] || { echo "no backup set found on $host:$remote" >&2; exit 1; }
tag="${COSMIC_IMAGE_TAG:-$(ssh_ "docker inspect --format '{{.Config.Image}}' cosmictrading-worker-1" | sed 's/.*://')}"
docker image inspect "cosmictrading-backup:$tag" >/dev/null 2>&1 \
  || { echo "backup image cosmictrading-backup:$tag is not on this machine" >&2; exit 2; }

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
line=$(printf '{"stem":"%s","drilled_at":"%s","result":"%s","checks":%d,"where":"workstation","duration_s":%d,"image":"%s"}' \
  "$stem" "$started" "$result" "$checks" "$(( $(date +%s) - t0 ))" "$tag")
printf '%s\n' "$line" | ssh_ "cat >> $remote/restore-drills.jsonl"
echo "$result: $stem ($checks checks passed) recorded in $host:$remote/restore-drills.jsonl"
[ "$result" = PASS ]
