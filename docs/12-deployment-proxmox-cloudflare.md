# 12 — Deployment: Proxmox VM and Cloudflare

**Status (S9, 2026-10-11):** artefacts written; the application stack, backups and restore drill are **verified locally** in a production-like run (§7). VM deployment BLOCKED (B-02); tunnel and DNS BLOCKED (B-03). Nothing in this document has been executed against a Proxmox host or a Cloudflare zone; `create-vm.sh` and `bootstrap.sh` have only been dry-run and lint-checked (shellcheck).

## 1. Target

| Item | Plan |
|---|---|
| Hypervisor | Proxmox VE (version to be recorded on access). The application never runs on the hypervisor host. |
| Guest | One dedicated VM `cosmictrading`, Ubuntu Server 24.04 LTS cloud image, 4 vCPU (`host` CPU type), 10 GB RAM (no ballooning), 200 GB disk on SSD-backed storage (`discard=on`, `ssd=1`), virtio NIC on the LAN bridge, QEMU guest agent, `onboot=1`, startup order after storage/network services. |
| Disks | Single 200 GB virtual disk. Layout: `/var/lib/docker` (images + the `cosmictrading_pgdata` named volume), `/srv/cosmic/src` (git checkout), `/srv/cosmic/config` (root:10001 0750, non-secret), `/srv/cosmic/secrets` (root:10001 0750, files 0440), `/srv/cosmic/backups` (10001:10001 0700). Recommended: a second virtual disk mounted at `/srv/cosmic/backups` so a full data disk cannot destroy backups, plus an off-VM copy. |
| Network | Static DHCP reservation or static IP (owner supplies). Outbound: 443 (Cloudflare, data sources, AI, Telegram, OS updates), 7844 TCP/UDP (cloudflared). Inbound: SSH from the LAN management subnet only; no application ports. |
| OS hardening | non-root `cosmic` user in `docker` group; SSH keys only, no root login; `ufw` default deny inbound; `unattended-upgrades` (security); time sync (systemd-timesyncd); journald size cap. |

## 2. Provisioning (`deploy/proxmox/create-vm.sh`)

Dry run by default: prints every command; executes only with `--apply` on the Proxmox host as root. Read-only prechecks (VMID free, storage and bridge exist, `pveversion`) run first; it refuses an existing VMID and never changes host networking, storage definitions, the Proxmox firewall or other VMs. Steps: download the Ubuntu 24.04 cloud image and verify it against Ubuntu's `SHA256SUMS`; `qm create <VMID> --name cosmictrading --memory 10240 --balloon 0 --cores 4 --cpu host --net0 virtio,bridge=<BRIDGE> --scsihw virtio-scsi-single --agent enabled=1 --onboot 1 --startup order=10,up=60`; `qm set --scsi0 <STORAGE>:0,import-from=<image>,discard=on,ssd=1,iothread=1` (needs PVE ≥ 7.2); `qm resize scsi0 200G`; cloud-init drive with `--ciuser cosmicadmin --sshkeys <owner key> --ipconfig0`; optional `--cicustom` with `deploy/vm/cloud-init.yaml`; optional `--start`. **Not yet executed against a real Proxmox host** — verify each command on first use.

## 3. Guest setup (`deploy/vm/bootstrap.sh`)

Idempotent; dry run by default, `--apply` as root; requires `--mgmt-cidr` (the LAN subnet allowed to SSH) so enabling the firewall cannot lock the owner out. Installs `docker.io`, `docker-compose-v2`, `ufw`, `unattended-upgrades`, `qemu-guest-agent`, `git`, `jq` from the Ubuntu archive (no third-party repositories); Docker daemon defaults (json-file 10 MB × 5, `live-restore`, `no-new-privileges`, no userland proxy); SSH drop-in (keys only, no root, no forwarding); journald cap 1 GB; unattended security upgrades; `ufw` default-deny inbound + SSH from the management subnet only; group 10001; `/srv/cosmic/{config,secrets,backups}` with the modes above; generates database passwords, the app secret key and the metrics token **only if absent** (never printed, never overwritten); empty optional secret files (= NOT_CONFIGURED); copies the env templates; installs and enables (does not start) `cosmictrading.service` and `cosmic-backup.timer`. Any existing file that would change is first copied to `<file>.bak-<UTC>`.

