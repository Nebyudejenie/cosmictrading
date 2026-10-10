# 01 — Current-State Audit (Phase 0 baseline)

**Date:** 2026-10-10 · **Method:** read-only inspection of the repository, workstation and reachable network. Nothing was modified before this audit was written.

## 1. Repository

| Item | Finding |
|---|---|
| Project directory | `/home/prophet/trading` |
| Contents at start | One file: `COSMICTRADING_QUANT_MASTER_BUILD_PROMPT.md` (103,419 bytes, 1,468 lines) |
| Git | No repository of its own. The directory sat inside a Git repository rooted at `/home/prophet` (the whole home directory) with **no commits** and hundreds of untracked personal files. |
| Action taken | A standalone repository was initialised in `/home/prophet/trading` (branch `main`) so project history can never mix with the home directory (ADR-001). The prompt file is kept verbatim. |
| Code, tests, manifests, migrations, env examples, CI | None. |
| `AGENTS.md`, `README.md`, `cosmictrading.md` | None existed. A search of the home directory (depth 4) found no `cosmictrading*` file other than the prompt. |
| TODO/mock/stub/secret search | Not applicable: no code. Re-run as a release check (S10). |
| Baseline test run | Not applicable: no tests existed. No pre-existing failures to record. |

### The missing baseline

The prompt states it is "an execution directive, not a replacement for `cosmictrading.md`" and that the V4/V5 baseline (Sections 0–1803: Global Intelligence Operating System, Search Fabric, institutional knowledge graph, digital workforce, Telegram/Mini App, owner portal, resource controls, security, risk, audit, resilience) must accompany V6. That baseline is not present. The owner instructed that the master prompt be treated as the product vision. Consequences (ADR-002):

- Requirements are derived only from the 1,468 lines that exist; nothing is invented to "fill in" the missing sections.
- Capabilities that the prompt only names (World Brain, Search Fabric, knowledge graph, broad global coverage) are recorded as a capability map (MIS-03), BLOCKED on the baseline and DEFERRED by scope.

## 2. Workstation (development environment)

| Item | Observed |
|---|---|
| OS | Ubuntu 24.04.5 LTS on WSL2 (kernel 6.6.87.2-microsoft-standard-WSL2) |
| CPU / RAM | 4 logical CPUs; 7.8 GiB RAM, about 2.2 GiB available at inspection; 16 GiB swap |
| Disk | 1007 GB volume, 795 GB free |
| Python | 3.12.3 (system), `uv` 0.11.15 |
| Node | 22.22.1, npm 10.9.4, pnpm 10.32.1 (not needed by this design) |
| Docker | Docker Desktop 29.1.x, Compose v2.40.3; 4 CPUs / 7.76 GiB shared with other stacks |
| Other tools | `cloudflared` 2026.9.1 (supports `--token-file`), `gh` 2.45, `git` 2.43, `jq` 1.7, `gpg` 2.4.4, `make` 4.3, `curl` 8.5 |
| Missing | `psql`, `pg_dump`, `sqlite3`, `redis-server`, `age`, Proxmox CLIs (`qm`, `pvesh`) |
| Cached images | `python:3.12-slim`, `postgres:17-alpine` |

### Other projects sharing this machine (must not be disturbed)

Running containers at inspection: `arada-dev-api-1`, `arada-dev-postgres-1` (publishes 127.0.0.1:55432), `openrelay-*` (including its own `cloudflared` tunnel container), `jobingo-postgres`, `jobingo-redis`. COSMICTRADING uses its own Compose project name, network, volumes and ports (Postgres 127.0.0.1:55442, web 127.0.0.1:8742) and never touches these. The `~/.cloudflared` directory contains credentials belonging to other projects; COSMICTRADING does not read or use them.

## 3. Infrastructure and external access

