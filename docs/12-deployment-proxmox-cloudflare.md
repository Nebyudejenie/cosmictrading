# 12 — Deployment: Proxmox VM and Cloudflare

**Status:** artefacts prepared and verified locally; VM deployment BLOCKED (B-02); tunnel and DNS BLOCKED (B-03). Nothing in this document has been executed against a Proxmox host or a Cloudflare zone.

## 1. Target

| Item | Plan |
|---|---|
| Hypervisor | Proxmox VE (version to be recorded on access). The application never runs on the hypervisor host. |
| Guest | One dedicated VM `cosmictrading`, Ubuntu Server 24.04 LTS cloud image, 4 vCPU (`host` CPU type), 10 GB RAM (no ballooning), 200 GB disk on SSD-backed storage (`discard=on`, `ssd=1`), virtio NIC on the LAN bridge, QEMU guest agent, `onboot=1`, startup order after storage/network services. |
| Disks | Single 200 GB virtual disk with separate LVM logical volumes is optional; minimum is separate directories with quotas: `/srv/cosmic/pgdata` (Docker volume bind), `/srv/cosmic/backups`, `/srv/cosmic/secrets` (0700), `/var/lib/docker`. Recommended: a second virtual disk for backups so a full data disk cannot destroy backups. |
| Network | Static DHCP reservation or static IP (owner supplies). Outbound: 443 (Cloudflare, data sources, AI, Telegram, OS updates), 7844 TCP/UDP (cloudflared). Inbound: SSH from the LAN management subnet only; no application ports. |
| OS hardening | non-root `cosmic` user in `docker` group; SSH keys only, no root login; `ufw` default deny inbound; `unattended-upgrades` (security); time sync (systemd-timesyncd); journald size cap. |

## 2. Provisioning (`deploy/proxmox/create-vm.sh`)

Prints, and only with `--apply` runs, the `qm` commands on the Proxmox host: download the Ubuntu cloud image (checksum verified), `qm create <VMID> --name cosmictrading --memory 10240 --balloon 0 --cores 4 --cpu host --net0 virtio,bridge=<BRIDGE> --scsihw virtio-scsi-single --agent enabled=1 --onboot 1`, `qm importdisk`, `qm set --scsi0 <STORAGE>:vm-<VMID>-disk-0,discard=on,ssd=1`, `qm resize scsi0 200G`, cloud-init drive with `deploy/vm/cloud-init.yaml` (user, SSH key, packages). It refuses to run if the VMID already exists. It never changes host networking, storage definitions or firewall.

## 3. Guest setup (`deploy/vm/bootstrap.sh`, idempotent)

Installs Docker Engine + Compose plugin from Ubuntu packages, creates `/srv/cosmic/{secrets,backups}`, installs `cosmictrading.service` (runs `docker compose -f compose.prod.yaml up -d` after `docker.service`, `network-online.target`), `cosmic-backup.service` + `.timer` (daily 02:30 UTC), configures `ufw`, sets Docker log rotation. `--dry-run` prints every action.

## 4. Application stack (`deploy/compose.prod.yaml`)

| Service | Image | Limits (of 10 GB) | Notes |
|---|---|---|---|
| postgres | `postgres:17-alpine` @digest | 2.0 GB mem, 1.5 CPU | `shared_buffers=512MB`, `effective_cache_size=1536MB`, named volume, healthcheck `pg_isready` |
| migrate | cosmic image | — | one-shot; `alembic upgrade head` as `cosmic_owner` |
| web | cosmic image | 512 MB, 1 CPU | read-only root fs, tmpfs `/tmp`, non-root UID 10001, healthcheck `/readyz`, no published port |
| worker | cosmic image | 1 GB, 1.5 CPU | read-only root fs; heartbeat |
| bot | cosmic image | 256 MB | profile `telegram` |
| cloudflared | `cloudflare/cloudflared` @digest | 128 MB | profile `tunnel`; `tunnel --no-autoupdate run --token-file /run/secrets/tunnel_token` |

Budget: services ≈ 4 GB; OS + Docker ≈ 1.5 GB; page cache and headroom ≈ 4.5 GB. Disk: database ≤ 60 GB, backups ≤ 60 GB (retention-pruned), logs ≤ 5 GB, Docker images ≤ 10 GB, free reserve ≥ 50 GB. The worker pauses background jobs when free disk < 15 % and raises `CRITICAL` below 10 %.

Networks: `data` (internal: postgres, migrate, web, worker, bot) and `edge` (web, cloudflared). Secrets mounted from `/srv/cosmic/secrets` as files (`*_FILE` variables).

## 5. Cloudflare Tunnel, Access and DNS

Preconditions (B-03): owner names the zone and hostname, e.g. `<portal-host>.<zone>`; we inventory existing DNS records first (`deploy/cloudflared/dns-inventory.sh` uses a read-only API token: `Zone:DNS:Read`).

1. **Tunnel:** create a remotely-managed tunnel `cosmictrading-prod` in the Cloudflare dashboard (Zero Trust → Networks → Tunnels). Copy the token into `/srv/cosmic/secrets/tunnel_token` (0400) without echoing it. Public hostname: `<portal-host>.<zone>` → `http://web:8000`. Add a path rule `^/internal/` → `http_status:404` above it. Catch-all → `http_status:404`. (A locally-managed equivalent is in `deploy/cloudflared/config.example.yml`; validate with `cloudflared tunnel ingress validate`.)
2. **DNS:** the dashboard creates one proxied `CNAME <portal-host> → <tunnel-id>.cfargotunnel.com`. No other record is created, changed or deleted; nameservers are never changed. Rollback = delete only that CNAME and the tunnel.
3. **Access:** self-hosted application for `<portal-host>.<zone>`, policy *Allow* = the owner's e-mail (one-time PIN or the owner's IdP), session 12 h. Application login and MFA remain mandatory behind Access.
4. **Environments:** production uses its own tunnel and hostname; a staging hostname (if wanted) uses a separate tunnel and database. Production is pointed only at a build that passed `make check` and the local production smoke test.
5. **SSH:** not published. Remote SSH, if wanted, uses a separate Access-protected SSH application with `cloudflared access ssh` on the client.

### Verification (`deploy/cloudflared/verify-public.sh <hostname>`)

Each layer separately: DNS (`dig CNAME` → `*.cfargotunnel.com`), tunnel connector health (dashboard/`cloudflared tunnel info`), TLS (certificate chain valid), Access (unauthenticated request → redirect to Access login), application (`/readyz` through an Access service token if configured), `/internal/metrics` → 404, an unapproved hostname on the zone → not routed, origin isolation (`nmap`/`ss` on the VM shows no listening app port on the LAN interface). Only when all pass is the deployment called "deployed".

## 6. Deploy and update (`deploy/deploy.sh`)

`deploy.sh [--dry-run] <version>`: verify image digest/tag exists → take a pre-deploy encrypted backup → `docker compose pull` (or load) → run `migrate` → `up -d` → wait for `/readyz` and worker heartbeat → smoke test → on failure, print the rollback command (previous image tag; migrations are backward-compatible within a release, otherwise the release notes state the downgrade). No step deletes volumes.

## 7. Local production-like verification (what is actually executed in this repository)

`deploy/compose.prod.yaml` + `deploy/compose.local-verify.yaml` (publishes `web` on `127.0.0.1:8742` only, uses generated throwaway secrets in `var/secrets/`) on the workstation: build, migrate, readiness, login, paper flow, `docker compose restart`, kill -9 of the worker during a submission, backup + restore drill, `docker stats`. Results are recorded in `IMPLEMENTATION-STATUS.md`.