Firewall plan: inbound deny-all except TCP 22 from the management subnet; outbound allow (HTTPS 443 to data sources, AI providers, Telegram, Ubuntu mirrors; 7844 TCP/UDP for cloudflared; NTP 123/UDP). Docker publishes no ports in production, so Docker's iptables rules do not open anything (Docker would bypass `ufw` for published ports — which is why none exist).

## 4. Application stack (`deploy/compose.prod.yaml`, `deploy/Dockerfile`)

Image: multi-stage build on `python:3.12-slim-trixie` pinned by digest; dependencies installed from `uv.lock` with `--frozen --no-dev`; application and standard-library bytecode precompiled (the official image ships no stdlib `.pyc`, and the read-only root filesystem would otherwise recompile it on every process start); runtime user UID/GID 10001; no compilers, no test tools. Size measured: 380 MB. The backup image is `postgres:17-alpine` (same digest as the server) plus `age` (443 MB).

| Service | Image | Limits | Networks | Notes |
|---|---|---|---|---|
| postgres | `postgres:17-alpine@sha256:b0f9…2b24` | 2 GB, 1.5 CPU, 300 PIDs | data | `--data-checksums`; `shared_buffers=512MB`, `effective_cache_size=1536MB`, `max_connections=80`; caps reduced to CHOWN/DAC_OVERRIDE/FOWNER/SETGID/SETUID |
| migrate | cosmic | 512 MB, 1 CPU | data | one-shot `cosmic db bootstrap && cosmic db migrate`; the only service with the owner/superuser passwords besides postgres/backup |
| web | cosmic | 512 MB, 1 CPU, 200 PIDs | data, edge (fixed IP) | healthcheck: stdlib-only request to `/readyz` every 30 s (0.12 s CPU; `cosmic probe web` cost 1.6 s and is kept for deploy smoke tests); trusts `X-Forwarded-For` only from cloudflared's fixed IP |
| worker | cosmic | 1 GB, 1.5 CPU, 200 PIDs | data, egress | healthcheck: `/tmp/worker.heartbeat` younger than 60 s — touched only after a successful DB heartbeat (every 15 s); `cosmic probe worker` checks the DB row for smoke tests; backups mounted read-only only to measure free space |
| bot | cosmic | 256 MB, 0.5 CPU | data, egress | profile `telegram` |
| cloudflared | `cloudflare/cloudflared:2026.9.1@sha256:b269…54e4` | 128 MB, 0.5 CPU | edge (fixed IP), egress | profile `tunnel`; `--token-file`; healthcheck `tunnel ready` |
| backup, backup-prune | backup image / cosmic | 512 MB | data / none | profile `backup`, run by the timer |

All app containers: `read_only: true`, tmpfs `/tmp`, `cap_drop: ALL`, `no-new-privileges`, `init: true`, json-file logs 10 MB × 5, `restart: unless-stopped`. `data` and `edge` are **internal** networks: the web process cannot reach the internet (verified locally: TCP connect and DNS both fail). No service publishes a port.

Secrets are files in `/srv/cosmic/secrets` mounted per service as `/run/secrets/<name>` and read through `COSMIC_*_FILE`. An empty optional file (AI keys, Telegram token, metrics token) means NOT_CONFIGURED. Runtime processes need only the app role's password; the owner password is required only where it is used.

Budget: limits total ≈ 4.4 GB of 10 GB; measured use in the local run is far below the limits (§7). Disk: database ≤ 60 GB, backups ≤ 60 GB (retention-pruned), logs ≤ 5 GB, images ≤ 10 GB, free reserve ≥ 50 GB. The worker pauses background jobs when free disk < 15 % and raises `CRITICAL` below 10 %.

## 5. Cloudflare Tunnel, Access and DNS

