#!/usr/bin/env bash
# CF-10: production takes only a build that has run on staging. Exit 0 when <tag> passed
# staging's deploy smoke tests (deploy.sh records it only then), or ran in production before
# (a rollback); exit 1 otherwise. Read-only.
#   deploy/tested-on-staging.sh <tag> <production-root> <staging-root>
set -euo pipefail
[ $# -eq 3 ] || { echo "usage: $0 <tag> <production-root> <staging-root>" >&2; exit 2; }
tag="$1" production="$2" staging="$3"
[[ "$tag" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo "invalid tag: $tag" >&2; exit 2; }
# deploy.sh writes "<UTC> deployed <tag> (previous <tag>)"; a fixed-string match, so a shorter
# tag or one with a dot never matches another.
ran() { [ -r "$1/config/deploy-history.log" ] && grep -Fq " deployed $tag (" "$1/config/deploy-history.log"; }
if ran "$staging"; then echo "  $tag passed staging's smoke tests ($staging)"; exit 0; fi
if ran "$production"; then echo "  $tag ran in production before (rollback)"; exit 0; fi
echo "  $tag has not run on staging ($staging/config/deploy-history.log)" >&2
exit 1
