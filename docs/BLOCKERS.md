# Blockers Register

Each blocker records what is missing, why it matters, how to resolve it safely, what stays blocked, what continues meanwhile, and how resolution is verified (directive §21). Work continues on everything that does not depend on a blocker.

| ID | Blocker | Status |
|---|---|---|
| B-01 | Missing baseline vision (`cosmictrading.md`, Sections 0–1803) | OPEN |
| B-02 | No Proxmox host access | OPEN — staging runs on a shared test server (2026-10-12); the dedicated production VM is still pending |
| B-03 | No domain / Cloudflare zone / API token | RESOLVED for staging (2026-10-12): `cosmictrading.bond`, tunnels + Access live; production hostname `app.` reserved |
| B-04 | No free-tier AI provider keys | OPEN |
| B-05 | No Telegram bot token / operator Telegram ID | RESOLVED (2026-10-12): @cosmictradingBot running on staging; the owner links their Telegram account from the portal |
| B-06 | No broker/exchange selected | RESOLVED by owner decision (ADR-027): HFM primary, Binance second; Exness dropped |
| B-10 | HFM link: MetaTrader 5 on Windows and the bridge | IN PROGRESS: owner steps done, connection built (milestone 1); trading (milestone 2) to build |
| B-07 | No least-privilege broker sandbox/live credentials | Binance test network RESOLVED (2026-10-15); live OPEN |
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
- **Continues meanwhile:** all deployment artefacts, local production-like verification in Docker, restart and restore drills on the workstation. *(S9: done — see IMPLEMENTATION-STATUS S9.)*
- **Verification:** `deploy/vm/verify.sh` on the VM (read-only) reports postgres/web/worker healthy, `/readyz` OK, audit chain OK, firewall active, autostart and backup timer enabled, no application port on the LAN, a backup from the last 26 h, secret files not world-readable, disk < 85 %, live trading disabled; results recorded in `IMPLEMENTATION-STATUS.md`. Then reboot the VM and re-run it (AT-20 VM part).

## B-03 — Domain, Cloudflare zone and API token

- **Missing:** the hostname(s) to publish, the Cloudflare zone, a tunnel token, and (optionally) an API token scoped to `Zone:DNS:Edit` for that zone plus `Account:Cloudflare Tunnel:Edit`.
- **Why required:** public operator access via Cloudflare Tunnel + Access (§14). Owner decision 2026-10-10: no domain yet.
- **How to obtain safely:** follow doc 12 §5: create the tunnel in the dashboard, store the token only in `/srv/cosmic/secrets/tunnel_token` (root:10001, mode 0440 — the cloudflared container runs as UID 10001), export the existing DNS records for the zone before any change, add only one new proxied CNAME for the chosen hostname, add a Cloudflare Access application restricted to the owner.
- **Blocked:** CF-08, CF-13, public access verification.
- **Continues meanwhile:** tunnel ingress template (placeholder hostname, catch-all 404), Access policy design, verification script, local validation of the ingress file. *(S9: done — `cloudflared ingress validate` OK; `dns-inventory.sh` (GET-only) and `verify-public.sh` written, not run against a zone; break-glass access via `compose.admin-access.yaml` + SSH forward until the tunnel exists.)*
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

## B-10 — HFM through MetaTrader 5 (ADR-027)

- **Owner decisions (2026-10-15):**
  - HFM is the main broker; Binance is second.
  - Demo money only, buy only, no leverage.
  - The bridge runs on the owner's Windows PC for now, and on a Windows VM on Proxmox in production.
- **Verified:**
  - No official HFM trading API was found. HFM's MT5 page names Expert Advisors for automation, and hfm.com blocks automated reading.
  - MetaQuotes' `MetaTrader5` Python package talks to a running MT5 terminal on the same computer, and PyPI ships it for Windows only (`win_amd64`).
- **Owner steps:**
  1. In myHF (`my.hfm.com`), open an **MT5 demo** account.
  2. Install MetaTrader 5 on the Windows PC and log in to that demo account.
  3. Later, install Python for Windows and the bridge, when it is built (instructions will be added here).
- **To build:**
  - facts from the official MT5 Python documentation (`docs/integrations/mt5-python-api.md`);
  - the bridge with outbound-only, token-authenticated transport;
  - the adapter and an emulator-based contract suite;
  - CFD accounting: lots, contract size, margin, swaps, commission;
  - the demo-only, buy-only, no-leverage risk rules.
- **Production:** a Windows VM on Proxmox next to the Linux VM (B-02).
- **Verification:** the contract suite passes against the real HFM demo account through the bridge.

## B-06 — Broker / exchange selection

