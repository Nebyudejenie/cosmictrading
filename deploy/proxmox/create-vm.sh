#!/usr/bin/env bash
# Create the dedicated COSMICTRADING VM on a Proxmox VE host (docs/12 §2).
# DRY RUN BY DEFAULT: prints every command. Executes only with --apply, on the Proxmox host,
# as root. Never changes host networking, storage definitions, firewall or other VMs, and
# refuses to run if the VMID is already in use.
#
# Usage (on the Proxmox host):
#   create-vm.sh --vmid 210 --storage local-lvm --bridge vmbr0 --ssh-key ~/owner.pub \
#                [--ip 192.168.1.50/24 --gw 192.168.1.1] [--snippets local] [--start] [--apply]
set -euo pipefail

VMID=""; STORAGE=""; BRIDGE=""; SSH_KEY=""; IP="dhcp"; GW=""; SNIPPETS=""; START=0; APPLY=0
NAME=cosmictrading; MEMORY=10240; CORES=4; DISK=200G; CIUSER=cosmicadmin
IMAGE_URL=https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
SUMS_URL=https://cloud-images.ubuntu.com/noble/current/SHA256SUMS
IMAGE_DIR=/var/lib/vz/template/cosmictrading
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --vmid) VMID="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --gw) GW="$2"; shift 2 ;;
    --snippets) SNIPPETS="$2"; shift 2 ;;
    --start) START=1; shift ;;
    --apply) APPLY=1; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[[ "$VMID" =~ ^[0-9]{3,9}$ ]] || { echo "--vmid <number> is required" >&2; exit 2; }
if [ -z "$STORAGE" ] || [ -z "$BRIDGE" ]; then echo "--storage and --bridge are required" >&2; exit 2; fi
[ -r "$SSH_KEY" ] || { echo "--ssh-key <public key file> is required" >&2; exit 2; }
grep -q '^ssh-\(ed25519\|rsa\) ' "$SSH_KEY" || { echo "$SSH_KEY is not an SSH public key" >&2; exit 2; }

run() {
  printf '+ %s\n' "$*"
  if [ "$APPLY" -eq 1 ]; then "$@"; fi
}

# ---- read-only prechecks (also in dry run when on a Proxmox host) ------------------------
if command -v qm >/dev/null 2>&1; then
  if qm status "$VMID" >/dev/null 2>&1; then echo "VMID $VMID already exists — refusing." >&2; exit 1; fi
  pvesm status --storage "$STORAGE" >/dev/null || { echo "storage $STORAGE not found" >&2; exit 1; }
  ip link show "$BRIDGE" >/dev/null || { echo "bridge $BRIDGE not found" >&2; exit 1; }
  pveversion
elif [ "$APPLY" -eq 1 ]; then
  echo "qm not found: --apply must run on the Proxmox host" >&2; exit 1
else
  echo "# (not a Proxmox host: prechecks skipped, printing the plan only)"
fi

ipconfig="ip=dhcp"
if [ "$IP" != "dhcp" ]; then
  [ -n "$GW" ] || { echo "--gw is required with a static --ip" >&2; exit 2; }
  ipconfig="ip=$IP,gw=$GW"
fi
image="$IMAGE_DIR/$(basename "$IMAGE_URL")"

# ---- image: download and verify the checksum published by Ubuntu --------------------------
run mkdir -p "$IMAGE_DIR"
run wget -q -O "$image.download" "$IMAGE_URL"
run wget -q -O "$IMAGE_DIR/SHA256SUMS" "$SUMS_URL"
if [ "$APPLY" -eq 1 ]; then
  expected=$(awk -v f="*$(basename "$IMAGE_URL")" '$2 == f {print $1}' "$IMAGE_DIR/SHA256SUMS")
  actual=$(sha256sum "$image.download" | cut -d' ' -f1)
  if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then echo "image checksum mismatch" >&2; exit 1; fi
  mv "$image.download" "$image"
else
  echo "+ verify sha256 of the image against SHA256SUMS, then rename to $image"
fi

# ---- VM definition ------------------------------------------------------------------------
run qm create "$VMID" --name "$NAME" --ostype l26 \
  --memory "$MEMORY" --balloon 0 --cores "$CORES" --cpu host --numa 0 \
  --net0 "virtio,bridge=$BRIDGE,firewall=0" \
  --scsihw virtio-scsi-single --agent enabled=1,fstrim_cloned_disks=1 \
  --onboot 1 --startup order=10,up=60 \
  --serial0 socket --vga serial0 \
  --description "COSMICTRADING QUANT (dedicated). Application never runs on the hypervisor."
run qm set "$VMID" --scsi0 "$STORAGE:0,import-from=$image,discard=on,ssd=1,iothread=1"
run qm resize "$VMID" scsi0 "$DISK"
run qm set "$VMID" --ide2 "$STORAGE:cloudinit" --boot order=scsi0
run qm set "$VMID" --ciuser "$CIUSER" --sshkeys "$SSH_KEY" --ipconfig0 "$ipconfig"
if [ -n "$SNIPPETS" ]; then
  run cp "$here/../vm/cloud-init.yaml" "/var/lib/vz/snippets/cosmictrading-user.yaml"
  run qm set "$VMID" --cicustom "user=$SNIPPETS:snippets/cosmictrading-user.yaml"
  echo "# NOTE: with --cicustom, put the owner's SSH key into cloud-init.yaml first."
fi
[ "$START" -eq 1 ] && run qm start "$VMID"

if [ "$APPLY" -eq 0 ]; then
  echo "# DRY RUN — nothing was changed. Re-run with --apply on the Proxmox host."
else
  echo "VM $VMID created. Next: ssh $CIUSER@<vm-ip>, clone the repo to /srv/cosmic/src, run deploy/vm/bootstrap.sh."
fi
