#!/usr/bin/env bash
# Prepare the dedicated Ubuntu guest, 24.04 or later (docs/12 §3). Idempotent.
# DRY RUN BY DEFAULT: prints every action; changes the system only with --apply (as root).
# Never overwrites an existing secret or config file; existing files that would change are
# backed up first (<file>.bak-<UTC>).
#
# Usage (inside the VM, from the repository checkout at /srv/cosmic/src):
#   sudo deploy/vm/bootstrap.sh --mgmt-cidr 192.168.1.0/24 [--apply]
set -euo pipefail
APPLY=0; MGMT_CIDR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --mgmt-cidr) MGMT_CIDR="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[[ "$MGMT_CIDR" =~ ^[0-9.]+/[0-9]{1,2}$ ]] || { echo "--mgmt-cidr <LAN management subnet> is required (SSH stays reachable only from it)" >&2; exit 2; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
R=/srv/cosmic
stamp=$(date -u +%Y%m%dT%H%M%SZ)

run() { printf '+ %s\n' "$*"; if [ "$APPLY" -eq 1 ]; then "$@"; fi; }
install_file() { # dest mode owner content — back up a differing existing file first
  local dest="$1" mode="$2" owner="$3" content="$4"
  if [ -e "$dest" ] && [ "$(cat "$dest")" = "$content" ]; then echo "= $dest unchanged"; return; fi
  [ -e "$dest" ] && run cp -a "$dest" "$dest.bak-$stamp"
  printf '+ write %s (%s %s)\n' "$dest" "$mode" "$owner"
  if [ "$APPLY" -eq 1 ]; then
    printf '%s\n' "$content" > "$dest.tmp-$stamp" && chmod "$mode" "$dest.tmp-$stamp" &&
      chown "$owner" "$dest.tmp-$stamp" && mv "$dest.tmp-$stamp" "$dest"
  fi
}
new_secret() { # name generator — create once, never print, never overwrite
  local f="$R/secrets/$1"
  if [ -e "$f" ]; then echo "= secret $1 exists (kept)"; return; fi
  printf '+ generate secret %s\n' "$1"
  if [ "$APPLY" -eq 1 ]; then (umask 077; $2 > "$f"); chown root:10001 "$f"; chmod 0440 "$f"; fi
}

if [ "$APPLY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then echo "--apply must run as root" >&2; exit 1; fi
# shellcheck source=/dev/null
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID%%.*}" -ge 24 ] 2>/dev/null ||
  echo "WARNING: written for Ubuntu 24.04 or later, found ${PRETTY_NAME:-unknown}"

echo "# 1. packages (Ubuntu archive; no third-party repositories)"
run apt-get update -q
run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io docker-compose-v2 \
  docker-buildx ufw unattended-upgrades qemu-guest-agent git jq curl ca-certificates rsync zstd
run systemctl enable --now docker unattended-upgrades
# The agent only starts when Proxmox gives the VM its port (VM → Options → QEMU Guest Agent).
if [ -e /dev/virtio-ports/org.qemu.guest_agent.0 ]; then
  run systemctl enable --now qemu-guest-agent
else
  echo "  note: no QEMU guest agent port; enable the agent in Proxmox, then reboot the VM"
fi

echo "# 2. Docker daemon: log rotation, live restore, no-new-privileges by default"
install_file /etc/docker/daemon.json 0644 root:root '{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": true,
  "no-new-privileges": true,
  "userland-proxy": false
}'
run systemctl restart docker

echo "# 3. OS hardening"
# 00- sorts before the installer's 50-cloud-init.conf: sshd keeps the FIRST value it reads.
# Forwarding is allowed only to the two loopback relays (break-glass 8743, staging 8744).
install_file /etc/ssh/sshd_config.d/00-cosmic.conf 0644 root:root 'PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
AllowTcpForwarding local
PermitOpen 127.0.0.1:8743 127.0.0.1:8744
MaxAuthTries 3'
[ ! -e /etc/ssh/sshd_config.d/60-cosmic.conf ] || run rm -f /etc/ssh/sshd_config.d/60-cosmic.conf
run sshd -t
run systemctl reload ssh
if [ "$APPLY" -eq 1 ]; then
  sshd -T | grep -Ei '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|allowtcpforwarding|permitopen) '
