#!/usr/bin/env bash
# The HFM bridge's direct link, on the server (S91; ADR-028 addendum of 2026-11-13; docs/12 §8).
#
# The trading computer, on the same home network as this VM, keeps one SSH connection to it and
# sends the bridge's requests through it to the relay on this VM's loopback
# (deploy/compose.bridge-link.yaml). This script gives that connection a sign-in of its own
# that can forward to the relay's port and do nothing else: no shell, no command, no terminal,
# no other forward; only from the trading computer's address and only with its key. Nothing
# listens on the home network that did not before (sshd).
#
# DRY RUN BY DEFAULT, like bootstrap.sh: prints every action and changes the system only with
# --apply (as root). Idempotent. deploy.sh runs it for production; a release needs no other step.
#
#   bridge-link.sh --check  --key <public key file> --from <IPv4>   are these two usable?
#   bridge-link.sh [--apply] --key <public key file> --from <IPv4>  set the link up, or update it
#   bridge-link.sh [--apply] --remove                               take it away
#   bridge-link.sh --verify                                         (root) is it as it must be?
#
# sshd checks the whole configuration before it is reloaded (never restarted: open sessions
# stay). What it applies to the admin's own sign-in afterwards must be exactly what it applied
# before, and what it applies to the link's sign-in must be the rules below; otherwise the
# change is taken back and nothing is reloaded.
set -euo pipefail
APPLY=0; MODE=setup; KEY=""; FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --check) MODE=check; shift ;;
    --remove) MODE=remove; shift ;;
    --verify) MODE=verify; shift ;;
    --key) KEY="${2:-}"; shift 2 ;;
    --from) FROM="${2:-}"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