Preconditions (B-03): owner names the zone and hostname, e.g. `<portal-host>.<zone>`; we inventory existing DNS records first with `deploy/cloudflared/dns-inventory.sh <zone> <token-file>` — HTTP GET only, a token scoped to Zone:Read + DNS:Read, the token passed to curl through a file descriptor (never in argv), output as TSV. The hostname is used only if no record with that name exists.

1. **Tunnel:** create a remotely-managed tunnel `cosmictrading-prod` in the Cloudflare dashboard (Zero Trust → Networks → Tunnels). Copy the token into `/srv/cosmic/secrets/tunnel_token` (0400) without echoing it. Public hostname: `<portal-host>.<zone>` → `http://web:8000`. Add a path rule `^/internal/` → `http_status:404` above it. Catch-all → `http_status:404`. (A locally-managed equivalent with the same rules is `deploy/cloudflared/config.example.yml`, with `portal.example.com` as the placeholder. Verified with cloudflared 2026.9.1: `ingress validate` → OK; `ingress rule` maps `/internal/metrics` → rule 0 (404), `/` and `/approvals` → rule 1 (`http://web:8000`), any other hostname → rule 2 (404).)
2. **DNS:** the dashboard creates one proxied `CNAME <portal-host> → <tunnel-id>.cfargotunnel.com`. No other record is created, changed or deleted; nameservers are never changed. Rollback = delete only that CNAME and the tunnel.
3. **Access:** self-hosted application for `<portal-host>.<zone>`, policy *Allow* = the owner's e-mail (one-time PIN or the owner's IdP), session 12 h. Application login and MFA remain mandatory behind Access.
4. **Environments:** production uses its own tunnel and hostname; a staging hostname (if wanted) uses a separate tunnel and database. Production is pointed only at a build that passed `make check` and the local production smoke test.
5. **SSH:** not published. Remote SSH, if wanted, uses a separate Access-protected SSH application with `cloudflared access ssh` on the client.

### Verification (`deploy/cloudflared/verify-public.sh <hostname>`)

Each layer separately: DNS (`dig CNAME` → `*.cfargotunnel.com`), tunnel connector health (dashboard/`cloudflared tunnel info`), TLS (certificate chain valid), Access (unauthenticated request → redirect to Access login), application (`/readyz` through an Access service token if configured), `/internal/metrics` → 404, an unapproved hostname on the zone → not routed, origin isolation (`nmap`/`ss` on the VM shows no listening app port on the LAN interface). Only when all pass is the deployment called "deployed".

## 6. Deploy and update (`deploy/deploy.sh`)

`deploy.sh [--dry-run] [--build] <tag>` (tag = git short SHA): preflight (Docker present; `cosmic.env`, `compose.env`, backup recipients present; required secret files non-empty; optional ones exist) → with `--build`, refuse unless the checkout is clean and exactly at `<tag>`, then build both images → confirm the images exist → if a release is running, take an encrypted pre-deploy backup with the **running** release's backup image → back up `compose.env`, set `COSMIC_IMAGE_TAG` → `docker compose up -d --wait` (migrations run first through `migrate`) → smoke tests (`probe web`, `probe worker`, `audit verify`, `golive status`) → append to `deploy-history.log`; on failure print the exact rollback commands. It never deletes a volume. A dry run prints every step and says that nothing was changed or verified (checked locally against a simulated `/srv/cosmic`; a missing secret and a dirty checkout are refused).

`cosmictrading.service` (oneshot, `docker compose up -d --wait` after `docker.service` and `network-online.target`) restores the stack after a reboot; containers also have `restart: unless-stopped`. `cosmic-backup.timer` runs `deploy/backup/backup.sh` daily at 02:30 UTC (persistent, 5 min jitter).

## 7. Local production-like verification (what is actually executed in this repository)

`scripts/local-verify.sh --fresh` runs `compose.prod.yaml` unchanged plus `compose.local-verify.yaml`, which only adds a TCP relay standing in for cloudflared (same edge address, so the web process keeps exactly its production networks and trust settings) published on `127.0.0.1:8743`, a separate project name/volume, and throwaway secrets in `var/verify/` (gitignored). `--fresh` removes only that project. The portal is driven over HTTP exactly as a browser would (`scripts/local_verify.py`: forms, CSRF tokens, TOTP); `docker` is used only to kill/restart containers and read invariants from PostgreSQL.

