#!/bin/sh
# Host key persisted in a volume (stable identity); authorised keys copied from a read-only
# mount into a root-owned file so sshd's StrictModes accepts it.
set -eu
[ -f /etc/ssh/keys/ssh_host_ed25519_key ] ||
  ssh-keygen -q -t ed25519 -N "" -C "cosmictrading-jump" -f /etc/ssh/keys/ssh_host_ed25519_key
[ -s /etc/bastion/authorized_keys ] || { echo "no authorised keys mounted" >&2; exit 2; }
install -o root -g root -m 0644 /etc/bastion/authorized_keys /etc/ssh/authorized/jump
exec /usr/sbin/sshd -D -e
