# 16 — Cost and Resource Budget

## 1. AI inference budget

**Exactly $0.** Only allowlisted free routes are callable; billing signals disable a provider immediately (doc 06). There is no payment method in the design, no credit top-up and no paid fallback.

## 2. Total operating cost (reported separately — the system is not "free")

| Item | Cost basis | Known? |
|---|---|---|
| Hardware (Proxmox host share) | Owner's existing hardware | Owner to state |
| Electricity, internet | Owner's utility costs | Owner to state |
| Domain | Registrar fee for the chosen zone | Not chosen (B-03) |
| Cloudflare Tunnel + Access | Free plan covers Tunnel; Access free tier covers a small number of users (verify current plan limits when configuring) | To verify at B-03 |
| Market data | Public keyless endpoints — $0 at current use | Terms to be confirmed by owner |
| Broker/exchange fees | Commissions, spreads, funding — **real costs once live** | Not configured (B-06, B-08) |
| Telegram | Free | — |
| Off-VM backup storage | Depends on target (owner NAS, cloud bucket) | Not chosen |
| AI | $0 (policy) | — |

## 3. Compute and storage budget (target VM: 4 vCPU, 10 GB RAM, 200 GB SSD)

| Component | Memory limit | CPU limit | Disk |
|---|---|---|---|
| PostgreSQL | 2.0 GB | 1.5 | ≤ 60 GB data |
| worker | 1.0 GB | 1.5 | — |
| web | 0.5 GB | 1.0 | — |
| bot | 0.25 GB | 0.5 | — |
| cloudflared | 0.125 GB | 0.5 | — |
| OS + Docker | ~1.5 GB | — | ~20 GB incl. images |
| Backups | — | — | ≤ 60 GB |
| Logs | — | — | ≤ 5 GB |
| Headroom / page cache | ~4.5 GB | — | ≥ 50 GB free reserve |

Measured in the local production-like run (2026-10-11, doc 12 §7; idle after a full verification): postgres 75 MiB / 3.4 % CPU, worker 68 MiB / 5.9 %, web 75 MiB / 0.2 %; database 12 MB; images 380 MB + 443 MB. Under 5 % of the memory limits — the limits leave room for load spikes, backups and growth, not for current use. These numbers come from a 4-CPU workstation and must be re-measured on the VM. Minimum viable profile: 2 vCPU / 4 GB RAM / 40 GB disk (no bot, smaller Postgres buffers) — not recommended for live trading.

## 4. Cost controls

Deterministic analytics first; AI only for narrative and red-team text; cache with explicit expiry; dedupe missions per market event; per-task and per-mission AI budgets; background research paused under resource pressure; optional subsystems (bot, tunnel, Prometheus) off by default.
