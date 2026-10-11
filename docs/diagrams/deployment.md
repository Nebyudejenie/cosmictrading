# Deployment topology (target: dedicated Proxmox VM)

As built in S9 (`deploy/compose.prod.yaml`). Verified locally in a production-like run; the VM itself is not accessible yet (B-02).

```mermaid
flowchart LR
  subgraph pve[Proxmox VE host - NOT ACCESSIBLE YET B-02]
    subgraph vm[VM cosmictrading: Ubuntu 24.04, 4 vCPU, 10 GB RAM, 200 GB disk, onboot=1]
      subgraph compose[Docker Compose project cosmictrading - no published ports]
        subgraph edge[network edge - internal]
          cfd[cloudflared 10.203.64.10 - profile tunnel]
          web[web :8000 - 10.203.64.20]
        end
        subgraph data[network data - internal]
          pg[(postgres:17 - volume cosmictrading_pgdata)]
          mig[migrate - one-shot]
          worker[worker]
          bot[bot - profile telegram]
          bkp[backup - profile backup, pg_dump to age]
        end
      end
      fs1[/srv/cosmic/secrets root:10001 0750/]
      fs2[/srv/cosmic/backups encrypted sets/]
      sysd[systemd: cosmictrading.service + cosmic-backup.timer]
      ufw[ufw: deny inbound except SSH from management LAN]
    end
  end
  cfd -- egress 7844/443 --> cfe[Cloudflare edge + Access]
  worker -- egress 443 --> ext[Coinbase, Bitstamp, status page, free AI APIs]
  bot -- egress 443 --> tg[Telegram Bot API]
  cfd --> web
  web --> pg
  worker --> pg
  bot --> pg
  mig --> pg
  bkp --> pg
  bkp --> fs2
  sysd --> compose
```

Networks: `edge` (internal: cloudflared ↔ web), `data` (internal: postgres and everything that uses it) and `egress` (worker, bot, cloudflared only). The web process has no route to the internet. Web trusts `X-Forwarded-For` only from cloudflared's fixed address. Before the tunnel exists, the owner reaches the portal through `compose.admin-access.yaml` (a relay on the VM's 127.0.0.1) and an SSH local forward.
