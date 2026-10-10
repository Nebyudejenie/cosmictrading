# Blockers Register

Each blocker records what is missing, why it matters, how to resolve it safely, what stays blocked, what continues meanwhile, and how resolution is verified (directive §21). Work continues on everything that does not depend on a blocker.

| ID | Blocker | Status |
|---|---|---|
| B-01 | Missing baseline vision (`cosmictrading.md`, Sections 0–1803) | OPEN |
| B-02 | No Proxmox host access | OPEN |
| B-03 | No domain / Cloudflare zone / API token | OPEN |
| B-04 | No free-tier AI provider keys | OPEN |
| B-05 | No Telegram bot token / operator Telegram ID | OPEN |
| B-06 | No broker/exchange selected | OPEN |
| B-07 | No least-privilege broker sandbox/live credentials | OPEN |
| B-08 | Venue fee schedule unconfirmed for paper simulation | OPEN |
| B-09 | Kraken and Binance APIs unreachable from the development network | OPEN (worked around) |

---

## B-01 — Missing baseline vision

- **Missing:** `cosmictrading.md` / the V4/V5 baseline (Sections 0–1803) that the master prompt says must accompany V6.
- **Why required:** it defines the Global Intelligence OS, Search Fabric, knowledge graph, Mini App and other capabilities that the prompt only names.
- **How to obtain:** the owner places the file in the repository root (it may contain no secrets; review before committing).
- **Blocked:** requirement extraction for those capabilities (MIS-03); nothing in the first release depends on it.
- **Continues meanwhile:** everything in this repository, derived from the 1,468 lines that exist.
- **Verification:** file present; traceability matrix extended with new IDs; ADR-002 updated.

## B-02 — Proxmox host access

- **Missing:** a reachable Proxmox VE host, an account or API token able to create one VM, the storage/bridge names, and the VM's IP plan.
- **Why required:** the deployment target is a dedicated Proxmox VM (§13). The SSH alias `cosmic` (192.168.74.80) is a libvirt/KVM host and timed out from this machine.
- **How to obtain safely:** create a Proxmox user/token restricted to the target VM ID range (role `PVEVMAdmin` on `/vms/<id>` plus `Datastore.AllocateSpace` on the chosen storage), or run `deploy/proxmox/create-vm.sh` yourself on the host — it prints every command first and runs nothing without `--apply`.
- **Blocked:** DEP-02, DEP-18, VM autostart validation (DEP-09), VM-restart test (AT-20 VM part), real resource measurements on the target.
- **Continues meanwhile:** all deployment artefacts, local production-like verification in Docker, restart and restore drills on the workstation.
- **Verification:** `deploy/vm/verify.sh` on the VM reports services healthy, `/readyz` OK, firewall active, autostart enabled; results recorded in `IMPLEMENTATION-STATUS.md`.

## B-03 — Domain, Cloudflare zone and API token

- **Missing:** the hostname(s) to publish, the Cloudflare zone, a tunnel token, and (optionally) an API token scoped to `Zone:DNS:Edit` for that zone plus `Account:Cloudflare Tunnel:Edit`.
- **Why required:** public operator access via Cloudflare Tunnel + Access (§14). Owner decision 2026-10-10: no domain yet.
- **How to obtain safely:** follow doc 12 §5: create the tunnel in the dashboard, store the token only in `/srv/cosmic/secrets/tunnel_token` (mode 0400), export the existing DNS records for the zone before any change, add only one new proxied CNAME for the chosen hostname, add a Cloudflare Access application restricted to the owner.
- **Blocked:** CF-08, CF-13, public access verification.
- **Continues meanwhile:** tunnel ingress template (placeholder hostname, catch-all 404), Access policy design, verification script, local validation of the ingress file.
- **Verification:** `deploy/cloudflared/verify-public.sh <hostname>` checks DNS CNAME target, TLS, Access redirect, app login, `/internal/*` = 404 and unknown hostname = 404.

## B-04 — Free-tier AI provider keys

- **Missing:** API keys for at least one approved free route (Groq free plan, Gemini API free tier, or OpenRouter `:free` models), plus the owner's attestation that the account has no billing/credits attached where that determines charging.
- **Why required:** live AI research narratives.
- **How to obtain safely:** create keys on accounts with **no payment method** (Groq, Gemini) or with a key credit limit of 0 (OpenRouter). Put them only in `/srv/cosmic/secrets/` (or `.env` locally). Record the attestation in the portal (`AI providers → Attest free tier`, MFA required) with the date and the page you checked.
- **Blocked:** live AI calls; AI-researched proposals (missions pause at the narrative step with status `WAITING_FOR_AI`).
- **Continues meanwhile:** the whole gateway, policy, quota, cache, breaker and tests against a fake provider; deterministic research and risk.
- **Verification:** provider health page shows `HEALTHY`, last successful request time, usage rows with cost 0.

## B-05 — Telegram bot token and operator ID

- **Missing:** a bot token from @BotFather; the operator links their Telegram account in the portal with a one-time code.
- **Why required:** Telegram notifications, approvals and kill switch (V6 §1823).
- **How to obtain safely:** create the bot, store the token in `/srv/cosmic/secrets/telegram_bot_token`, enable the `bot` Compose profile, then `/link <code>` in a private chat.
- **Blocked:** live bot (TG-11).
- **Continues meanwhile:** all handler logic tested with simulated updates; the portal is a complete approval route.
- **Verification:** bot replies to `/status` only for the linked user; a test proposal round-trip in PAPER mode is audited.

## B-06 — Broker / exchange selection

- **Missing:** the owner's choice of venue for eventual live trading, the account type, and confirmation it is available in the owner's jurisdiction.
- **Why required:** a live adapter must be written and tested against that venue's official API and sandbox (V6 §1814).
- **Blocked:** Phase 9 adapter, go-live gates C, G (live comparison), I.
- **Continues meanwhile:** adapter contract, contract test suite, paper broker, full approval/execution path in PAPER mode.
- **Verification:** adapter passes the contract suite against the venue sandbox; readiness report updated.

## B-07 — Least-privilege broker credentials

- **Missing:** sandbox keys first, live keys only at Gate I; trading permission only, **withdrawals and transfers disabled**, IP allowlist where supported.
- **Blocked:** TRD-29 for live; every live gate.
- **Verification:** readiness report shows the key's permission scopes as read from the venue's key-introspection endpoint where one exists.

## B-08 — Venue fee schedule for paper simulation

- **Missing:** confirmation of the taker/maker fees that apply to the owner's account tier on the chosen venue.
- **Why required:** realistic paper fills and backtests (QNT-09).
- **Meanwhile:** fee and slippage parameters are configuration, shown in the UI with the label `ASSUMPTION`, and recorded with every backtest and simulated fill.
- **Verification:** owner updates the risk/simulation policy with the confirmed fees (audited change).

## B-09 — Kraken and Binance unreachable from the development network

- **Observed:** connection timeouts to `api.kraken.com` and `api.binance.com` on 2026-10-10, while Coinbase, Bitstamp and Gemini responded.
- **Workaround:** first connectors are Coinbase Exchange (primary) and Bitstamp (cross-check) (ADR-015).
- **Verification:** if the production VM's network reaches Kraken, a Kraken connector can be added behind the same contract.