NAME=cosmicbridge
KEYS=/etc/ssh/cosmic-bridge-link.keys
CONF=/etc/ssh/sshd_config.d/99-cosmic-bridge-link.conf
PORT="${COSMIC_BRIDGE_LINK_PORT:-8745}"
# A machine without systemd (the test sandbox) names its own way to make sshd read again.
RELOAD="${COSMIC_SSHD_RELOAD:-systemctl reload ssh}"
[[ "$PORT" =~ ^[0-9]{4,5}$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] ||
  { echo "COSMIC_BRIDGE_LINK_PORT must be a port from 1024 to 65535" >&2; exit 2; }

die() { echo "bridge-link: $*" >&2; exit 1; }
run() { printf '+ %s\n' "$*"; if [ "$APPLY" -eq 1 ]; then "$@"; fi; }
need_root() { [ "$(id -u)" -eq 0 ] || die "$1 must run as root"; }

# What the link's sign-in may do, as sshd itself reports it (sshd -T prints lower case).
RULES=(
  "authorizedkeysfile $KEYS"
  "authenticationmethods publickey"
  "passwordauthentication no"
  "kbdinteractiveauthentication no"
  "allowtcpforwarding local"
  "permitopen 127.0.0.1:$PORT"
  "permitlisten none"
  "allowagentforwarding no"
  "allowstreamlocalforwarding no"
  "x11forwarding no"
  "permittty no"
  "permittunnel no"
  "permituserrc no"
  "maxsessions 0"
  "forcecommand /usr/sbin/nologin"
)
conf_text() {
  cat <<EOF
# The HFM bridge's direct link (deploy/vm/bridge-link.sh writes this file; do not edit it).
# This sign-in can forward to the bridge relay on this machine's loopback and do nothing else.
Match User $NAME
    AuthorizedKeysFile $KEYS
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:$PORT
    PermitListen none
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    PermitUserRC no
    MaxSessions 0
    ForceCommand /usr/sbin/nologin
    ClientAliveInterval 15
    ClientAliveCountMax 2
EOF
}

the_key() { # the one public key in $KEY: its type and its body, nothing else from the file
  [ -r "$KEY" ] || die "cannot read the key file $KEY"
  [ "$(grep -c . "$KEY" || true)" -eq 1 ] || die "the key file must hold exactly one line"
  local type body _rest
  read -r type body _rest < "$KEY" || true
  [ "$type" = ssh-ed25519 ] || die "only an ssh-ed25519 public key is taken"
  [[ "$body" =~ ^AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{40,60}={0,2}$ ]] || die "that is not an ed25519 public key"
  printf '%s %s\n' "$type" "$body" | ssh-keygen -l -f /dev/stdin >/dev/null 2>&1 ||
    die "ssh-keygen does not read that as a public key"
  printf '%s %s' "$type" "$body"
}
the_address() {
  [[ "$FROM" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "--from must be the trading computer's IPv4 address"
  local part
  for part in ${FROM//./ }; do [ "$part" -le 255 ] || die "--from is not an IPv4 address"; done
  printf '%s' "$FROM"
}
applied_to() { # user [address] → what sshd applies to that sign-in, one setting a line
  sshd -T -C "user=$1,host=bridge-link.invalid,addr=${2:-192.0.2.1},laddr=192.0.2.2,lport=22" | sort
}
link_rules_hold() { # [address] → does sshd apply every rule to the link's sign-in?
  local applied rule ok=0
  applied="$(applied_to "$NAME" "${1:-}")" || return 1
  for rule in "${RULES[@]}"; do
    grep -qxF "$rule" <<<"$applied" || { echo "  not applied to $NAME: $rule" >&2; ok=1; }
  done
  return "$ok"
}

case "$MODE" in
check)
  the_key >/dev/null; the_address >/dev/null
  echo "bridge-link: the key and the address are usable"
  ;;

verify)
  need_root --verify
  bad=0
  no() { echo "  $*" >&2; bad=1; }
  id "$NAME" >/dev/null 2>&1 || no "the sign-in $NAME does not exist"
  [ "$(getent passwd "$NAME" | cut -d: -f7)" = /usr/sbin/nologin ] || no "$NAME has a shell"
  [ -f "$KEYS" ] && [ "$(stat -c '%U %a' "$KEYS")" = "root 644" ] || no "$KEYS is not root's, 0644"
  [ "$(grep -c . "$KEYS" 2>/dev/null || true)" -eq 1 ] || no "$KEYS must hold exactly one key"
  grep -qE "^restrict,port-forwarding,permitopen=\"127\.0\.0\.1:$PORT\",from=\"[0-9.]+\" ssh-ed25519 " "$KEYS" 2>/dev/null ||
    no "the key in $KEYS is not restricted to the relay and to one address"
  [ "$(cat "$CONF" 2>/dev/null)" = "$(conf_text)" ] || no "$CONF is not what this script writes"
  sshd -t 2>/dev/null || no "sshd rejects its configuration"
  link_rules_hold || bad=1
  [ "$bad" -eq 0 ] || die "the bridge link is not as it must be"
  echo "bridge-link: the sign-in $NAME can forward to 127.0.0.1:$PORT and do nothing else"
  ;;

remove)
  [ "$APPLY" -eq 0 ] || need_root --apply
  if [ ! -e "$CONF" ] && [ ! -e "$KEYS" ] && ! id "$NAME" >/dev/null 2>&1; then
    echo "= no bridge link on this machine"; exit 0
  fi
  [ ! -e "$CONF" ] || run rm -f "$CONF"
  run sshd -t
  # shellcheck disable=SC2086  # the reload command is a command line
  run $RELOAD
  if id "$NAME" >/dev/null 2>&1; then
    printf '+ end the open connections of %s\n' "$NAME"
    [ "$APPLY" -eq 0 ] || pkill -u "$NAME" || true
    run userdel "$NAME"
  fi
  [ ! -e "$KEYS" ] || run rm -f "$KEYS"
  echo "bridge-link: removed"
  ;;

setup)
  key="$(the_key)"; address="$(the_address)"
  [ "$APPLY" -eq 0 ] || need_root --apply
  admin="${SUDO_USER:-root}"
  keys_text="restrict,port-forwarding,permitopen=\"127.0.0.1:$PORT\",from=\"$address\" $key cosmic-bridge-link"
  if [ "$APPLY" -eq 1 ]; then
    # A list of the users who may sign in at all would silently shut the link's sign-in out.
    ! sshd -T 2>/dev/null | grep -qiE '^(allowusers|allowgroups) ' ||
      die "sshd has an AllowUsers/AllowGroups list; add $NAME to it first (nothing was changed)"
    before="$(applied_to "$admin")" || die "sshd -T failed before any change (nothing was changed)"
  fi

  if id "$NAME" >/dev/null 2>&1; then
    echo "= sign-in $NAME exists"
  else
    run useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$NAME"
    # No password at all ('*'), which is not a locked account: sshd refuses locked ones when
    # it runs without PAM.
    run usermod -p '*' "$NAME"
  fi

  if [ -e "$KEYS" ] && [ "$(cat "$KEYS")" = "$keys_text" ]; then
    echo "= $KEYS unchanged"
  else
    printf '+ write %s (0644 root:root): the one key, restricted to the relay and to %s\n' "$KEYS" "$address"
    if [ "$APPLY" -eq 1 ]; then
      printf '%s\n' "$keys_text" > "$KEYS.tmp" && chmod 0644 "$KEYS.tmp" && chown root:root "$KEYS.tmp" &&
        mv "$KEYS.tmp" "$KEYS"
    fi
  fi

  if [ -e "$CONF" ] && [ "$(cat "$CONF")" = "$(conf_text)" ]; then
    echo "= $CONF unchanged"
    [ "$APPLY" -eq 0 ] || link_rules_hold "$address" || die "sshd does not apply the link's rules; see above"
  else
    printf '+ write %s (0644 root:root), check it with sshd, then reload sshd\n' "$CONF"
    if [ "$APPLY" -eq 1 ]; then
      old=""; [ ! -e "$CONF" ] || { old="$CONF.old"; cp -a "$CONF" "$old"; }
      back() { # take the change back: sshd was not reloaded, so nothing ever applied it
        if [ -n "$old" ]; then mv "$old" "$CONF"; else rm -f "$CONF"; fi
        die "$1; the sshd configuration is as it was and was not reloaded"
      }
      conf_text > "$CONF.tmp" && chmod 0644 "$CONF.tmp" && chown root:root "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
      sshd -t || back "sshd rejects the new configuration"
      after="$(applied_to "$admin")" || back "sshd -T failed with the new configuration"
      [ "$before" = "$after" ] || back "the new configuration would change what sshd applies to $admin"
      link_rules_hold "$address" || back "sshd would not apply the link's rules"
      [ -z "$old" ] || rm -f "$old"
      # shellcheck disable=SC2086  # the reload command is a command line
      $RELOAD
      echo "  sshd reloaded"
    fi
  fi
  echo "bridge-link: the sign-in $NAME forwards to 127.0.0.1:$PORT for $address, and does nothing else"
  ;;
esac