fi
run mkdir -p /etc/systemd/journald.conf.d
install_file /etc/systemd/journald.conf.d/60-cosmic.conf 0644 root:root '[Journal]
SystemMaxUse=1G'
run systemctl restart systemd-journald
install_file /etc/apt/apt.conf.d/20auto-upgrades 0644 root:root 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";'

echo "# 4. firewall: deny inbound except SSH from $MGMT_CIDR (Docker publishes no ports)"
run ufw default deny incoming
run ufw default allow outgoing
run ufw allow from "$MGMT_CIDR" to any port 22 proto tcp comment "ssh from management LAN"
# The SSH jump container (deploy/cloudflared/compose.ssh-access.yaml) reaches sshd only from its
# fixed network; nothing else on Docker's networks may.
run ufw allow in on br-cosmic-jump from 10.203.65.0/29 to any port 22 proto tcp \
  comment "ssh from the cosmictrading jump container"
run ufw --force enable

echo "# 5. directories and secrets (group 10001 = the containers' user)"
if ! getent group 10001 >/dev/null; then  # the login user's own group may already be "cosmic"
  if getent group cosmic >/dev/null; then group=cosmicapp; else group=cosmic; fi
  run groupadd --system --gid 10001 "$group"
fi
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then  # the admin runs deploys
  run usermod -aG docker "$SUDO_USER"
  [ "$APPLY" -eq 0 ] || usermod -aG "$(getent group 10001 | cut -d: -f1)" "$SUDO_USER"
fi
run install -d -m 0755 -o root -g root "$R"
run install -d -m 0750 -o root -g 10001 "$R/config" "$R/secrets"
# 0750: the worker's backup check and the admin's restore drill read it (the sets are encrypted).
run install -d -m 0750 -o 10001 -g 10001 "$R/backups"
# The workstation drill (scripts/drill-backups.sh) appends its result here as the admin.
[ -e "$R/backups/restore-drills.jsonl" ] ||
  run install -m 0660 -o 10001 -g 10001 /dev/null "$R/backups/restore-drills.jsonl"
pw() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n'; }
key() { head -c 32 /dev/urandom | base64 | tr -d '\n'; }
new_secret db_superuser_password pw
new_secret db_owner_password pw
new_secret db_app_password pw
new_secret secret_key key
new_secret metrics_token pw
for optional in groq_api_key gemini_api_key openrouter_api_key telegram_bot_token tunnel_token \
                binance_testnet_api_key binance_testnet_private_key hfm_bridge_token; do
  new_secret "$optional" true   # empty file = NOT_CONFIGURED until the owner fills it
done
[ -e "$R/config/cosmic.env" ] || run install -m 0640 -o root -g 10001 "$here/../env/cosmic.env.example" "$R/config/cosmic.env"
# root:10001: the backup container (UID 10001) archives the whole config directory.
[ -e "$R/config/compose.env" ] || run install -m 0640 -o root -g 10001 "$here/../env/compose.env.example" "$R/config/compose.env"

echo "# 6. systemd units (enabled, not started; deploy.sh starts the backup timer after a deploy)"
for unit in cosmictrading.service cosmic-backup.service cosmic-backup.timer; do
  install_file "/etc/systemd/system/$unit" 0644 root:root "$(cat "$here/systemd/$unit")"
done
run systemctl daemon-reload
run systemctl enable cosmictrading.service cosmic-backup.timer

cat <<NEXT

Next steps (docs/12, docs/15):
  1. Edit $R/config/cosmic.env (portal hostname in COSMIC_ALLOWED_HOSTS) and compose.env.
  2. Put the owner's age PUBLIC key in $R/config/backup-recipients.txt (private key stays off the VM).
  3. deploy/deploy.sh --build <git-short-sha>       (builds images, migrates, starts, smoke-tests)
  4. Create the owner: docker compose -f deploy/compose.prod.yaml run --rm --no-deps --entrypoint cosmic migrate admin create-owner
  5. Tunnel: only after the domain/DNS inventory (B-03): fill $R/secrets/tunnel_token, COMPOSE_PROFILES=tunnel.
NEXT
[ "$APPLY" -eq 1 ] || echo "# DRY RUN — nothing was changed. Re-run with --apply as root."