**Final run on the S10 code, 2026-10-11 22:52 UTC (`var/verify/run-20261011T225224Z.log`; the S9 run `run-20261011T175651Z.log` gave the same results), all 13 steps PASS:**

| Step | Result |
|---|---|
| Build both images; stack up with `--wait` (postgres → migrate → web, worker) | PASS (27 s to healthy) |
| Owner created with the real CLI inside a container | PASS |
| Portal flow, 22 checks: readiness through the relay; CSP/headers; `/internal/metrics` 404; unknown Host 400; wrong password 401; TOTP enrolment; paper account; mode → PAPER_TRADING; reconciliation OK; proposal passes R01–R17 on **real delayed Coinbase + Bitstamp data**; bound approval; order FILLED by the paper venue; protective stop placed; re-used approval → 409; kill switch → EMERGENCY_STOP with the stop kept; a complete proposal refused by R01; stop-triggered reconciliation passed; resume via the checklist; audit order stop → reconciliation → resume | 22/22 |
| AT-20 chaos (container level): 4 approved trades, worker container SIGKILLed 0.24–2.97 s after approval (2 kills with the entry ACKNOWLEDGED at the venue, 2 before the order existed), restarted each time | 17/17 — no order left AUTHORIZED/SUBMITTING/SUBMITTED/UNKNOWN, no duplicate venue order, ≤ 1 entry per intent, no approval consumed twice, reconciliation OK |
| `docker compose restart` of the whole stack with 5 resting orders | 10/10 — all survived (in the S9 run one filled meanwhile, confirmed by a BROKER event), no duplicate/orphan venue orders, reconciliation OK, sessions still valid |
| `cosmic audit verify` in the worker container | ok, 68 events |
| Encrypted backup + retention; no plaintext in the backup directory | PASS |
| Restore drill into a `--network none` container (AT-19) | DRILL PASS in 3–4 s (10 checks, doc 14 §5) |

The random SIGKILLs did not land inside the few-millisecond window between persisting `SUBMITTING` and the venue's answer; that window is covered deterministically in-process by `test_at20_restart_recovers_without_resubmitting` and `test_at20_authorized_but_never_sent_is_closed`. VM reboot (the VM part of AT-20) is BLOCKED (B-02).

**Isolation checks (manual, same stack):** web container cannot open a TCP connection to the internet or resolve DNS; worker can reach `api.exchange.coinbase.com`; only the relay publishes a port (127.0.0.1); app containers run as UID 10001 with a read-only root filesystem.

**Resources (six `docker stats` samples 10 s apart, idle after the run):**

| Container | CPU avg / max | Memory avg (limit) | PIDs |
|---|---|---|---|
| postgres | 2.6 % / 9.2 % | 75 MiB (2 GiB) | 12 |
| worker | 9.0 % / 29.7 % | 63 MiB (1 GiB) | 5 |
| web | 0.2 % / 0.3 % | 74 MiB (512 MiB) | 6 |
| relay (verification only) | 0 % | 1 MiB | 1 |

Database 12 MB after the run; images 380 MB (app) + 443 MB (backup tool). An earlier run measured web at 25.6 % average CPU while idle: the healthcheck started a full `cosmic` process every 15 s (1.6 s CPU each, standard library recompiled every time). Fixed by precompiling the standard library and using stdlib-only healthchecks every 30 s (0.12 s CPU each).

**Defects found by this verification and fixed:** periodic jobs sharing a kind (quotes, bars, instruments per source) were deduplicated by kind only, so Bitstamp was never polled and R10 always failed closed in a real deployment (regression test added); runtime processes required the database owner password (least privilege restored); uvicorn trusted `X-Forwarded-For` from any client (now only cloudflared's address; `*` refused); the restore drill used a helper missing from the current postgres image; backup retention could prune a same-day pre-deploy backup (newest 3 sets now always kept); idle healthcheck CPU (above).
