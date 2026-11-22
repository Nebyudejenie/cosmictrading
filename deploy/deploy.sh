#!/usr/bin/env bash
# Deploy or update the stack on the VM (docs/12 §6). Run from the repository checkout.
#   deploy.sh [--dry-run] [--build] [--untested] <tag>
# <tag> is the image tag (convention: git short SHA). --build builds both images from the
# checkout, which must be at exactly that commit. Never deletes volumes or data.
# Production (/srv/cosmic) takes only a tag that has run on staging or in production before
# (CF-10, deploy/tested-on-staging.sh); --untested overrides that, and the history records it.
set -euo pipefail
DRY=0; BUILD=0; UNTESTED=0; TAG=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;; --build) BUILD=1 ;; --untested) UNTESTED=1 ;;
    -*) echo "unknown flag $a" >&2; exit 2 ;; *) TAG="$a" ;;
  esac
done
[[ "$TAG" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo "usage: $0 [--dry-run] [--build] [--untested] <tag>" >&2; exit 2; }
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The stack's root is the directory holding this checkout (/srv/cosmic/src → /srv/cosmic), so a
# staging checkout can never deploy into production's root by a forgotten variable.
R="${COSMIC_ROOT:-$(dirname "$src")}"
compose_env="$R/config/compose.env"
cf="$src/deploy/compose.prod.yaml"

run() { printf '+ %s\n' "$*"; if [ "$DRY" -eq 0 ]; then "$@"; fi; }
fail() { echo "deploy: $*" >&2; exit 1; }
[ "$(cd "$R/src" 2>/dev/null && pwd)" = "$src" ] || fail "this checkout ($src) is not $R/src"

echo "# preflight"
command -v docker >/dev/null || fail "docker not installed"
for f in "$R/config/cosmic.env" "$compose_env" "$R/config/backup-recipients.txt"; do
  [ -s "$f" ] || fail "missing or empty: $f"
done
for s in db_superuser_password db_owner_password db_app_password secret_key metrics_token; do
  [ -s "$R/secrets/$s" ] || fail "required secret file missing or empty: $R/secrets/$s"
done
# Optional secrets added after a root was set up are created empty once, as bootstrap.sh does
# (never printed, never overwritten): the second and third bridge tokens (S74, S83).
for s in hfm_bridge2_token hfm_bridge3_token; do
  [ -e "$R/secrets/$s" ] || { install -m 0440 -o root -g 10001 /dev/null "$R/secrets/$s" &&
    echo "  created empty optional secret $s"; }
done
# Terminal 2's bridge token (S110, the owner's yes of 2026-11-20 to a second MT5 for Robot A's own
# demo): made once on production, the bridges' server, while it is empty; never printed and never
# replaced. The bridge computer gets its copy over SSH, as the first bridge's was.
if [ "$R" = /srv/cosmic ] && [ ! -s "$R/secrets/hfm_bridge2_token" ]; then
  if [ "$DRY" -eq 0 ]; then
    new_token="$R/secrets/hfm_bridge2_token.new"
    ( umask 077 && od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$new_token" )
    [ "$(wc -c < "$new_token")" -eq 64 ] || { rm -f "$new_token"; fail "could not make a token"; }
    install -m 0440 -o root -g 10001 "$new_token" "$R/secrets/hfm_bridge2_token"
    rm -f "$new_token"
  fi
  echo "  made the second bridge's token (not shown)"
fi
# Terminal 3's bridge token (S113, the owner's Manual Trading of 2026-11-22: their real account
# read through the read-only terminal): made once on production while it is empty, as the second
# one was; never printed, never replaced. The read-only bridge refuses every trading command.
if [ "$R" = /srv/cosmic ] && [ ! -s "$R/secrets/hfm_bridge3_token" ]; then
  if [ "$DRY" -eq 0 ]; then
    new_token="$R/secrets/hfm_bridge3_token.new"
    ( umask 077 && od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$new_token" )
    [ "$(wc -c < "$new_token")" -eq 64 ] || { rm -f "$new_token"; fail "could not make a token"; }
    install -m 0440 -o root -g 10001 "$new_token" "$R/secrets/hfm_bridge3_token"
    rm -f "$new_token"
  fi
  echo "  made the third bridge's token (not shown)"
fi
for s in groq_api_key gemini_api_key openrouter_api_key telegram_bot_token tunnel_token \
         binance_testnet_api_key binance_testnet_private_key hfm_bridge_token hfm_bridge2_token \
         hfm_bridge3_token; do
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
overlay="${COSMIC_COMPOSE_OVERLAY:-}"  # e.g. deploy/compose.staging.yaml (relative to repo)
[ -z "$overlay" ] || [ -f "$src/$overlay" ] || fail "overlay not found: $overlay"
# The HFM bridge's direct link (S91, deploy/vm/bridge-link.sh): production only. The admin hands
# in the trading computer's public key and its address on the home network as two files in the
# checkout's var/ (not in git), or asks for the link's removal with var/bridge-link.remove.
# They are checked here, before anything is changed, and kept in the root's config from then on.
link_key="$R/config/bridge-link.pub"; link_from="$R/config/bridge-link.from"
link_tool="$src/deploy/vm/bridge-link.sh"
link=0; link_removed=0
if [ "$R" = /srv/cosmic ]; then
  handed_key="$src/var/bridge-link.pub"; handed_from="$src/var/bridge-link.from"
  if [ -e "$src/var/bridge-link.remove" ]; then
    echo "# bridge link: removal asked for"
    link_removed=1  # also in a dry run, where the files below are still there
    run rm -f "$link_key" "$link_from" "$handed_key" "$handed_from" "$src/var/bridge-link.remove"
  elif [ -e "$handed_key" ] || [ -e "$handed_from" ]; then
    echo "# bridge link: a key and an address were handed in"
    [ -s "$handed_key" ] && [ -s "$handed_from" ] ||
      fail "the bridge link needs both var/bridge-link.pub and var/bridge-link.from"
    "$link_tool" --check --key "$handed_key" --from "$(cat "$handed_from")" ||
      fail "the handed-in bridge link key or address is not usable (nothing was changed)"
    run install -m 0644 -o root -g root "$handed_key" "$link_key"
    run install -m 0644 -o root -g root "$handed_from" "$link_from"
    run rm -f "$handed_key" "$handed_from"
  fi
  if [ "$link_removed" -eq 0 ] && [ -s "$link_key" ] && [ -s "$link_from" ]; then link=1; fi
fi
link_overlay=""; [ "$link" -eq 0 ] || link_overlay="$src/deploy/compose.bridge-link.yaml"
compose() {
  docker compose -f "$cf" ${overlay:+-f "$src/$overlay"} ${link_overlay:+-f "$link_overlay"} "$@"
}
if [ "$R" != /srv/cosmic ] && [ -z "$overlay" ] && [ -z "${COSMIC_SHARED_HOST:-}" ]; then
  fail "$R is not production (/srv/cosmic) but compose.env names no overlay"
fi
if [ "$overlay" = deploy/compose.staging.yaml ] && [[ "${COMPOSE_PROFILES:-}" =~ tunnel|telegram ]]; then
  fail "staging never runs the tunnel or the Telegram bot (COMPOSE_PROFILES=$COMPOSE_PROFILES)"
fi
project="$(compose config --format json 2>/dev/null | jq -r .name 2>/dev/null || true)"
echo "  project: $project, root: $R"
untested=""
if [ "$R" = /srv/cosmic ]; then  # whatever compose.env says about overlays
  echo "# staging gate (CF-10)"
  if ! "$src/deploy/tested-on-staging.sh" "$TAG" "$R" /srv/cosmic-staging; then
    [ "$UNTESTED" -eq 1 ] ||
      fail "production takes only a build that has run on staging (scripts/release.sh), or --untested"
    echo "  WARNING: --untested: $TAG goes to production without having run on staging" >&2
    untested=" UNTESTED"
  fi
fi

if [ "$BUILD" -eq 1 ]; then
  echo "# build images at $TAG"
  # The tag must name HEAD; any unambiguous abbreviation length is accepted.
  head=$(git -C "$src" rev-parse --short=7 HEAD)
  [ "$(git -C "$src" rev-parse -q --verify "$TAG^{commit}" 2>/dev/null)" = "$(git -C "$src" rev-parse HEAD)" ] ||
    fail "checkout is at $head, not $TAG"
  [ -z "$(git -C "$src" status --porcelain)" ] || fail "checkout has uncommitted changes"
  run docker build -f "$src/deploy/Dockerfile" -t "cosmictrading:$TAG" "$src"
  run docker build -t "cosmictrading-backup:$TAG" "$src/deploy/backup"
fi
if [ "$DRY" -eq 0 ]; then
  docker image inspect "cosmictrading:$TAG" "cosmictrading-backup:$TAG" >/dev/null ||
    fail "images for $TAG not found (use --build or docker load)"
fi

if [ -n "$previous" ] && [ -n "$(compose ps -q postgres 2>/dev/null)" ]; then
  echo "# pre-deploy encrypted backup with the running release ($previous)"
  COSMIC_IMAGE_TAG="$previous" COSMIC_COMPOSE_OVERLAY="$overlay" run "$src/deploy/backup/backup.sh"
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
if [ "$R" = /srv/cosmic ]; then
  # The link's sign-in on this machine, after the release itself is up: set up while the link
  # is on, taken away when it is not. A failure here never fails the release (the bridge
  # carries on over the internet path); verify.sh then reports the link as not in order.
  echo "# bridge link"
  apply=(); [ "$DRY" -eq 1 ] || apply=(--apply)
  if [ "$link" -eq 1 ]; then
    "$link_tool" "${apply[@]}" --key "$link_key" --from "$(cat "$link_from")" ||
      echo "  WARNING: the bridge link's sign-in was not set up; the release goes on without it" >&2
  else
    "$link_tool" "${apply[@]}" --remove ||
      echo "  WARNING: the bridge link's sign-in was not removed; see above" >&2
  fi
fi
if [ "$DRY" -eq 1 ]; then
  echo "deploy: DRY RUN — nothing was changed, nothing was verified"
elif [ "$ok" -eq 1 ]; then
  # An enabled timer starts only at the next boot: start this root's daily backup now, so a new
  # VM or a new staging root is not left without backups until a reboot.
  timer=cosmic-backup.timer
  [ "$R" = /srv/cosmic ] || timer=cosmic-staging-backup.timer
  if command -v systemctl >/dev/null && systemctl is-enabled --quiet "$timer" 2>/dev/null &&
    ! systemctl is-active --quiet "$timer"; then
    run systemctl start "$timer"
  fi
  run sh -c "echo '$(date -u +%FT%TZ) deployed $TAG (previous ${previous:-none})$untested' >> '$R/config/deploy-history.log'"
  echo "deploy: $TAG is running and healthy"
else
  cat >&2 <<ROLLBACK
deploy: SMOKE TESTS FAILED for $TAG.
Rollback (data is untouched):
  sed -i 's/^COSMIC_IMAGE_TAG=.*/COSMIC_IMAGE_TAG=${previous:-<previous-tag>}/' $compose_env
  set -a; . $compose_env; set +a
  docker compose -f $cf ${overlay:+-f $src/$overlay} up -d --wait
Migrations only move forward. If $TAG added a migration that the previous release cannot
run against, restore the pre-deploy backup into a new database (deploy/backup/restore.sh)
and point COSMIC_DB_NAME at it (docs/15 "Restore from backup").
ROLLBACK
  exit 1
fi
