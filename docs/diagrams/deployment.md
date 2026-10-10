# Deployment topology (target: dedicated Proxmox VM)

```mermaid
flowchart LR
  subgraph pve[Proxmox VE host - NOT ACCESSIBLE YET (B-02)]
    subgraph vm[VM cosmictrading: Ubuntu 24.04, 4 vCPU, 10 GB RAM, 200 GB disk, onboot=1]
      subgraph compose[Docker Compose project cosmictrading]
        cfd[cloudflared - profile tunnel]
        web[web :8000 internal]
        worker[worker]
        bot[bot - profile telegram]
        pg[(postgres:17 - volume pg_data)]
        mig[migrate - one-shot]
      end
      fs1[/srv/cosmic/secrets 0700/]
      fs2[/srv/cosmic/backups - encrypted dumps/]
      sysd[systemd: cosmictrading.service + backup.timer]
      ufw[ufw: deny inbound except SSH from LAN]
    end
  end
  cfd -- outbound 7844/443 --> cfe[Cloudflare edge]
  web --- cfd
  web --- pg
  worker --- pg
  bot --- pg
  mig --- pg
  sysd --> compose
```

Networks: `edge` (cloudflared ↔ web only) and `data` (internal: web, worker, bot, migrate ↔ postgres). No service publishes a host port in production.