| Item | Finding | Status |
|---|---|---|
| Proxmox host | No Proxmox CLI, API endpoint or credentials found. | BLOCKED (B-02) |
| SSH alias `cosmic` (192.168.74.80) | A libvirt/KVM host (backup VM definition `hermis-ubuntu-ai.xml` in `~/cosmicServer-backup`). SSH probe timed out from this machine. Not Proxmox as far as can be determined. | Unreachable |
| Target VM spec | Stated by the prompt: Proxmox Linux VM, 10 GB RAM, 200 GB SSD, CPU-first. Treated as the design budget, not as verified hardware. | NOT VERIFIED |
| Domain / Cloudflare zone | None designated for COSMICTRADING. Owner decision 2026-10-10: no domain yet, no DNS changes. | BLOCKED (B-03) |
| Cloudflare API token | None in the environment. | BLOCKED (B-03) |
| AI provider keys | None in the environment (Groq, Gemini, OpenRouter, Cerebras). | BLOCKED (B-04) |
| Telegram bot token / operator ID | None. | BLOCKED (B-05) |
| Broker / exchange | None selected; no credentials. | BLOCKED (B-06, B-07) |

## 4. Network reachability (keyless public endpoints, 2026-10-10)

| Endpoint | Result |
|---|---|
| `api.exchange.coinbase.com` (products, candles, ticker) | 200 OK, ~0.8 s; responses captured as fixtures |
| `www.bitstamp.net/api/v2` (ohlc, ticker, markets) | 200 OK, ~0.9 s; responses captured as fixtures |
| `status.coinbase.com/api/v2/status.json` | 200 OK |
| `api.gemini.com` | 200 OK |
| `api.kraken.com` | **Connection timeout** (20 s, repeated) |
| `api.binance.com` | **Connection timeout** |
| `openrouter.ai/api/v1/models` | 200 OK; 458 models, 15 with `:free` suffix and `pricing.prompt = pricing.completion = "0"` |
| `api.groq.com/openai/v1/models` | 401 without key (reachable) |
| `generativelanguage.googleapis.com` | Reachable; `ai.google.dev` documentation pages could not be fetched by the documentation tool |
| `pypi.org` | Reachable but slow (~12 s) |

The Cloudflare edge serving these responses reported the `ADD` (Addis Ababa) colo. Kraken and Binance unreachability may be regional; it is recorded as an environment fact for this network only (B-09), and the first market-data slice uses Coinbase Exchange (primary) and Bitstamp (cross-check) instead (ADR-015).

## 5. Provider facts verified on 2026-10-10 (official documentation)

| Provider | Verified | Not verified |
|---|---|---|
| Groq | Rate-limit headers `x-ratelimit-{limit,remaining,reset}-{requests,tokens}`; `retry-after` on 429 (console.groq.com/docs/rate-limits) | The free-plan model list and numeric limits (page shows Developer-plan table only) |
| OpenRouter | `:free` models: 20 requests/minute; 50 requests/day if fewer than 10 credits ever purchased, 1,000/day otherwise (openrouter.ai/docs/api/reference/limits). Public `/api/v1/models` exposes per-model pricing for a runtime $0 check. | Data-retention terms of individual free model hosts |
| Cerebras | Free access is a **Free Trial funded by $5 of credits that expire after 30 days**; 5 RPM, 1M tokens/day for listed models (inference-docs.cerebras.ai) | — |
| Google Gemini | — | Free-tier models, limits and data-use terms: the official pages could not be fetched; third-party sources disagree. |

Consequences are recorded in doc 06 and ADR-014: Cerebras is excluded from the default allowlist (time-limited credit trial, not a standing free tier); Gemini and Groq require an operator attestation recorded in the system before use; every provider is assumed to retain and possibly review prompts, so no sensitive data is ever sent.

## 6. Owner precedent

The owner's other active project (ARADA, `/home/prophet/arada`) uses Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2 Core/asyncpg + Alembic + aiogram 3, Docker Compose and no Kubernetes (its ADR-004, ADR-020). COSMICTRADING shares no code, runtime, database, tunnel or credentials with it; the precedent informs only the stack choice (ADR-004).

## 7. Unverified assumptions (to be confirmed by the owner)

1. The production VM will have 10 GB RAM, 200 GB SSD and a CPU-only profile (from the prompt).
2. The VM will run Ubuntu 24.04 LTS (our choice, ADR-017; any current Debian/Ubuntu LTS works).
3. Paper-trading fee and slippage parameters are assumptions, labelled `ASSUMPTION` in the UI, until the owner confirms the fee schedule of the venue they choose.
4. Public Coinbase/Bitstamp market data is used for the operator's own analysis, not redistribution. The owner should confirm this matches the venues' terms for their jurisdiction.