- **Owner's choice (2026-10-14):** Binance and Exness; markets BTC and gold.
- **Binance (spot):** public market data is connected (`binance_public`, keyless). Gold on Binance spot is the PAXG and XAUT tokens (each 1 troy oz). Binance's `XAUUSDT` gold contract is a leveraged perpetual future and stays out of scope (no leverage, no shorting). Trading needs the sandbox adapter, written against the official Spot API and run through the contract suite on the Spot Test Network (`testnet.binance.vision`) — see B-07.
- **Exness:** BLOCKED. Exness documents an official API (REST and WebSocket) for *Exness trading accounts* only — MetaTrader accounts cannot use it — and its developer documentation sits behind a browser check that automated access cannot pass. No endpoint, authentication scheme or rate limit can be verified, so nothing is built (never invent a broker API). Exness gold (`XAUUSD`) is a leveraged CFD: 100 oz per lot, minimum 0.01 lot (1 oz ≈ USD 4,100 at today's price), overnight swaps. Under the current risk policy it could only be held unlevered, and 1 oz exceeds the USD 500 per-order limit.
- **Also needed from the owner:** confirmation that both venues are available in the owner's jurisdiction and account type.
- **Blocked:** live adapters, go-live gates C, G, I.
- **Verification:** an adapter passes the contract suite against the venue's sandbox; readiness report updated.

## B-07 — Least-privilege broker credentials

- **Missing:** Binance Spot Test Network keys first (create at `testnet.binance.vision`, sign-in required; Ed25519 recommended; virtual balances, periodically reset); live keys only at Gate I — trading permission only, **withdrawals and transfers disabled**, IP allowlist. For Exness: a demo Exness trading account and access to the developer portal (see B-06).
- **Ready for the keys:** the Binance Spot Test Network adapter (`cosmic.trading.brokers.binance_spot`, ADR-025) passes the broker contract against an emulator built from the official documentation (`tests/trading/test_binance_adapter.py`; facts in `docs/integrations/binance-spot-api.md`, reviewed 2026-10-14). The real run is `make sandbox-binance` with `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_PRIVATE_KEY_FILE` (Ed25519 PEM). The adapter is not registered for any account until that run passes.
- **Built for the keys (ADR-026, 2026-10-15):** test-network (SANDBOX) accounts end to end. The worker alone holds the keys as Docker secrets. Contract runs are recorded with a key fingerprint, and an account can be opened only after a PASSED run for that key. Fees are booked in the asset charged. Reconciliation counts the test account's unmanaged holdings. All of this is tested against the emulator (`tests/trading/test_sandbox.py`).
- **Prepared on the server (2026-10-15):** an Ed25519 key pair, `~/.secrets/binance-testnet-ed25519.pem` (0600, never leaves the server) and its public key `binance-testnet-ed25519.pub.pem` (`MCowBQYDK2VwAyEAqzc9fyS2z/o1F6oT9GXW7mJuEgHgjfurSYdkZbmvaqQ=`). The worker reads copies in `~/apps/cosmictrading/secrets/` (`docs/15` §9).
- **Resolved for the test network (2026-10-15):**
  - The first saved key (fingerprint `d0f350dca9add161`) was refused with `-2015`.
  - The owner re-registered the server's public key and saved the new API key with the hidden-input command (`docs/15` §9).
  - The contract check then **PASSED** on the real Spot Test Network: all 8 steps, run `01a13ec1-8887-732b-a76d-7dc5c713e8db` at 08:50:33 UTC, key fingerprint `40ad1a36e4c400a2`.
  - Ed25519 keys on the test network have no Secret Key; only HMAC keys do.
  - `~/.secrets/binance-testnet-api-key.txt` still holds the refused key. The worker's copy in `~/apps/cosmictrading/secrets/` is the one in use.
- **Owner's real-account key (2026-10-15):** the owner provided an API key of their **real** Binance account. At their request it is kept for later, inert:
  - stored on the server as `~/.secrets/binance-live-api-key.txt` (0600);
  - read by no code and not mounted into any container;
  - its probable **Secret Key** (64 characters, saved 2026-10-15 06:05 server time as `binance-testnet-secret.txt`; the test network's Ed25519 keys have none) is kept at the owner's choice (A, 2026-10-15) as `~/.secrets/binance-live-secret.txt` (0600). It was not verified against Binance: using real-account credentials is not authorised.
  - Together the two could sign requests for the real account. Neither is read by any code or mounted into any container.

  Owner advice: on binance.com keep withdrawals disabled and restrict the key to the server's IP (196.191.95.113), or replace it at Gate I with a restricted Ed25519 key. Live trading stays disabled until gates A–I pass.
- **Blocked:** TRD-29 for live; every live gate; the real Binance sandbox contract run.
- **Verification:** readiness report shows the key's permission scopes as read from the venue's key-introspection endpoint where one exists.

## B-08 — Venue fee schedule for paper simulation

- **Missing:** confirmation of the taker/maker fees that apply to the owner's account tier on the chosen venue.
- **Why required:** realistic paper fills and backtests (QNT-09).
- **Meanwhile:** fee and slippage parameters are configuration, shown in the UI with the label `ASSUMPTION`, and recorded with every backtest and simulated fill.
- **Verification:** owner updates the risk/simulation policy with the confirmed fees (audited change).

## B-09 — Kraken and Binance unreachable from the development network

- **Observed:** connection timeouts to `api.kraken.com` and `api.binance.com` on 2026-10-10, while Coinbase, Bitstamp and Gemini responded.
- **Resolved 2026-10-14:** both answer from the workstation and the staging server (`/api/v3/ping` 200; server clock within 0.3 s of Binance). The workstation's network still drops individual connections now and then; the server does not.

## Resolution notes (2026-10-12)

- **B-03:** zone `cosmictrading.bond` (Cloudflare, Free plan; DNS inventory before any change showed **0 records**). Created only: tunnels `cosmictrading-staging` and `cosmictrading-ssh`, proxied CNAMEs `staging.` and `ssh.` (comment `managed-by: cosmictrading-setup`), Cloudflare Access apps for both (allow `nebiyudejenie@gmail.com`; service token `cosmictrading-ssh-automation` for automation). Other tunnels on the account (`arada`, `ebony`, `openrelay-arada`, `zemen-game`) untouched. `deploy/cloudflared/verify-public.sh staging.cosmictrading.bond` → ALL CHECKS PASSED. The setup token is stored on the server in `~/.secrets/cosmictrading-cf-token` (0600); revoke or let it expire when not needed.
- **B-05:** bot token stored only as a secret file on the server; `getMe` confirms @cosmictradingBot. The token was shared in chat — rotate it with @BotFather `/revoke` and replace the file.
- **B-02:** the shared test server (Ubuntu 24.04 KVM guest, also running other production stacks) hosts the staging environment without host changes (no sudo, no published ports, own Compose projects). It is not the dedicated VM the design requires for production.
