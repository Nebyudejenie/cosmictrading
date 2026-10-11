#!/usr/bin/env bash
# Deploy or update the stack on the VM (docs/12 §6). Run from the repository checkout.
#   deploy.sh [--dry-run] [--build] <tag>
# <tag> is the image tag (convention: git short SHA). --build builds both images from the
# checkout, which must be at exactly that commit. Never deletes volumes or data.
set -euo pipefail
DRY=0; BUILD=0; TAG=""
for a in "$@"; do
  case "$a" in --dry-run) DRY=1 ;; --build) BUILD=1 ;; -*) echo "unknown flag $a" >&2; exit 2 ;; *) TAG="$a" ;; esac
done
[[ "$TAG" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo "usage: $0 [--dry-run] [--build] <tag>" >&2; exit 2; }
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
R="${COSMIC_ROOT:-/srv/cosmic}"
compose_env="$R/config/compose.env"
cf="$src/deploy/compose.prod.yaml"

run() { printf '+ %s\n' "$*"; if [ "$DRY" -eq 0 ]; then "$@"; fi; }
fail() { echo "deploy: $*" >&2; exit 1; }

echo "# preflight"
command -v docker >/dev/null || fail "docker not installed"
for f in "$R/config/cosmic.env" "$compose_env" "$R/config/backup-recipients.txt"; do
  [ -s "$f" ] || fail "missing or empty: $f"
done
for s in db_superuser_password db_owner_password db_app_password secret_key metrics_token; do
  [ -s "$R/secrets/$s" ] || fail "required secret file missing or empty: $R/secrets/$s"
done
for s in groq_api_key gemini_api_key openrouter_api_key telegram_bot_token tunnel_token; do
  [ -e "$R/secrets/$s" ] || fail "optional secret file must exist (may be empty): $R/secrets/$s"
done
if grep -q '<portal-host>' "$R/config/cosmic.env"; then
  echo "  note: COSMIC_ALLOWED_HOSTS still has the placeholder hostname (fine until the tunnel exists)"
fi
set -a
# shellcheck disable=SC1090
. "$compose_env"
set +a
previous="${COSMIC_IMAGE_TAG:-}"
export COSMIC_SECRETS_DIR="$R/secrets" COSMIC_CONFIG_DIR="$R/config" COSMIC_BACKUP_DIR="$R/backups" \
       COSMIC_ENV_FILE="$R/config/cosmic.env"
compose() { docker compose -f "$cf" "$@"; }

if [ "$BUILD" -eq 1 ]; then
  echo "# build images at $TAG"
  head=$(git -C "$src" rev-parse --short HEAD)
  [ "$head" = "$TAG" ] || fail "checkout is at $head, not $TAG"
  [ -z "$(git -C "$src" status --porcelain)" ] || fail "checkout has uncommitted changes"
  run docker build -f "$src/deploy/Dockerfile" -t "cosmictrading:$TAG" "$src"
  run docker build -t "cosmictrading-backup:$TAG" "$src/deploy/backup"
fi
if [ "$DRY" -eq 0 ]; then
  docker image inspect "cosmictrading:$TAG" "cosmictrading-backup:$TAG" >/dev/null ||
    fail "images for $TAG not found (use --build or docker load)"
fi

if [ -n "$previous" ] && docker ps --format '{{.Names}}' | grep -q '^cosmictrading-postgres-1$'; then
  echo "# pre-deploy encrypted backup with the running release ($previous)"
  COSMIC_IMAGE_TAG="$previous" run "$src/deploy/backup/backup.sh"
fi

echo "# switch to $TAG (previous: ${previous:-none})"
run cp -a "$compose_env" "$compose_env.bak-$(date -u +%Y%m%dT%H%M%SZ)"
run sed -i "s/^COSMIC_IMAGE_TAG=.*/COSMIC_IMAGE_TAG=$TAG/" "$compose_env"
export COSMIC_IMAGE_TAG="$TAG"
run compose up -d --wait --wait-timeout 300 --remove-orphans

echo "# smoke tests"
ok=1
run compose exec -T web cosmic probe web || ok=0
run compose exec -T worker cosmic probe worker --max-age 90 || ok=0
run compose exec -T worker cosmic audit verify || ok=0
run compose exec -T worker cosmic golive status || ok=0
if [ "$DRY" -eq 1 ]; then
  echo "deploy: DRY RUN — nothing was changed, nothing was verified"
elif [ "$ok" -eq 1 ]; then
  run sh -c "echo '$(date -u +%FT%TZ) deployed $TAG (previous ${previous:-none})' >> '$R/config/deploy-history.log'"
  echo "deploy: $TAG is running and healthy"
else
  cat >&2 <<ROLLBACK
deploy: SMOKE TESTS FAILED for $TAG.
Rollback (data is untouched):
  sed -i 's/^COSMIC_IMAGE_TAG=.*/COSMIC_IMAGE_TAG=${previous:-<previous-tag>}/' $compose_env
  COSMIC_IMAGE_TAG=${previous:-<previous-tag>} docker compose -f $cf up -d --wait
Migrations only move forward. If $TAG added a migration that the previous release cannot
run against, restore the pre-deploy backup into a new database (deploy/backup/restore.sh)
and point COSMIC_DB_NAME at it (docs/15 "Restore from backup").
ROLLBACK
  exit 1
fi
