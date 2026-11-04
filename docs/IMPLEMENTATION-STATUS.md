# Implementation Status (living log)

Each entry records what was actually done and verified. Test counts come from real runs; anything not run is marked NOT_TESTED.

## Deployment state

Updated 2026-10-19; earlier sections keep the state as it was then.

| Layer | State |
|---|---|
| Local development | VERIFIED |
| Local production-like stack | VERIFIED (S9–S11 runs, 13/13 steps) |
| Production VM | RUNNING since 2026-10-19 at `staging.cosmictrading.bond` behind Cloudflare Tunnel and Access (S18); VM restart test passed 2026-10-20 |
| Staging | RUNNING on the same VM, loopback only (S18) |
| Shared test server | RETIRED 2026-10-19 (stopped; volumes kept) |
| AI providers | NOT CONFIGURED (B-04) |
| Telegram bot | RUNNING on production, linked to the owner |
| Binance market data | CONNECTED (keyless public data, real network and test network) |
| Binance test-network trading | READY (ADR-026): contract PASSED on the real test network (2026-10-15); account opening, the risk-policy change and trades are the owner's steps |
| Exness | BLOCKED (B-06) |
| Live trading | DISABLED: no live adapter exists, live modes are unreachable |

---

## Gate A — Design package (2026-10-10)

- **Requirements addressed:** GOV-01…11, GOV-13, GOV-14, GOV-20, PRIN-09/10, ARC-01/06, DATA-01, SEC-01 (draft), COST-02, REPO-07.
- **Files:** `docs/00`–`16`, `docs/17-ADRs/` (22 ADRs), `docs/diagrams/` (5 diagram files), `docs/BLOCKERS.md`, this file, `.gitignore`, `README.md`, `AGENTS.md`.
- **Verification performed:** read-only environment inventory; reachability of external endpoints; official documentation checks for Groq, OpenRouter, Cerebras, Coinbase Exchange, Bitstamp (Gemini pages unreachable — recorded as not verified); sample responses captured for fixtures.
- **Tests run:** none (no code yet).
- **Security impact:** threat model drafted; no secrets read or stored. `~/.cloudflared` and other projects' credentials were listed by name only and not used.
- **Next:** S1 Foundation.

## S1 — Foundation (2026-10-11)

- **Implemented:** uv project with locked dependencies; `Makefile` (`make check` = format, lint, mypy strict, secret scan, tests); `compose.dev.yaml` (Postgres 17 on 127.0.0.1:55442); `.env` generator with random local secrets; validated configuration with environment tiers, `*_FILE` secrets, fail-early non-secret errors, LIVE tiers refused; structured JSON logging with correlation IDs and key/value/pattern redaction; database bootstrap (roles `cosmic_owner`/`cosmic_app`), Alembic migration `0001_foundation` with explicit SQL; hash-chained append-only audit (DB trigger, `audit_verify()`, UPDATE/DELETE/TRUNCATE blocked for every role); identity (argon2id, lockout after 5 failures, server-side sessions with hashed tokens, absolute + idle expiry, revocation, TOTP MFA with AES-GCM-encrypted secrets and replay protection, step-up for privileged capabilities); capability model (12 directive capabilities + VIEW + ACTIVATE_EMERGENCY_STOP; roles in code); web app (CSP and security headers, trusted hosts, CSRF synchronizer token + Origin check, login double-submit CSRF, per-IP/per-user rate limits, safe error pages with correlation IDs, `/healthz`, `/readyz`); worker framework (Postgres SKIP LOCKED queue, idempotent periodic scheduling, lease recovery, full-jitter backoff, priority shedding under disk pressure, heartbeats); incidents with dedup/escalation; periodic audit-chain verification and disk checks; owner bootstrap CLI (interactive prompts only).
- **Migrations:** `0001_foundation` (tested up → down → up; metadata drift test passes).
- **Tests run:** `make check` → ruff format/check clean, mypy strict clean (54 files), secret scan passed (144 files), **83 passed** (43 unit, 28 integration, 12 security) in 33.6 s.
- **Manual verification:** `cosmic web` against the dev database: `/healthz` 200, `/readyz` ready (database + schema), `/login` 200, `/` → 303 `/login`, static CSS 200.
- **Bug found and fixed during the slice:** login/MFA failures were raised inside the transaction, which would have rolled back the failed-attempt counter, lockout and the audit record; both now return an outcome and the caller reports after commit (covered by `test_failed_logins_lock_account_and_are_audited`).
- **Security impact:** authentication, sessions, CSRF, headers, rate limits, audit immutability and secret redaction in place and tested.
- **Known limitations:** in-process rate limiting resets on restart (lockout is persistent); TOTP secret shown as text (no QR code library, by choice); uvicorn's own startup lines are plain text, not JSON.
- **Next:** S2 market data and provenance.

## S2 — Market data and provenance (2026-10-11)

- **Implemented:** migration `0002_market_data` (sources with full §6 provenance metadata, fetch runs, data-quality results, venues, assets, instruments with tick/lot/min-notional rules and provenance, instrument listings, bars, quotes, venue status, market events, documents, claims, evidence links, contradictions); code-defined registry (Coinbase Exchange public, Bitstamp public, Coinbase status; BTC/USD and ETH/USD on both venues, Bitstamp reference-only); connectors as pure parsers + rate-limited fetchers (exact `Decimal` JSON parsing, 429/5xx/timeout mapping); ingestion with fetch-run vintage on every row, validation with recorded reasons, idempotent upserts, batched bar upserts, completed-bar revision tracking, still-open bar flagged and excluded from analytics, instrument-rule change events; freshness labels (DELAYED/STALE/UNKNOWN/UNAVAILABLE/NOT_CONFIGURED; LIVE reserved for licensed streams); cross-venue deviation; source-stale incidents; SSRF guard; deterministic indicators (SMA, EMA, Wilder RSI/ATR, Bollinger, MACD, log returns, realised volatility, max drawdown, correlation); worker jobs for quotes (60 s), bars (300 s), rules (6 h), venue status (300 s), freshness (120 s); portal pages `/sources` (provenance, fetch runs, coverage gaps), `/market` (watchlist with labels, cross-venue check, venue status), `/market/{instrument}` (rules, SVG chart of completed bars, indicator estimates).
- **Migrations:** `0002_market_data` (drift test passes; up/down/up passes).
- **Tests run:** `make check` → 137 passed, 1 deselected (live), mypy strict clean, secret scan passed (191 files); `tests/integration/test_ingestion.py` 11 passed; `tests/e2e/test_market_pages.py` 3 passed; opt-in `make smoke-live-data` **1 passed against the real public endpoints** (2026-10-10 21:1x UTC).
- **Manual verification:** `cosmic worker` run for 45 s against the dev database with real network: 9 jobs DONE, 5 fetch runs SUCCESS, 4 quotes, 700 hourly bars (2 open bars flagged), 2 instruments' rules synced, Coinbase venue status OPERATIONAL, heartbeat recorded, graceful stop on SIGTERM.
- **Bugs found and fixed:** registry "inserted" detection miscounted repeated upserts at the same instant (now `xmax = 0`); bar ingestion was N+1 (one SELECT per bar) — batched, cutting related test time from ~69 s to 28 s; httpx request logging lowered to WARNING.
- **Known limitations:** Bitstamp has no status source (shown as NOT_CONFIGURED); terms of both venues recorded as UNVERIFIED pending owner confirmation; quotes are polled every 60 s, so data is DELAYED by design; Coinbase returned 350 candles although its docs state 300 max — the parser accepts any count.
- **Next:** S3 ledger, authority and deterministic risk.

## S3 — Ledger, authority and deterministic risk (2026-10-11)

- **Implemented:** migration `0003_trading_core` (single-row `authority_state` seeded RESEARCH_ONLY and protected from INSERT/DELETE; immutable `authority_transitions`; accounts constrained to PAPER unless a live adapter is used; versioned immutable `risk_policies` with exactly one active version; immutable `risk_check_runs`; double-entry `ledger_txns`/`ledger_entries` with a **deferred constraint trigger rejecting any per-asset imbalance at commit** and immutability triggers; positions; equity snapshots; immutable `golive_gates`; `live_activations`). Modules: `trading.modes` (mode → permitted operations), `trading.authority` (persisted state, audited transitions; permissive changes need ADMINISTER_SYSTEM + fresh MFA + reason; live modes refused with the full list of unmet preconditions — no live adapter registered, gates A–I not passed, no live account; strategy authority disabled; EMERGENCY_STOP cannot be entered or left via a mode change; leaving PROTECTION_ONLY requires reconciled accounts; `COSMIC_MAX_AUTHORITY_MODE` cap enforced at startup with a CRITICAL incident), `trading.policy` (typed policy with `extra="forbid"`, no leverage/shorting/market orders, documented limit semantics, hash-verified on load, MFA-gated non-retroactive changes), `trading.precision` (conservative lot/tick rounding with tolerance), `trading.sizing` (risk budget with caps and uncertainty haircut), `trading.ledger`, `trading.accounts` (paper accounts with simulated capital), `trading.risk` (immutable context; checks R01–R17; any non-PASS blocks; a crashing check fails closed).
- **Tests run:** `make check` → **228 passed**, 1 deselected (live data); mypy strict clean; secret scan passed. New: 74 unit (every check's FAIL/UNKNOWN path, reduce-only path, broken-check fail-closed, stale-data blocking, live account rejected in paper/research modes, Hypothesis: limit monotonicity, rounding never increases exposure, sizing never exceeds caps) and 17 integration (authority rules, cap breach, table protections, policy workflow and immutability, tamper detection, ledger idempotency and DB-enforced balance).
- **Bugs found and fixed:** an invalid stored policy raised a raw validation error instead of failing closed (now a clear `ConflictError`); a missing authority row would have raised an unhandled error (now fails closed); test harness truncation removed the seeded authority row (harness re-seeds).
- **Next:** S4/S5 orders, paper broker, execution authority, reconciliation, kill switch, proposals and approvals.

## S4/S5 (engine) — Orders, paper venue, execution, reconciliation, kill switch, proposals, approvals (2026-10-11)

- **Implemented:** migration `0004_execution` (trade intents with material terms frozen by trigger; immutable approval packets; single-use approval challenges; approvals with one decision per packet; orders with the 14-state machine, one entry per intent, reduce-only enforced for exits; immutable order events and fills; reconciliation runs/items; recovery checklists; paper venue books and fault table; post-trade reviews; Telegram link codes). Modules: `trading.orders` (legal transitions only; broker-grounded states require BROKER/RECONCILE evidence; optimistic concurrency), `trading.brokers` (adapter contract; PaperBroker as an independent simulated venue with holds, fees/slippage ASSUMPTION, partial fills, stop-limit orders that may not fill in a gap, no network access, fault injection only in TEST/LOCAL; registry fails closed for LIVE accounts), `trading.proposals` (typed `Candidate` → precision normalisation → pre-approval risk → immutable intent + hashed packet, or NON_EXECUTABLE with reasons; re-proposal creates packet v+1), `trading.approvals` (challenge → decide with packet-hash binding, user/channel binding, fresh MFA or TOTP-in-chat, nonce, expiry, policy-version check; revoke; expiry sweep), `trading.execution` (single advisory-lock leader; Tx A consume + revalidate + AUTHORIZED; Tx B authority re-read + SUBMITTING persisted before sending; request rebuilt only from the order row; ambiguous → UNKNOWN_ORDER_STATE + reconciliation, never resubmitted; venue sync with idempotent fills; approved management: stop-loss sized to filled quantity, monitored take-profit that cancels the stop first, cancel-unfilled-after; protective failures → DEGRADED + CRITICAL incident + bounded identical retries; restart recovery), `trading.fills` (average-cost positions, realised P&L after fees, ledger posting, overfill/oversell anomalies never auto-repaired), `trading.reconcile` (orders → fills → positions → balances → ledger → account state), `trading.killswitch` (atomic stop, revoke approvals, block queued intents, cancel unfilled entries, keep stops and positions, recovery checklist, resume ≤ pre-stop mode), `trading.review` (planned vs actual, never rewriting the original packet), worker jobs.
- **Tests run:** `make check` → **267 passed**, 1 deselected; mypy strict clean; secret scan passed. New: 25 acceptance tests (V6 §1831 AT-01…AT-12, AT-14, AT-15, AT-17, AT-18, AT-20 and an end-to-end paper trade), 6 management tests, 8 broker-contract tests.
- **Bugs found and fixed:** UNKNOWN orders resolved from venue evidence did not record the venue order ID; paper fault counters were rolled back when the fault raised inside the same transaction; SQLAlchemy 2.1 `ARRAY.any()` deprecation; a `mypy`-caught attribute error in reconciliation; recovery checklist timestamp comparison.
- **Behaviour confirmed by tests (not bugs):** the duplicate guard refuses a second proposal for an instrument/side while one awaits re-approval; re-proposal is refused when the last reconciliation is older than 900 s; a take-profit 0.36 % above entry is a net loss with 60 bps fees per side and the review labels it LOSS.
- **Known limitations:** AT-13/AT-16 belong to S6; AT-19 to S9; take-profit is monitored (not resting) because the paper venue has no OCO — documented in packets; `execution.py` is large (≈1,150 lines after formatting) and is a candidate for splitting the management code out.
- **Next:** approval UI (portal + JSON API), Telegram handlers.

## S5 (interfaces) — Approval UI, JSON API, Telegram (2026-10-11)

- **Implemented:** read models (`trading.queries`); portal pages `/approvals` (queue + recent), `/approvals/{id}` (plain language first, order and costs, approved management only, prohibited actions, evidence/counterarguments/scenarios, every risk-check run, decisions with revoke, orders, audit link), confirmation screen with the exact terms and an authenticator-code field when step-up is stale, `/proposals/new` (manual long limit proposals with automatic quote evidence), `/portfolio` (cash, equity or UNKNOWN, positions with mark labels and protection state, limits, reconciliation items, post-trade reviews, all SIMULATED-labelled), `/orders` and `/orders/{id}` (broker-confirmed vs local transitions, fills, cancel request), `/ops` (emergency stop, mode change with unmet live preconditions listed, recovery checklist and resume, incidents with acknowledge, worker heartbeats and queue, paper account opening, integration status), `/audit`, `/settings/telegram` (one-time link codes); authority banner on every page; broker-touching follow-ups (`trading.emergency_followup`, `trading.cancel_order`, `trading.reconcile`) queued for the worker. JSON API `/api/v1` (status, sources, instruments, intents, challenge, decision, orders, accounts, reconcile, emergency stop). Telegram: pure handlers (private chats only, immutable user-ID binding, one-time linking, proposal message with REVIEW DETAILS / EVIDENCE / COUNTERARGUMENTS / RISK / PORTFOLIO IMPACT / APPROVE EXACT TRADE / REJECT / REQUEST CHANGES, confirmation screen, TOTP in chat by default, callback data = opaque references, free text never approves, `/stop` with confirmation), aiogram 3.31 long-polling runner with outbox delivery that records failures; `cosmic bot` refuses to start without a token.
- **Tests run:** `make check` → **284 passed**, 1 deselected; mypy strict clean. New: 8 portal E2E tests (full approval flow over HTTP, XSS escaping, missing CSRF, stale MFA without code, viewer refusals, login required), 7 Telegram handler tests, 1 API security test, 1 kill-switch regression test.
- **Bug found and fixed:** the emergency stop failed (HTTP 409) when no valid risk policy existed because it read the emergency policy fail-closed; the stop now falls back to the safe default emergency behaviour and always engages.
- **Not verified:** live Telegram delivery (B-05) — only handler logic is tested; aiogram wiring is exercised up to the token check.
- **Dependency note:** adding aiogram 3.31.0 resolved pydantic to 2.13.5 / pydantic-core 2.46.5; the full suite passes on these versions.

## S6 — Free-only AI gateway (2026-10-11)

- **Implemented:** `config/ai_registry.toml` (Groq, Gemini, OpenRouter `:free`, Cerebras disabled as a credit trial; endpoints, policies and key *setting names* only); migration `0005_ai` (attestations, model approvals, provider state, immutable usage ledger, cache); `cosmic.ai` (registry loader; eligibility policy — policy must be FREE_CLOUD_AI_ONLY, provider enabled, key present, owner attestation valid where required, model approved/unexpired/pattern-allowed, OpenRouter live price check = 0 for every field within 24 h, breaker closed, not rate limited, not disabled; OpenAI-compatible adapter with explicit billing-signal detection (402, credit/billing errors, reported cost > 0), 429 + `retry-after`, timeouts, OpenRouter `provider.allow_fallbacks=false` and no model fallback list; prompt safety — fixed system policy, `<untrusted_data>` wrapping, data classification (CONFIDENTIAL never sent), outbound scanner for secrets/credential patterns/e-mail/forbidden identifiers; per-provider breaker; local per-minute/per-day budgets; usage ledger; cache with explicit expiry; schema-validated output with `extra="forbid"`; WAITING_FOR_AI degradation); MFA-gated attestation and model approval; `/ai` provider health page (status, reasons, attestation, approved models, last success/error, breaker, rate-limit window, calls and reported cost today — never keys); worker price-check job.
- **Tests run:** `make check` → **303 passed**, 1 deselected; mypy strict clean. New: 15 gateway tests against a fake provider through the real adapter (no live AI calls), AT-13 (AI outage leaves risk, execution and reconciliation working), AT-16 (13 pages, API, headers, static assets and logs contain no configured credential).
- **Bugs found and fixed:** provider-state upsert passed a column twice; the gateway reported WAITING_FOR_AI instead of INVALID_OUTPUT when every route returned schema-invalid output; log sinks were bound to the stdout object at configuration time (now resolved at write time).
- **Not verified:** any live AI provider call (B-04: no keys). Gemini free-tier terms and quotas remain unverified (official pages unreachable on 2026-10-10).

## S7 — Workforce and research (2026-10-11)

- **Implemented:** migration `0006_research` (strategies, hash-addressed versions, stored backtests with data vintage, missions, opportunities + immutable transitions, tasks with dependencies/depth/attempts/budgets, attempts, reviews, immutable artefacts, escalations; FKs from claims and trade intents). `market.regime` (documented trend × volatility method), `research.strategy` (`trend_sma_atr`, completed bars only, explicitly not a claim of edge), `research.backtest` (decide at close, fill next open with half spread + slippage + fees, stop before take-profit intrabar, gaps fill at the open, Sharpe/drawdown/exposure/fees, weak-sample notes, disclaimer; walk-forward with chosen params per window and disclosed trial count), `research.registry`, `research.events` (return/volume spikes, regime change, strategy signal, cross-venue divergence; idempotent per bar), `research.opportunities` (lifecycle with legal-path sync from trade intents), `evidence.service` (claims; numbers verified by code; AI statements INFERRED or CONTRADICTED), historical backfill via paged keyless candle requests, `workforce` (7 declared roles with every §7 attribute, allowlisted toolboxes, task engine with verification before DONE, reopen/escalate, WAITING_FOR_AI without consuming attempts, expiry), research mission pipeline (data check → quant → AI narrative for entry signals only → typed proposal by the research worker → review → change-first report → archive), worker jobs, portal `/research`, `/research/{id}`, `/strategies`.
- **Tests run:** `make check` → **324 passed**, 1 deselected; mypy strict clean. New: 8 research unit tests (no look-ahead under future perturbation, costs, gap fills, reproducibility, walk-forward honesty, opportunity paths), 7 role/toolbox tests, 3 mission end-to-end tests — **WF-08: one complete mission from detection to an archived report and an operator-approved paper execution**, AI-22 (no AI → mission waits, expires, NO_TRADE, no proposal), deterministic event-review missions without AI.
- **Bugs found and fixed:** analytics content hashed with the strict financial hash (rejects floats) — added a lenient hash for analytics only; opportunity sync could not move from APPROVAL_REQUIRED to EXECUTION when it ran after execution — sync now walks the legal path; the detector was placed in `market` (layering violation) — moved to `research` and a layering rule was added to the architecture test.
- **Known limitations:** the strategy is a transparent pipeline exerciser with no claimed edge; walk-forward needs ≥ 240 hourly bars (backfill job provides 90 days); performance decomposition (QNT-11) is partial (slippage vs limit, costs, outcome); no live AI narrative has been produced (B-04).

## S8 — Portal completion (2026-10-11)

- **Implemented:** command center at `/` (`reporting.command_center`: authority banner, what needs attention, recent market events, source freshness labels, pending approvals, claim counts and open contradictions, open incidents, task counts, overdue tasks and escalations, system health (database, schema revision, worker heartbeat age, AI), per-account reconciliation state, orders in UNKNOWN state, degraded protection, open recovery checklist); go-live readiness (`trading.golive`: gates A–I from the directive, append-only evidence records, gates that depend on a VM or a broker are reported BLOCKED whatever evidence is recorded, gate G compares the paper period against 20 trades / 30 days); `/golive` page and `cosmic golive status|record` CLI; risk policy page `/settings/risk` (limit semantics table, editable limits through the existing MFA-gated `change_policy`, fail-closed notice when the policy is missing, invalid or tampered); Prometheus text metrics at `/internal/metrics` (bearer token, answers 404 without it so the route is not discoverable); navigation entries for go-live and risk policy.
- **Tests run:** `make check` → **331 passed**, 1 deselected; ruff, mypy strict (154 files) and the secret scan clean. New: `tests/e2e/test_command_center.py` (every POST route in the app is reachable from a form or documented as API-only — "every button real"; command center headings; go-live never passes without a broker; metrics need the token; a risk change with stale MFA is refused) and `tests/security/test_authz_matrix.py` (every GET route × anonymous/viewer/analyst; an unclassified new route fails the test; 12 privileged POSTs refused for a viewer).
- **Bugs found and fixed:** the route inventory missed routes wrapped by FastAPI's included-router objects — the inventory now flattens them; `GET /settings/risk` returned 409 when no policy existed — it now renders the fail-closed state.
- **Known limitations:** no JavaScript, so tables do not sort or refresh live (reload to refresh); accessibility checked by structure (labels, headings, `role="alert"`, keyboard-only forms) and not with an automated screen-reader audit; metrics are not scraped by anything until a monitoring stack exists (none in the VM budget).

## S9 — Deployment artefacts, verified locally (2026-10-11)

- **Implemented:** `deploy/Dockerfile` (multi-stage, base images pinned by digest, `uv.lock --frozen --no-dev`, app + standard-library bytecode precompiled, UID 10001, 380 MB); `deploy/compose.prod.yaml` (postgres, one-shot migrate, web, worker, bot/`telegram`, cloudflared/`tunnel` with `--token-file`, backup/`backup`; no published ports; `data`/`edge` internal networks, web without internet access; read-only root, `cap_drop: ALL`, `no-new-privileges`, memory/CPU/PID limits, healthchecks, json-file rotation; secrets as files mounted per service); backup image (same `postgres:17-alpine` digest + `age`), `cosmic-backup` (pg_dump streamed into age; manifest with pre-dump audit head and row counts; `.sha256`), `restore-drill.sh` (`--network none`, identity streamed into a tmpfs, 10 checks), `restore.sh` (into a new database only), retention `cosmic backup prune` (14/8/6 + newest 3); Cloudflare tunnel template (catch-all 404, `/internal/*` 404), `dns-inventory.sh` (GET-only), `verify-public.sh`; `proxmox/create-vm.sh` (dry run by default); `vm/cloud-init.yaml`, `vm/bootstrap.sh` (dry run by default; Docker from the Ubuntu archive, SSH/journald/unattended-upgrades hardening, ufw with mandatory management CIDR, secrets generated only if absent), systemd units, `vm/verify.sh`; `deploy/deploy.sh` (preflight, build at a clean tag, pre-deploy backup, `up --wait`, smoke tests, printed rollback); break-glass `compose.admin-access.yaml`; `cosmic probe web|worker`, trusted-proxy setting, optional secret files, heartbeat file for healthchecks; local verification harness (`scripts/local-verify.sh`, `verify-compose.sh`, `local_verify.py`, `compose.local-verify.yaml`).
- **Verified (real runs, this workstation):** final production-like run `var/verify/run-20261011T175651Z.log` — 13/13 steps PASS: portal flow 22/22 on real delayed Coinbase + Bitstamp data (login, TOTP enrolment, paper account, mode change, proposal passing R01–R17, bound approval, FILLED by the paper venue, protective stop, approval re-use refused, kill switch with stop kept, R01 refusal, stop-triggered reconciliation, checklist resume); AT-20 at container level 17/17 (worker SIGKILLed 4× at 0.24–2.97 s after approval; no uncertain or duplicate orders); `docker compose restart` 10/10; audit chain ok (67 events); encrypted backup with no plaintext; restore drill PASS (negative cases — tampered dump, wrong key — fail as they must). `cloudflared tunnel ingress validate` OK. Resources idle: postgres 75 MiB, worker 68 MiB, web 75 MiB (doc 12 §7). `make check` → **356 passed**, 1 deselected; ruff (now including `scripts/`), mypy strict (156 files) and the secret scan clean. All shell scripts pass shellcheck.
- **Not verified (blocked):** nothing ran on a Proxmox host or a Cloudflare zone. `create-vm.sh`, `bootstrap.sh`, `deploy.sh` and `vm/verify.sh` were dry-run and linted only (B-02, B-03). VM reboot (AT-20 VM part) BLOCKED.
- **Bugs found and fixed:** periodic jobs of the same kind were deduplicated by kind only, so Bitstamp was never polled and R10 failed closed in every real deployment (regression test proves the old key fails); every non-dev process demanded the database owner password (least privilege restored); uvicorn trusted `X-Forwarded-For` from anyone outside dev (now only cloudflared's address; `*` rejected); `GET /settings/risk` → 409 without a policy (S8 carry-over); the restore drill used `su-exec`, absent from the current postgres image; retention could prune a same-day pre-deploy backup; healthchecks cost ~1.6 s CPU each (stdlib recompiled on every start) — idle web CPU 25.6 % → 0.2 %; a loopback probe in `cli.py` violated the HTTP-client layering rule (moved to an explicitly allowlisted `ops/probe.py`). New tests: deployment-artefact guards (digests, no ports, segmentation, hardening, per-service secrets, tunnel template), disk thresholds and background shedding under pressure, heartbeat file, probe, resume refused until a post-stop reconciliation, backup retention, config (wildcard proxy, optional secrets, owner password).
- **Known limitations:** the random kills never landed inside the millisecond SUBMITTING window (covered deterministically in-process); measurements are from a 4-CPU workstation, not the VM; web holds AI/Telegram secret files only to show "configured" (threat model residual risk 6).

## S10 — Hardening and handover (2026-10-11 → 2026-10-12)

- **IMPLEMENTED:** worker watchdog (exits when the loop makes no progress for 120 s so the restart policy recovers a hung worker; Docker only marks unhealthy containers); safety-relevant `assert`s replaced by explicit fail-closed checks (execution approval, R08 inputs, approval decision session, paper capital, AI key); `make scan` audits the exact locked dependency set; bandit configured with reviewed per-line exceptions; `make lint` fails on TODO/FIXME/XXX/HACK and now covers `scripts/`; `restore.sh` verifies checksums inside the backup container. New tests: order state-machine table (9), schema rules (no float columns, UTC timestamps, NUMERIC(38,18) money), approval-packet completeness (every V6 §1807 term, warning and cost texts, stored hash), audit trail of a whole trade, cancel/fill race, resume refused until a post-stop reconciliation, watchdog, backtest disclaimer. Docs: traceability matrix fully re-assessed (301 rows: 237 IMPLEMENTED, 26 DONE, 25 PARTIAL, 4 DEFERRED, 8 BLOCKED, 1 N/A, none PLANNED), invariant table (doc 08 §12) and failure-mode matrix (doc 13 §3) rewritten with test names that exist (checked mechanically), API contract marks built vs portal-only endpoints, user guide (doc 18), executive summary, session report.
- **NOT_IMPLEMENTED:** live broker adapters and anything live (B-06/B-07); VM, tunnel, DNS (B-02/B-03); real AI and Telegram use (B-04/B-05); JSON endpoints for bars, go-live, AI providers, tasks, audit (portal only); free-text search; performance decomposition beyond costs/slippage (QNT-11); strategy mandates (disabled by design); automated accessibility audit; splitting the 1,150-line `execution.py`.
- **TESTS_RUN / TEST_RESULTS:** `make check` → **373 passed**, 1 deselected (`live_data`), ruff + TODO scan + mypy strict (156 files) + secret scan (347 files) clean. `make scan` → pip-audit: no known vulnerabilities in 88 locked packages (47 runtime); bandit: no issues. Local production-like run on the S10 image (`var/verify/run-20261011T225224Z.log`) → 13/13 steps PASS (portal 22/22, chaos 17/17, restart 10/10, audit 68 events, drill PASS). `deploy.sh --build` run for real twice against a simulated `/srv/cosmic` (first deploy; idempotent second deploy with pre-deploy backup); `restore.sh` into a new database, stack switched to it and healthy; same-name restore refused; the simulated project was then removed.
- **Bugs found and fixed:** `restore.sh` needed host read access to backup files owned by UID 10001 (checksum check moved into the container); doc 14 claimed a heartbeat incident that did not exist (doc corrected and a watchdog added); docs cited test names that did not exist.
- **KNOWN_LIMITATIONS:** see the session report §9; the random SIGKILLs never hit the millisecond SUBMITTING window (covered in-process); measurements are from a 4-CPU workstation.
- **RESOURCE_USAGE:** idle after the run: postgres 75 MiB / 2.6 % CPU, worker 63 MiB / 9.0 %, web 74 MiB / 0.2 %; DB 12 MB; images 380 MB + 443 MB.
- **SECURITY_IMPACT:** least-privilege secrets per container, web without internet, trusted-proxy fix, no safety logic depending on `assert`, dependency and static scans clean.
- **BACKUP/ROLLBACK_STATUS:** encrypted backups, retention and restore drill verified locally; DR restore into a new database verified; rollback commands printed by `deploy.sh`; none of it run on the VM (B-02).
- **NEXT_ACCEPTANCE_GATE:** owner access to a Proxmox host (B-02) → `create-vm.sh --apply`, `bootstrap.sh --apply`, `deploy.sh --build`, `vm/verify.sh`, VM reboot test; then domain + tunnel (B-03) → `dns-inventory.sh`, tunnel, Access, `verify-public.sh`.

## S11 — Post-handover engineering backlog (2026-10-12)

- **IMPLEMENTED:** `trading/execution` split into a package (common, base, entry, sync, protection, recovery, service; all 25 methods AST-identical to the original); JSON API for market bars, go-live readiness, AI providers, missions, audit, mode change, approval revoke and search (doc 10 now fully built); actionable alerts — `Alert` with six required answers, `raise_incident` requires one (15 call sites), notification only on open/escalation, portal **Alerts** inbox with unread count (alerts were previously only delivered to Telegram, which is not configured); post-trade attribution (market move, delay, entry/exit execution, fees — parts sum exactly; sizing vs plan) shown on the trade page; AI tier-routing test, fair and marked context budget (the old budget could silently drop items), decision-value ordering of waiting AI work; structural accessibility checks over every page; full-text research search (migration 0007, GIN indexes); per-process secrets — the web process no longer holds AI keys or the Telegram token (worker and bot report presence in heartbeats).
- **NOT_IMPLEMENTED:** everything that needs the VM, domain, AI keys, Telegram token or a broker (B-02…B-07); screen-reader audit; Mini App (ADR-021).
- **TESTS_RUN / TEST_RESULTS:** `make check` → **396 passed**, 1 deselected; ruff, TODO gate, mypy strict (171 files), secret scan clean; bandit clean. Production-like run on the S11 image (`var/verify/run-20261012T063130Z.log`) → 13/13 PASS (portal 22/22, chaos 17/17, restart 10/10, audit 68 events, drill PASS); in that stack web holds only `db_app_password`, `metrics_token`, `secret_key`.
- **Bugs found and fixed:** portal notifications were never displayed anywhere; AI context truncation could drop whole items silently; per-attempt duplicate alerts for protective-order failures; doc 10 listed endpoints that did not exist (built now).
- **KNOWN_LIMITATIONS:** Alembic cannot compare the expression indexes, so the drift test ignores those three by name and a separate test asserts they exist; attribution needs point-in-time quotes ≤ 5 min old, otherwise those parts are reported unknown.
- **RESOURCE_USAGE:** idle: postgres 71 MiB / 1.0 % CPU, worker 63 MiB / 2.4 %, web 74 MiB / 0.2 %.
- **SECURITY_IMPACT:** smaller secret footprint on the internet-facing process; search queries are bound parameters only; alerts never include secrets (payloads redacted).
- **BACKUP/ROLLBACK_STATUS:** migration 0007 has a tested downgrade; backup/drill re-verified on the new schema.
- **NEXT_ACCEPTANCE_GATE:** unchanged — owner access to a Proxmox host (B-02), then domain/tunnel (B-03).

## Staging deployment — test server and Cloudflare (2026-10-12)

- **Done:** repository pushed to GitHub (`main`, public; full history scanned first — no secrets). Shared test server: stack deployed with `deploy.sh` under `~/apps/cosmictrading` (no sudo, no host changes, no published ports); images built locally and streamed; secrets generated on the server; owner account `nebyu` created (password only in the owner's secret files; MFA enrolled by the owner at first sign-in). Cloudflare (`cosmictrading.bond`): read-only inventory (0 records) → tunnels `cosmictrading-staging` and `cosmictrading-ssh`, proxied CNAMEs `staging.` and `ssh.`, Access apps (owner e-mail + service token). Remote SSH via a key-only jump container (no shell; forwards only to the host's sshd); the host's sshd is never exposed. HTTPS enforced (edge 301 and app 308). Daily encrypted backups via user crontab; restore drill on the server: DRILL PASS.
- **Verified:** `verify-public.sh staging.cosmictrading.bond` → ALL CHECKS PASSED; `vm/verify.sh` (shared-host mode) → 12/12; remote SSH through Cloudflare reaches the server with the host key pinned from the LAN; `make check` → **398 passed**.
- **Found and fixed:** **every real browser login was rejected** (`cross-origin request rejected`): `Referrer-Policy: no-referrer` makes browsers send `Origin: null` on form posts, which the origin check refuses; tests and the verification driver never sent an `Origin` header, so they could not see it. Now `same-origin`, with a browser-realistic regression test; the owner then signed in and enrolled MFA (2026-10-12 09:04/09:07). Bot OOM at 256 MiB (measured peak 241 MiB → limit 384 MiB); plain HTTP was served; `restore.sh`-style permission assumptions adapted for a host without sudo (closed secrets directory).
- **Not done / owner actions:** dedicated production VM (B-02) and `app.cosmictrading.bond`; disable group joins for the bot; keep the backup age key off the server; credential housekeeping (given to the owner directly).

- **Follow-up (2026-10-12, later):** CI on GitHub Actions — first run found that the runner's shellcheck (0.9) rejected the BusyBox directive and four `A && B || C` lines; scripts made portable (verified with 0.11 and 0.9), CI green (check + images). Admin Telegram link code (`cosmic admin telegram-link-code`, audited as CLI); the bot leaves groups automatically (replaces BotFather's group setting). Stale heartbeat pruning; guarded `scripts/deploy-staging.sh`; verification driver sends browser `Origin`. Server account password rotated (key logins unaffected). `make check` → 401 passed.
- **Follow-up (2026-10-12, owner feedback on staging):** two portal defects found by the owner in a real browser. (1) Privileged forms failed with `step_up_required` and no way to enter a code once the sign-in code was more than 10 minutes old; they now show an authenticator-code field inline in that case (`web.deps.authorize_form`), and the error page links to `/mfa?next=` the form. (2) The mode control was a dropdown offering two live modes this build always refuses; it is now a labelled choice of only the reachable modes (`trading.modes.selectable_targets`: not the current mode, not EMERGENCY_STOP, nothing above `COSMIC_MAX_AUTHORITY_MODE`, no live mode while live preconditions are unmet), each with a one-line description, PAPER_TRADING preselected when offered; the rest are listed as not offered. Server-side checks are unchanged.
- **Follow-up (2026-10-12):** after that release the owner saw the new form with the old stylesheet: the origin sent no `Cache-Control` for `/static/`, so Cloudflare applied its default 4-hour TTL at the edge and in the browser (`cf-cache-status: HIT`, `max-age=14400`). The stylesheet URL now carries a content hash (`?v=`, `web.state.static_version`); that exact URL is `immutable`, any other static request is `no-cache`. Rendered and checked in headless Chromium. `make check` → 405 passed.
- **First owner paper trade on staging (2026-10-12):** `T-20261012-08Q2`, BUY 0.0024196 BTC limit 82,657.98 on `paper-main`. R01–R17 passed at proposal; approved in the portal 15:23:28 UTC; entry order created 15:23:31 and FILLED at the limit (ask 82,624.42 + 5 bps assumed slippage exceeded it), fee 1.20 USD (ASSUMPTION), all SIMULATED; protective STOP_LIMIT (80,058.14 / 79,893.07) acknowledged 15:23:46, position protection ACTIVE; next reconciliation PASSED; audit chain verifies (44 events); four Telegram messages delivered in 3–5 s. Display fix from the owner's screenshot: the plain-language sentence carried the instrument grid's 18 decimal places and money estimates were unrounded; new packets use the canonical form, the portal and Telegram show money to the cent and times as UTC. Existing packets are immutable and keep their text. `make check` → 407 passed.

## S12 — Binance, gold, the 100-rule strategy library, approved exits (2026-10-14)

- **Owner request:** "consider Exness and Binance … gold and crypto BTC … top 100 strategies … make and apply them".
- **IMPLEMENTED:**
  - **Binance data:** keyless public market data (BTC/USDT, PAXG/USDT, XAUT/USDT, system status). Gold is held as spot tokens (PAXG/XAUT); Coinbase PAXG/USD is used for paper trading; daily bars for every listing (ADR-023).
  - **Strategy library:** 100 published rules with sources, one honest engine, development and validation periods, timing Sharpe against an exposure-matched holding, and the deflated Sharpe ratio over all trials. QUALIFIED/WATCH/REJECTED tiers; a look-ahead proof for every rule; append-only results (migration 0008); portal pages and the owner's paper switch (ADMINISTER_SYSTEM + fresh MFA, at most 10, WATCH needs an acknowledgement); CLI `cosmic research history|library|library-status` (ADR-024).
  - **Applying rules:** switched-on rules turn daily entries and exits into ordinary proposals. Entries carry a labelled 3×ATR disaster stop.
  - **Approved exits:** a sell names the entries it closes (bound into the packet hash). Execution cancels their protective orders first, sends a reduce-only IOC exit only once those are confirmed gone, and never sends it if a stop filled meanwhile. An unfilled exit expires and the stop is placed again. Two exits can't oversell one entry. A plain sell beside open entries is refused. A flat rule re-proposes its exit daily while it still holds something.
  - **Binance Spot Test Network adapter** (unregistered, ADR-025): built from 80 independently verified documentation facts (`docs/integrations/binance-spot-api.md`), tested against an emulator; `make sandbox-binance` is ready for the owner's testnet keys.
- **NOT DONE / BLOCKED:**
  - Exness: its API documentation is not reachable for automated review, and its gold is a leveraged CFD (B-06).
  - The real Binance testnet run needs keys (B-07).
  - Live trading stays disabled.
- **Results (real data):** Library evaluated on Bitstamp BTC/USD daily 2011-08-18…2026-10-13 (5,536 bars) and Binance PAXG/USDT daily 2020-08-28…2026-10-13 (2,238 bars), 195 trials per scenario.
  - **Coinbase-like costs (0.66% one way):** BTC 0 QUALIFIED / 21 WATCH / 74 REJECTED.
  - **Binance-like costs (0.16%):** the best deflated probability is about 0.3 (Keltner breakout). No rule reaches 0.95.
  - Timing edges were consistently weaker in the validation years than in the development years. Gold results are statistically weaker (about six years, mostly one rising market).
- **Staging (2026-10-14, `19d356a`):**
  - The first real backfill exposed a defect: validation rejected bars older than 400 days, a limit meant for intraday data, so 27,509 Bitstamp and 4,842 Binance daily bars arrived but only 200 were stored. The limit is now per timeframe, with a regression test.
  - After the fix, `cosmic research history` stored BTC 5,537 and PAXG 2,239 daily bars in 41 s.
  - `cosmic research library` evaluated both scenarios in 34 s, recorded as runs at 18:17 UTC, code `41b0989251dbdea0`, 195 trials each.
  - **BTC, Binance-like costs:** 40 WATCH / 55 REJECTED. Best deflated probabilities: Keltner breakout 0.33, halving cycle 0.31 (only 4 trades), OBV trend 0.28, Ichimoku 0.26.
  - **BTC, Coinbase-like costs:** 21 WATCH / 74 REJECTED, best 0.04.
  - **Gold:** 13 WATCH / 82 REJECTED (Binance-like) and 6 / 89 (Coinbase-like); best golden cross 50/200 at 0.13 (3 trades).
  - **Rotation:** 2 WATCH / 3 REJECTED.
  - No rule qualifies. No rule is switched on: that is the owner's decision. Audit chain verifies (718 events).
- **Follow-up (2026-10-15):**
  - **Close a position from the portal:** Portfolio → *Propose closing* builds an approved exit (IOC, closes the position's entries, replaces their stops). It is tested end to end through the portal: buy, fill, stop, close, approve, stop cancelled, sale filled, flat, and a second close refused.
  - **Testnet check from the worker container:** `cosmic broker binance-testnet-check` runs the broker contract on the real Spot Test Network, with keys as JSON on stdin; it is tested against the emulator.
  - **Testnet key pair:** an Ed25519 pair was generated on the server; the private key stays on the server.
  - **Bot healthcheck:** the Telegram bot touches a heartbeat file after each delivery-loop heartbeat, and compose checks it, like the worker.
  - `make check` → 571 passed.

## S13 — Binance test-network (SANDBOX) accounts (2026-10-15)

- **Owner request:** "try now and continue" (retry the Binance testnet key, keep building).
- **Testnet key, retried after the server came back (about 06:58 UTC):** still `-2015` (Invalid API-key, IP, or permissions) on the Spot Test Network. The three key files on the server are all different (checked by comparison, no values printed). Owner step in B-07.
- **Server outage:** the staging server was unreachable from about 05:00 EAT until it rebooted at about 06:46 UTC (09:46 EAT). All seven containers then came back healthy on their own; Docker took about 100 s to start under heavy disk I/O.
- **IMPLEMENTED (ADR-026):**
  - **Stage 1 (`e2c6d11`):** the test network as its own source and venue (`BINANCE_TESTNET`, kind SIMULATED) with BTC/USDT and PAXG/USDT quotes, so orders are checked against the book they meet. Research uses EXCHANGE venues only. Verified on staging: quotes every 30 s; tick 0.01, lot 0.00001 BTC / 0.0001 PAXG, minimum 5 USDT.
  - **Account kind SANDBOX** (migration 0009, tested downgrade that refuses while SANDBOX accounts or UNMANAGED entries exist): only with the `binance_spot_testnet` adapter; one active per adapter. It is authorised and labelled like paper (`SIMULATED_ACCOUNT_KINDS`); live modes are unchanged and unreachable.
  - **Keys:** two optional Docker secrets for the worker only. The adapter is built only when both are present and the private key is a valid Ed25519 key (otherwise SANDBOX fails closed and paper is unaffected). The worker reports presence in its heartbeat.
  - **Contract runs:** `cosmic broker binance-testnet-check [--from-secrets]` records every run (append-only `broker_contract_runs`, audited) with a 16-hex key fingerprint.
  - **Opening:** from the portal (Operations → Binance test network) through a worker job. It needs a PASSED run for the worker's key, reads the venue's balances, and books the chosen USDT as capital and the rest of USDT/BTC/PAXG as `UNMANAGED`, then reconciles. A refusal is an incident.
  - **Fees in the received asset:** positions are net of base-asset fees with cost basis quantity × price; a third-asset fee (BNB) is booked against UNMANAGED, left out of P&L and reported.
  - **Dust:** stops, exits, open entries and the closed-trade review use whole lot steps only.
  - **Reconciliation:** a SANDBOX account compares only its tracked assets, as CASH/POSITION + UNMANAGED, and ignores venue trades from before its opening (5-minute clock margin).
  - **Web and bot:** they cannot reach the test network. An emergency stop from Telegram queues the cancels and reconciliation for the worker in the same transaction; reconcile-all skips accounts a process cannot reach.
- **Bugs found and fixed:**
  - **Binance trade lookup used our clock.** The adapter searched `myTrades` in a window ending at our clock's "now". A fill stamped at or after that instant, or before the order's local creation time when the venue clock is behind, was missed, and the order stuck as "venue FILLED, no fills". The window now uses venue time with a one-minute margin (regression test for venue clocks 20 s ahead, equal and 20 s behind).
  - **Telegram emergency stop and test-network orders.** The bot process would have crashed cancelling them, because it holds no keys. The cancels are now queued for the worker.
  - **Status table out of date.** The deployment table at the top of this file predated staging; it is updated.
- **NOT DONE / BLOCKED:**
  - The real contract run needs a key Binance accepts (B-07).
  - Retiring a test-network account after Binance's monthly reset is not built: the account stays blocked, which is safe.
  - The strategy library and AI missions still propose for paper only.
- **Staging (`4812ea0`, 07:48 UTC):** deployed with `scripts/deploy-staging.sh`:
  - Backup taken first; audit chain verified (900 events); live trading DISABLED; server checks VERIFIED; public readiness OK.
  - Migration `0009_sandbox_accounts` applied. The worker reports `binance_testnet: CONFIGURED` (presence only); web and bot hold no testnet keys. Worker logs show no errors.
  - `cosmic broker binance-testnet-check --from-secrets`: ping PASS, then `-2015`, CONTRACT FAILED. It is recorded as contract run `01a13e88-df30-7382-afbc-e32a839fd2ea` with key fingerprint `d0f350dca9add161`, so no test-network account can be opened (gate verified on the real system).
- **The owner's new key (08:49 UTC):**
  - Saved with the hidden-input command: 64 characters, different from both earlier keys, fingerprint `40ad1a36e4c400a2`.
  - After a worker restart the contract check **PASSED** on the real Spot Test Network: all 8 steps, run `01a13ec1-8887-732b-a76d-7dc5c713e8db` at 08:50:33 UTC.
  - Test account balances (virtual): 10,000 USDT, 1 BTC, 1 PAXG, plus 514 other assets; no open orders.
- **Owner decision (A):** a value the owner provided (probably the real account's Secret Key) is kept outside the application. It is read by no code and mounted nowhere, and it was not tested against the real account.
- **Found and fixed after that:** the risk-policy page edited only numeric limits, so the owner could not allow the test-network instrument, venue or currency. It now edits the three lists (pre-filled). Added entries must exist and be tradable, with their venue and currency allowed (`check_new_allowlist_entries`); a clear refusal names what is missing.
- **TESTS:** `make check` → **589 passed** at `4812ea0`; **590 passed**, 3 deselected (opt-in live-data and sandbox tests) with the risk-page change; ruff, mypy strict (189 files) and secret scan (445 files) clean.

## S14 — Venue change: HFM primary, Binance second; Coinbase and Bitstamp retired (2026-10-15)

- **Owner decisions** (ADR-027):
  - Exness is dropped. Its API is available only in Vietnam and Thailand and not to MetaTrader accounts (Exness help centre).
  - HFM (`my.hfm.com`) becomes the primary venue: demo only, buy only, no leverage. It connects through MetaTrader 5 on the owner's Windows PC now, and a Windows VM on Proxmox in production.
  - Binance becomes the second venue.
  - Coinbase and Bitstamp stop being used; their data is kept.
- **IMPLEMENTED (Binance part):**
  - **Retired venues:** a setting, `COSMIC_RETIRED_VENUES`. Their sources are disabled at the registry sync and skipped by the scheduler, so no stale-data alarms. Their instruments are no longer tradable; nothing stored is deleted.
  - **Registry decides tradability:** it now applies to existing rows too, not only new ones.
  - **Safety check:** every 10 minutes the worker looks for positions or unfinished orders left on a retired venue, and raises an incident that explains how to recover.
  - **Paper trading on Binance prices:** BTC/USDT and PAXG/USDT are tradable for paper; the Operations form offers Binance (USDT) first and refuses retired venues.
  - **Paper simulator:** its venue status now follows its accounts' venues instead of a hard-coded Coinbase.
  - **Default risk policy for new installations:** includes the Binance paper pairs and USDT.
  - **Automatic proposals:** the strategy library and the AI missions now pick the paper account on the instrument's own venue (before: the oldest paper account, which was Coinbase).
  - **Strategy library:** rules keep their evaluations on the stored Bitstamp BTC history. Live signals read Binance BTC/USDT daily bars, whose history is backfilled since 2017. Retired sources are never backfilled, and the rule pages hide switches on retired instruments unless already on.
- **NOT DONE:**
  - HFM (B-10): the official MT5 Python documentation must be reviewed first (the research agent stopped at the account's usage limit before writing anything), then the bridge, adapter, emulator, CFD accounting and risk rules.
  - Retiring Coinbase on staging waits for the owner to close `paper-main`'s 0.0024196 BTC paper position (runbook §10).
- **TESTS:** `make check` → **595 passed**, 3 deselected; ruff, mypy strict, secret scan clean. New: `tests/trading/test_binance_paper.py` (Binance paper trade, retirement, exposure incident, account per venue), `tests/e2e/test_paper_venues.py`.

## S15 — HFM through MetaTrader 5: milestone 1, the connection (2026-10-15)

- **On the owner's PC:**
  - MT5 build 6264 is logged in to the HFM demo account (server `HFMarketsGlobal-Demo`, hedging, USD, demo balance 100,000).
  - The official `MetaTrader5` 5.0.6231 package was installed in Windows Python 3.11, and a read-only probe connected.
  - Two setup problems were found and fixed with the owner: the terminal was not logged in (`IPC timeout`), then logged in to the wrong server `…-Demo3` (`Authorization failed`).
  - Facts read from the terminal: `docs/integrations/hfm-mt5-demo.md`. The package's constant names and values were verified on the PC.
- **IMPLEMENTED:**
  - ADR-028.
  - Migration 0010 (broker venues, CFD instruments, metal assets, the `hfm_mt5_demo` SANDBOX adapter value, `bridge_state`, `bridge_commands`; tested downgrade).
  - HFM venue and instruments `CFD:XAU/USD@HFM` and `CFD:BTC/USD@HFM`.
  - `POST /bridge/v1/sync`: bearer token in constant time; 404 without a token. Prices, status and contract rules pass through the normal ingestion, throttled. A non-demo account gets no commands and raises a CRITICAL incident. Commands go out once and their results are recorded.
  - The bridge program (`bridge/cosmic_mt5_bridge.py`): demo only, buy only, stops on positions, magic-number idempotency, journal, deadlines.
  - The MT5 emulator.
  - Tests: `tests/unit/test_mt5_bridge.py` (11), `tests/e2e/test_bridge_sync.py` (4).
- **NOT DONE (milestone 2):** the worker's HFM adapter (commands from orders, the CFD→spot view, fills from deals), the HFM contract check, HFM account opening, and the risk limits the owner must raise (1 oz of gold is about USD 4,100).
- **TESTS:** `make check` → **610 passed**, 3 deselected; ruff (with `bridge/`), mypy strict (194 files + the bridge), secret scan (462 files) clean.

## S16 — HFM through MetaTrader 5: milestone 2, trading (2026-10-15)

- **IMPLEMENTED:**
  - **`HfmBridgeAdapter` in the worker:**
    - Order operations become bridge commands; a send that times out is "outcome unknown" and is reconciled, never resent.
    - Orders, fills and balances are answered from the bridge's latest report: at most 15 s old, demo only, broker time converted to UTC.
    - The hedging CFD account is shown as fully paid spot (USD = balance − open cost; holdings in oz/BTC; fees = commission, fee and swap), so the standard reconciliation works unchanged.
  - **Exits:** the entry an exit order closes is read from the order record. The first end-to-end run found that exit orders carry the exit's trade code: the bridge refused the sale safely and the stop was put back.
  - **Contract check:** `cosmic broker hfm-check`: a 1 oz buy limit far below the market, re-sent, listed, cancelled; balances read. Recorded with the account fingerprint, which the bridge now sends (a hash of login@server).
  - **Test venues generalised:** opening and the contract-run gate work per adapter (Binance test network, HFM demo); Operations shows both cards.
  - **Price cross-check:** a pair quoted by one fresh venue is compared with its reference pairs (HFM gold with PAXG/USDT, BTC/USD with BTC/USDT).
  - **Bridge history:** 120 days of deals by default.
- **Tests:** `tests/trading/test_hfm.py`. The real bridge code runs on the MT5 emulator (broker clock UTC+3) through the real sync into the execution engine:
  - contract check and account opening;
  - buy → stop on the position → owner exit → reconciliation exact;
  - a stop-out with a night's swap booked and reconciled;
  - a real account never traded.
- **Owner's paper account:** `paper-main`'s 0.0024196 BTC was sold by its protective stop at 80,014.66 (13:31 UTC). `paper-binance` (10,000 USDT) is open. Coinbase and Bitstamp are retired on staging (`COSMIC_RETIRED_VENUES`, applied with this deploy).
- **TESTS:** `make check` → **616 passed**, 3 deselected; ruff, mypy strict (195 files + the bridge), secret scan (464 files) clean.
- **Real HFM contract check (staging, the owner's demo account):**
  1. 14:44 UTC, FAILED: HFM answered a placed buy limit with 10009 (DONE), deal 0, and the bridge read it as a fill. The resting order was cancelled through the bridge at once (confirmed by HFM; nothing filled). Fixed in `f788ec1`.
  2. 15:07 UTC, FAILED: two bridge processes were running, and the old one took the command. The check's new clean-up removed its order. The bridge now has a single-instance lock.
  3. 15:09 UTC, **PASSED**, all 8 steps. The account was clean afterwards.
- **AI providers connected by the owner (keys in server secret files, hidden input):**
  - **Groq:** key works; 11 models listed; suggested `openai/gpt-oss-20b` (SMALL) and `openai/gpt-oss-120b` (LARGE).
  - **OpenRouter:** free tier, usage 0, 14 `:free` models at live price 0; suggested `google/gemma-4-31b-it:free` (LARGE) and `google/gemma-4-26b-a4b-it:free` (SMALL).
  - Its spending limit is checked by the owner.
  - Attestation and model approval are the owner's steps on the AI page.


## S17 — Owner's directive of 2026-10-15: audit, then the verified gaps (2026-10-15 → 2026-10-17)

- **Phase 1 audit:** `docs/reports/2026-10-15-audit.md`.
  - Checked again: staging health, audit chain, incidents, reconciliation, bridge freshness, contract runs, strategy results, secrets per container, backups.
  - Restore drill on the newest staging backup `cosmic-20261015T150626Z`: **DRILL PASS**, 10/10 checks. It ran on the workstation from a copy of the encrypted set, because running it on the shared server was not permitted in this session; the copy was deleted afterwards.
  - Baseline `make check`: 618 passed, 1 failed. The failure was a timing flake under load in the HFM contract check's clean-up; it passes 3/3 alone and is fixed below.
- **Environment labels and venue-true approval terms (ADR-029):**
  - `environment_of(kind, adapter)` gives PAPER, TESTNET, DEMO or LIVE. An unknown account is never read as safer than it is.
  - New packets record the account's adapter and environment and the instrument's asset class.
  - Terms that depend on the venue come from `trading/venue_terms.py`:
    - HFM's stop is described as a broker stop-loss on the position, filled at the market price;
    - CFD hours are the broker's (gold closes at weekends);
    - funding is the overnight swap, with HFM's rates from the instrument rules.
  - Telegram (`SIMULATED · DEMO — HFM MetaTrader 5 demo account, virtual funds`), the confirmation screens and Portfolio use the same label. The HFM demo account is no longer called "Binance test network".
  - The HFM contract check cancels a second time only when the cancel step did not confirm.
- **TESTS (environment labels and venue terms):** `make check` → **622 passed**, 3 deselected; ruff, mypy strict (196 files + the bridge), secret scan clean. New: `test_every_proposal_names_its_environment`, `test_environments_never_read_safer_than_they_are`, `test_venue_terms_describe_cfds_and_broker_stops`; the HFM packet terms are asserted in `test_buy_stop_and_exit_through_the_bridge`; a passing contract check sends exactly one cancel.
- **HFM stop-loss sent with the entry (ADR-028, bridge version 2):**
  - The approved stop price travels in the buy request (`sl` on the `BUY` deal or the `BUY_LIMIT` order). HFM holds the stop on its server from the fill, even when the bridge, the PC or the worker is offline. Before, the stop followed the fill as a separate request; the bridge was away about 4 h on 2026-10-15.
  - Adapters declare `attached_stop`; the execution engine reads the plan's stop only for those, and only for entries.
  - The HFM adapter refuses an entry with a stop while a bridge older than version 2 is connected. Nothing is sent.
  - Until the protection step sends the stop order (seconds after the fill), the entry's own stop is not listed as an unknown open order.
  - If the stop closes the position first, the bridge reports the stop order as filled from the stop-out deal, and the books stay exact.
  - Contract version 2: the resting buy limit carries a stop below it, and the check confirms that HFM holds it.
- **TESTS (attached stop):** `make check` → **628 passed**, 3 deselected; ruff, mypy strict, secret scan clean. New bridge tests:
  - the stop travels with a market entry and with a resting one;
  - a stop at or above the entry is refused before sending;
  - a stop order after a stop-out reports the fill.

  New end-to-end tests:
  - the entry is protected before its stop order exists (stop-out in that window; reconciliation exact);
  - an old bridge is refused the entry, and nothing reaches MT5.

  Both new end-to-end checks were mutation-tested: removing either rule makes its test fail.
- **Real HFM contract check, version 2 (staging, the owner's demo account, 2026-10-15):**
  - 21:50 UTC FAILED at the first order with 10018 "Market closed" (gold's daily break); nothing was placed.
  - 22:06 UTC **PASSED**, 9/9: the `BUY_LIMIT` carried its stop-loss, HFM held it on the order, the re-send found the same order, and one cancel removed it.
  - The packet's session text now names the daily break as well as weekends.
  - A market buy with a stop-loss has not been sent to HFM yet; the first demo trade will show it.
- **Backups watched by the worker (ADR-019 addendum):**
  - The newest set (complete: four files, sizes as in its manifest) and the last restore drill are shown on **Operations → Workers → Backups**, in the heartbeat and in the daily report.
  - Incidents: newest over 26 h old (WARNING; CRITICAL after 50 h), incomplete (CRITICAL), drill due after 35 days or failed. Each resolves by itself.
  - `scripts/drill-staging.sh` drills the newest staging set on the workstation (the key never reaches the server) and appends the result to `restore-drills.jsonl` on the server.
- **Daily report and `/status`:**
  - `notify.daily_report` sends once per UTC day after 05:00 UTC (`COSMIC_DAILY_REPORT_HOUR_UTC`): mode and health, each account on its own line in its own currency and environment (equity, cash, P&L, limit use, reconciliation, positions and their stops), trades closed in 24 h, strategy status ("NO QUALIFIED STRATEGY" when none).
  - Telegram `/status` answers with the same health lines.
- **Job history retention (ADR-005 addendum):** DONE jobs are kept 14 days and DEAD jobs 90 days; at most 5,000 rows are deleted per hourly run, oldest first.
- **TESTS (operations):** `make check` → **640 passed**, 3 deselected; ruff, mypy strict (198 files + the bridge), secret scan (474 files), shellcheck clean. New:
  - `tests/unit/test_backup_check.py` (6);
  - `tests/integration/test_backup_job.py` (2; incidents open and resolve; the heartbeat and Operations show the report);
  - `tests/trading/test_daily_report.py` (3);
  - `test_finished_jobs_are_pruned_after_retention`.
- **First run on staging found a permission gap (fixed the same evening):**
  - On the test server, backups are written as the host user (mode 0600 in a 0700 directory), so the worker (UID 10001) could not list them.
  - It opened a false CRITICAL "No backup found" incident, and the first daily report (22:12 UTC) said "Backups: NONE FOUND".
  - Fix: sets are written group-readable (`umask 027`); the worker joins the host user's group in `compose.test-server.yaml`; the directory was made 0750 and its files 0640 (runbook §8).
  - An unreadable directory now reads "Backups cannot be checked" (WARNING), not "No backup found".
  - The restore drill was then run with `scripts/drill-staging.sh` on the newest set `cosmic-20261015T221240Z`: **DRILL PASS**, 10 checks, recorded on the server. `make check` → 640 passed.
- **Telegram proposal card (directive Phase 3; ADR-029 addendum):**
  - New packets record the strategy (rule and version, or "manual proposal by the operator") and the estimated loss at the stop as a % of equity.
  - The card lists the approved terms itself: instrument and venue, order and band, quote and time, stop and target, notional, loss at the stop and its share of equity, assumed fee, strategy, and risk checks passed. Buttons are unchanged (details, evidence, counterarguments, risk, impact, approve with confirmation and authenticator code, reject, request changes).
  - **Seen in the test:** the fee assumption is policy-wide (`fee_bps_assumption`), so a 1 oz HFM gold card shows an assumed fee of about USD 24. That overstates HFM, which charges a spread and an overnight swap. A per-venue fee assumption is a follow-up.
- **TESTS (card):** `make check` → **641 passed**, 3 deselected.
- **Fees per venue (B-08):**
  - The risk policy gains `venue_fees` (Risk policy → Fee per venue). The owner's fee for a venue drives the packet's fee estimate and basis, R07 (cash incl. fees), R11 (cost), sizing and paper fills. Other venues keep `fee_bps_assumption`.
  - Unknown venues, duplicates and values outside 0–500 bps are refused.
  - **Tests:** `make check` → **647 passed**, 3 deselected.
  - **Hash safety:** `active_policy` re-checks every stored policy's hash and fails closed. A field added naively would have changed every stored hash and blocked all trading. `venue_fees` is therefore hashed only when set, and a test pins the old hash.
- **Overview (directive Phase 2, first step):**
  - New cards on the Overview: **Accounts** (each in its own currency and environment, with equity, cash, P&L, drawdown, positions with their stop state and reconciliation; never added together), **Prices** (every instrument the policy allows, with quote time and freshness) and **Connections** (worker, Telegram bot and HFM bridge heartbeats, last backup, restore drill, strategy status).
  - The attention rules now live in one place (`command_center.attention_items`) for the Overview, the daily report and `/status`.
  - The daily report moved next to it (`reporting/daily.py`) and gains an HFM bridge line and a "Needs attention" line.
- **Alerts for a silent bridge (ADR-028 addendum):**
  - Overnight 15–16 October the bridge was away 00:06–06:10 UTC. Each outage raised three alerts for one cause, one of them a CRITICAL "our records and the venue disagree" although nothing had been compared.
  - Now: one "Bridge not reporting" WARNING, keyed on the heartbeat. An unreachable venue gets "venue unreachable, account not reconciled": WARNING when flat, CRITICAL with positions or orders.
- **TESTS (Overview and alerts):** `make check` → **648 passed**, 3 deselected. New:
  - `test_a_silent_bridge_is_one_incident_and_never_a_disagreement`: flat → two WARNINGs; back → both resolve; with a position → CRITICAL;
  - Overview assertions in `test_command_center_answers_the_questions`.
- **Weekly and monthly reports (directive Phase 10):** `reporting/periodic.py`, job `notify.period_reports`.
  - Sent on Mondays and on the 1st at the report hour, for the last complete ISO week or calendar month (UTC).
  - Each account is reported apart, in its currency and environment: equity change against the period before, largest drawdown from the 5-minute equity samples, trades closed with wins and losses, realised P&L, and fees per currency (cents for dollar-like currencies).
  - Also: incidents with the longest one named, backups, strategies, and the definitions used.
- **TESTS (periodic reports):** `make check` → **651 passed**, 3 deselected; new `tests/trading/test_period_reports.py`:
  - whole ISO weeks and months in UTC, due on their day after the hour;
  - each account compared with the period before, drawdown, trades, fees in cents;
  - sent once.
- **Dark dashboard and equity curves (directive Phase 2):**
  - The portal is dark by default; the light palette is kept for printing. Buttons get dark text on the light accent (white on light blue failed contrast).
  - Cards are spaced consistently.
  - **Portfolio** draws each account's equity over the last 7 days from the 5-minute samples (server-side SVG, no scripts), thinned to 240 points with both ends kept. With too few samples it says so.
  - **TESTS:** `make check` → **652 passed**, 3 deselected (new `tests/e2e/test_equity_curve.py`).
- **Risk limits R18 and R19 (directive Phase 5; ADR-013 addendum):**
  - **Concurrent positions** (default 3; dust excluded, resting buys counted).
  - **Total open risk** (default 3 % of equity): the loss if every stop is hit, with unstopped holdings and stop-less trades at their full value; resting entries count at their own stops.
  - The context builder now carries lot sizes and the stops of resting entries; requests carry the plan's stop-limit.
  - Both limits can be changed on the risk page. They are hashed only when changed from their default, and staging's stored v2 keeps its hash (test).
  - Two tests that proposed stop-less trades above 3 % of equity were given stops; a dedicated test covers the refusal.
- **TESTS (R18, R19):** `make check` → **657 passed**, 3 deselected; staging's stored policy v2 re-hashed with this code: identical.
- **Deploy:** the staging deploy reuses the server's backup image when `deploy/backup` is unchanged. Two deploys on 2026-10-16 failed while uploading over a slow (about 130 KB/s) and unstable line; nothing changed on the server.
- **Scheduled-job health (directive Phase 8):**
  - The worker records every run (last success, run time, failures in a row, scrubbed last error) in `ops/job_health.JobBook` and publishes it in its heartbeat. After a restart it reads the last successes back from the job history.
  - **Operations → Workers → Scheduled jobs** shows each job.
  - `ops.job_watch` (every 5 min) raises `ops.job_overdue:<job>` after three intervals without a success, and `ops.job_failing:<job>` after three failures in a row. Both are CRITICAL for `trading.*` jobs, and both resolve after a success.
- **TESTS (job health):** `make check` → **662 passed**, 3 deselected. New:
  - `tests/unit/test_job_health.py` (4);
  - `tests/integration/test_job_watch.py`: three failures, then the incident; a success, then resolved; three intervals without a success, then overdue; shown on Operations.

  The risk check table in doc 08 now lists R17–R19.
- **TESTS (scenario batch):** `make check` → **668 passed**, 3 deselected.
- **Mandatory scenarios (directive Phase 11):** `docs/reports/2026-10-16-acceptance-scenarios.md` maps all 19 scenarios to their tests: 17 covered, 2 partial, with the gaps named. New tests close five gaps:
  - malformed Telegram callbacks;
  - the database unavailable (web and worker);
  - a venue rejecting an approved entry;
  - the emergency stop with an approved trade not yet sent;
  - a stop ended at the venue.

  The reconciliation incident is now asserted too.
- **Fixed on the way:**
  - A malformed callback raised an unhandled `ValueError` in the bot. It is now "Unknown action.".
  - The worker's new job-history read at start was outside its error handling. It is now best-effort.
  - A stop ended at the venue without our asking was replaced silently. It now also raises `trading.protection_ended_at_venue:<order>` (WARNING).
- **More scenario tests, and one more fix:**
  - New: a partial fill whose remainder is cancelled (the stop covers exactly what filled); a linked Telegram viewer can neither stop nor approve; cash (R07) and minimum size (R16) refuse a real proposal.
  - Fixed: an approved trade that could not reach the venue failed without telling the owner. It now sends `order_rejected` with the reason, and Telegram shows the reason for every rejection.
  - The report now counts 17 scenarios covered and 2 partial: the MT5 terminal's own disconnect, and the drill's failure cases outside pytest.
  - **TESTS:** `make check` → **672 passed**, 3 deselected.
- **Two more scenarios covered, one defect fixed:**
  - MT5 disconnected from HFM: R10 refuses new HFM trades until it reconnects (test).
  - Fixed: a Binance order request answered with HTTP 200 and a non-JSON body was read as "not accepted", so the order would have been marked never sent although it may exist. It is now "outcome unknown" and reconciled by client order ID.
  - The scenario report counts 18 covered and 1 partial: the restore drill's own failure cases run outside pytest.
- **TESTS:** `make check` → **675 passed**, 3 deselected.
- **Restore drill with negative checks** (`scripts/drill-staging.sh --negative`, staging's newest set `cosmic-20261016T162244Z`): **DRILL PASS**, 10 checks. A copy with one flipped byte in the dump and a freshly generated wrong key were both refused. Recorded in the server's `restore-drills.jsonl`. All 19 scenarios now have evidence.
- **Strategy library (directive Phase 7):** each series where no rule qualifies now says **NO QUALIFIED STRATEGY**, notes that the thresholds are fixed in the code version and never lowered, and lists how many rules failed each test (significance after deflation, the validation period, double costs, too few trades, drawdown).
  - **TESTS:** `make check` → **676 passed**, 3 deselected.
- **2026-10-17: the last three scenario gaps closed, and the defects behind them fixed:**
  - REQUEST CHANGES ends an approval already in progress. A rejected or changed proposal cannot be approved from Telegram or the portal (`test_a_decided_trade_can_never_be_approved_afterwards`).
  - The portal decision form sent twice at once, then resent, gives one approval and one order; the other requests answer 409 "already used" (`test_a_decision_form_sent_twice_approves_once`).
  - **Fixed:** an entry being sent when the emergency stop was pressed stayed open at the venue once acknowledged, and filled during the stop. ADR-012 had named this window, but nothing handled it. Now every execution cycle during the stop cancels any entry open at a venue (`cancel_entries_under_stop`, after the steps that record venue answers and place protective orders). The stop's alert counts the entries in flight. The test failed before the fix (FILLED) and passes after (CANCELLED, no fill). What fills before the venue acknowledges stays a position, protected by its plan (now tested too).
  - **Fixed:** a cancel request that never reached the venue (venue unreachable, or no clear answer) was not sent again, so the order stayed open at the venue. Now `follow_up_cancels` sends it again while the order stays cancel pending: a minute apart for the first five sends, then every 5 minutes. After the fifth send `trading.cancel_unconfirmed:<order>` (CRITICAL for an entry, WARNING otherwise) tells the owner, and it closes once the venue reports the order finished.
- **Three reviews of these changes (2026-10-17 → 2026-10-18).** Reviewer agents read the code, ran experiments in their own test databases and tried to refute their own findings. Several stopped at session limits, so not every area had a separate verifier. Findings not verified by an agent were checked against the code here before fixing.
  - **Round 1.** Five areas: the stop sweep, the cancel follow-up, the tests, the deploy script, and docs. Only the cancel follow-up had its verifier finish (all four confirmed, with experiments), and the docs check did not run.
  - **Round 2.** Four areas. The execution-logic and system-interactions reviewers finished; the deploy-script and docs reviewers did not.
  - **Round 3** (the deploy script and docs again, plus a verifier for the round-2 fixes). The deploy-script and docs reviewers finished; the verifier did not. Their findings are fixed below.
  - **Mutation check on the final code:** 21 mutations, one per fix or part of a fix, each make its test fail. The runner applied each change in turn and restored the file; it is not committed. Not covered by a mutation: the per-intent isolation inside the execute step and the per-order isolation inside the stop's cancel step (same `StepErrors` pattern as the tested ones).
  - **Fixed from round 1:**
    - The stop's cancels no longer depend on the earlier steps of the cycle. Each step, order, exit and position runs on its own (`StepErrors`), and the first failure is raised at the end. Before: a corrupt policy or one account's missing adapter stopped every cycle before the cancels.
    - Cancels are no longer given up after five sends. A venue outage of about four minutes would have ended them for good, which brought the original defect back.
    - The cancel alert re-checks the order under its row lock, so a just-settled order never gets an alert that cannot close.
    - **An order the venue no longer has is ended.** A venue-acknowledged order that has been "not found" for more than a minute (the Binance test network deletes every order when it resets) is recorded as CANCELLED if a cancel was pending, EXPIRED otherwise. This happens on reconciliation evidence, with `trading.order_missing_at_venue:<order>` (WARNING), and a protective order is replaced under its plan. Before: such an order stayed open in our books forever, and reconciliation counted it as a match, including a wiped stop believed to protect a position.
      - The minute is counted from the first "not found" this process sees, and any sighting resets it. One stray answer after a long silence ends nothing, and neither do two stray answers with a sighting between them. A restart only makes it wait longer.
    - A venue answer read just before a concurrent change is dropped (`status_version` check). Before: a false CRITICAL "illegal transition" that never closed.
    - An HFM cancel fails at once while the bridge is silent, instead of holding the cycle 25 s.
    - The stop's alert no longer promises to cancel in-flight entries under a policy that keeps them.
    - The Telegram bot checks the trade's status before asking for, or using up, an authenticator code.
  - **Fixed from round 2:**
    - **HFM shared stop-loss.** MT5 keeps one stop-loss per position. Re-sending an older stop's cancel cleared the stop-loss a newer stop of the same entry held (a partial fill, a lost cancel, then the rest filled). The follow-up no longer re-sends it while another stop of the trade holds the stop-loss (`stops_per_position`, `_newer_stop_holds_it`; an emulator test runs the real bridge code).
    - **A failing step is reported by the cycle itself.** Job health counts a run that is not the leader as a success, so a step failing on every slow cycle could look healthy. After three failures in a row the cycle raises `trading.cycle_step_failing:<step>` (CRITICAL), and closes it when the step succeeds.
    - **No cancel recorded without an adapter.** `request_cancel` resolves the venue adapter before anything is written, and positions of an account this process cannot reach are not managed here.
    - **Reconciliation asks again.** An order the venue listed as open while our record shows it final is asked about again. Only a venue that still reports it open, or cannot be asked again, is a mismatch.
  - **Fixed from round 3:**
    - The deploy script refuses when a probe of the server fails, instead of silently choosing a different set of images, which made a rerun start the upload from zero.
    - It validates `COSMIC_UPLOAD_ATTEMPTS`, and retries a transfer the server's end stopped (rsync code 20) instead of calling it an interruption.
    - Four tests were made exact:
      - the HFM shared stop-loss test counts SL1's cancels at MT5, waits for the bridge's report, and queries incidents directly;
      - the stop test's entry rests until the stop cancels it, at a venue that matches on arrival;
      - new tests cover "a sighting resets the minute" and the adapter-less account's positions being left alone;
      - the exit-isolation test now releases two exits in one pass.
    - Docs corrected: counts, an unqualified "harmless at every venue" in ADR-012, the reconciliation wording, and this section.
  - **Deploy script tests** (outside pytest, in scratch harnesses):
    - the retry loop with scripted rsync exit codes and attempt values;
    - one SIGINT sent to its process group from a new session, so SIGINT is at its default as in a terminal (an earlier attempt from a background job had SIGINT ignored and showed nothing);
    - the server probes with a stub ssh for each case.
  - **Known and left as is:**
    - On HFM, cancelling one of two stop orders of an entry by hand clears the entry's stop-loss until the next cycle places a replacement (ADR-028 addendum).
- **TESTS (2026-10-18, final code):** `make check` → **701 passed**, 3 deselected; ruff, mypy strict (200 files + the bridge), secret scan (483 files) and shellcheck clean.
- **Staging (`28e269e`, 2026-10-18 16:38 UTC):** deployed with the new `scripts/deploy-staging.sh` at about 104 KB/s. Results:
  - pre-deploy backup `cosmic-20261018T163748Z`; audit chain ok (3,837 events); live trading DISABLED; VM VERIFIED; public `/readyz` ready through Cloudflare Access;
  - the worker's heartbeat shows all 30 scheduled jobs OK, and `trading.cycle` completed 34 times in the first 3 minutes with no `execution_step_failed`;
  - the HFM bridge had been silent since 2026-10-17, when the PC restarted after three hours without reaching the server. It was restarted with `start-bridge.bat`. `data.bridge_offline` and `accounting.venue_unreachable` closed by themselves within 4 minutes, and `hfm-demo` reconciles OK.
  - At the owner's request it now starts at Windows logon: `COSMICTRADING MT5 bridge.cmd` in the Startup folder runs `start-bridge.bat`. This is not yet seen working; the next logon shows it.
- **2026-10-18: HFM stop-out after a partial fill fixed** (an older defect found by the last review). After a partial fill an HFM entry had two stop orders, each sized to its part, while MT5 keeps one stop-loss per position and the adapter gives every stop-loss deal to the newest stop. A stop-out's second deal was refused as an overfill, and the books kept a position MT5 had closed (detected as a CRITICAL overfill and a failed reconciliation).
  - Now, on a venue with `stops_per_position`, the new stop covers the whole holding and replaces the older stop orders in our books. They end CANCELLED, "superseded", and nothing is sent for them.
  - They end inside a savepoint with the new stop's risk checks, so R17 counts the new stop alone, and a failed check keeps them as they were. While an older stop is still on its way, the new one waits.
  - Test: partial fill, SL1, the rest fills, SL2 for 2 oz, then an MT5 stop-out of both positions. The books end flat, SL2 is FILLED for 2 oz, and there is no overfill; reconciliation passes. It fails with the change switched off.
- **2026-10-18: no false "Reconciliation failed" for an order being sent.** The stop reconciles every account at once, often while an entry is on its way, and that raised a CRITICAL "our records and the venue disagree". Now an order created or sent less than a minute ago is reported as "being sent now". A run whose only mismatches are such orders does not pass: the account stays REQUIRED and new exposure stays blocked. But it raises no disagreement alert, and the next run passes once the order is settled (`test_the_stops_reconciliation_does_not_call_an_entry_being_sent_a_mismatch`; it fails with the change switched off). An order stuck longer than a minute keeps the CRITICAL path.
- **TESTS (2026-10-18, after the HFM stop and in-flight reconciliation fixes):** `make check` → **703 passed**, 3 deselected; ruff, mypy strict (200 files + the bridge) and secret scan clean. The first run had failed one HFM test: a misplaced branch in the new reconciliation case had stopped a passing run from closing its incidents. It was fixed before this commit.
- **Staging (`f6f8b7a`, 2026-10-18 19:22 UTC):** deployed and checked.
  - pre-deploy backup `cosmic-20261018T192204Z`; audit chain ok (3,942 events); live trading DISABLED; VM VERIFIED; `/readyz` ready;
  - after the restart: 0 open incidents, 0 worker errors, all 30 scheduled jobs OK, `trading.cycle` 34 runs in 3 minutes, and all three accounts reconciled OK.
- **Monday checks (2026-10-19, 05:35 UTC):**
  - **Weekly report:** the first one, for week 42, was sent at 05:00 UTC. It shows each account in its own currency (paper-main: one closed trade, -8.76 USD after 2.36 USD of fees), incidents, backups, NO QUALIFIED STRATEGY and the definitions. The daily report was sent too.
  - **Jobs:** all 30 OK, with no false overdue alerts.
  - **Bridge at logon:** the bridge stopped reporting at 23:03 UTC, which matches the PC going to sleep or shutting down; its log has no error. At the owner's logon (05:25 UTC) it started by itself in the new Windows session. `data.bridge_offline` and `accounting.venue_unreachable` (WARNING, flat account) closed by themselves. While the PC is off, HFM pauses safely; overnight coverage needs the PC awake, or the planned Windows VM.
- **Deploy over the slow line:**
  - `scripts/deploy-staging.sh` sends one zstd file through `rsync --partial --append-verify`. Dropped connections are retried and resume where they stopped, and the server checks the SHA-256 before loading.
  - A resume was tested against the server with a cut-off upload: only the missing 2.7 MB of 3 MB were sent, and the checksum matched.
  - `COSMIC_BUILD_NETWORK=host` lets the build run when Docker's default network cannot resolve names, as on the workstation's native engine on 2026-10-17.
- **Workstation:** two Docker engines ran at once on 2026-10-17. Docker Desktop restarted at 15:08 UTC, while a native engine in WSL had been serving the shell since 14:25 UTC. The dev Postgres created in the native engine at 14:45 UTC holds 127.0.0.1:55442, so Docker Desktop's own dev Postgres runs without its port. Tests create their own databases either way.
- **Public repository hygiene:** the tracked docs and scripts no longer give credential file locations on the owner's machines in prose, the owner's earlier e-mail, the server's public IP address, or which credentials still need rotating or limits. They keep the server deployment's secret directory and file names, which the tracked deploy files show anyway, and scripts keep the paths they read. At the owner's choice (2026-10-18), `scripts/cloudflare_setup.py` names the owner's Access login as its default allow-list; `COSMIC_ACCESS_EMAILS` overrides it. The older text is still in git history; the specifics went to the owner directly.

## S18 — Production VM: production and staging leave the shared server (2026-10-19)

The owner gave a dedicated VM and asked that it hold both production and staging, and that the shared test server no longer be used. Their choices: remove MicroK8s; move the current data to production (staging starts empty); production takes `staging.cosmictrading.bond` with no DNS change, and staging has no public address; the Telegram bot and the HFM bridge go to production only.

- **Tooling** (`e482b3f`, `f9d2b2a`):
  - `deploy/compose.staging.yaml`: its own project and database volume, a smaller database, a loopback relay on 127.0.0.1:8744, and no tunnel or bot. `deploy.sh` refuses a non-production root without an overlay, and staging with the tunnel or Telegram profiles.
  - `deploy/vm/staging-init.sh` creates the staging root with fresh secrets and empty live tokens.
  - `bootstrap.sh` was fixed for Ubuntu 26.04 (buildx, rsync, zstd, the guest agent only when Proxmox gives its port, sshd checked before reload). `restore.sh` no longer lets compose read the key from stdin.
  - `verify.sh` knows production, staging and the shared host.
- **Setup on the VM:**
  - `bootstrap.sh --apply`. The installer's CD-ROM apt source was disabled (renamed), since apt failed on it.
  - MicroK8s was removed.
  - Staging was created with `staging-init.sh --apply` and runs `f9d2b2a`; its images were built on the VM.
- **Data move:**
  - The old stack was drained (bot, worker, then the tunnel and web). Its final backup `cosmic-20261019T124751Z` was copied to the VM and restored into the empty production database; its live tokens were withdrawn.
  - Checked against the old server: row counts in 73 tables, schema revision `0010_hfm_bridge`, 23 triggers, sequences, database grants, and the audit head (seq 4,637, same hash). `audit_verify` is OK on both, and an UPDATE on `audit_events` is refused on the VM.
- **Cut-over (12:47:37–12:52:55 UTC, about 5 minutes without service):**
  - Seven live tokens were moved into the VM's secret files; each SHA-256 matches the old copy, and none was printed or stored on the workstation. Then `COMPOSE_PROFILES=telegram,tunnel` and `deploy.sh f9d2b2a`: pre-deploy backup, smoke tests, audit chain OK (4,649 events), live trading DISABLED.
  - The bot polls without conflict, and the tunnel has 4 edge connections.
  - The HFM bridge reconnected by itself: snapshot under a second old, all 12 commands DONE.
  - All three accounts reconcile with 0 mismatches. The 9 incidents the gap opened (overdue jobs, bridge offline, HFM unreachable) closed by themselves on the next 5-minute runs, and their Telegram alerts were delivered.
  - `cosmic-backup.timer` is enabled; one run through the systemd unit wrote `cosmic-20261019T125414Z`.
  - On the old server, only the cosmictrading backup cron line was commented out; the other five lines are byte-identical. Its containers are stopped with restart disabled, and its volumes are kept.
- **SSH from anywhere moved too:**
  - The jump image, its host keys (same fingerprint), the authorised keys and the SSH tunnel token were copied to the VM; the old connector was stopped first, since two connectors on one tunnel share its traffic.
  - The firewall now lets the jump network (`br-cosmic-jump`, 10.203.65.0/29) reach sshd; `bootstrap.sh` adds that rule.
  - `ssh cosmictrading` reaches the VM through Cloudflare Access.
- **Checks:**
  - `verify.sh`: production 13/13, staging 10/10, VM VERIFIED. This was run from the working copy, which already had the loopback fix below, copied to the VM for the run. The VM's checkouts were still at `f9d2b2a`, whose `verify.sh` reports a false FAIL for "no application port listening on the LAN".
  - `verify-public.sh staging.cosmictrading.bond`: ALL CHECKS PASSED.
  - `scripts/drill-backups.sh` on the newest set, from the workstation over the internet path: DRILL PASS, 10 checks; with `--negative`, the tampered copy and the wrong key were refused. The workstation lacked that release's backup image, so the script built it from the commit.
- **Measured (idle):** about 815 MiB for production, staging and the SSH stack together; 1.5 of 10 GB RAM; disk 11 % of 98 GB (docs/12 §9).
- **Release scripts:**
  - `scripts/release.sh` replaces `scripts/deploy-staging.sh`: the VM builds the images, staging is deployed and checked, and `--production` sends the same images to production.
  - `scripts/drill-staging.sh` is now `scripts/drill-backups.sh`, for production's backups.
  - `release.sh` was checked with shellcheck and its guard-rail tests. Its `sudo` steps need a terminal (they ask for the password) and have not yet been run through the script.
- **Not done (owner's call):**
  - **Rebooting the VM** to finish AT-20 (VM part) and DEP-09. The tool's permission check blocked it in this session.
  - **A password-less `sudo` rule** for `deploy.sh` and `verify.sh`, which would let releases run without a terminal. It was also blocked, and it was left out of `bootstrap.sh`.
- **Fixed after the cut-over, in the commit after `f9d2b2a`:**
  - `verify.sh` treats all of 127.0.0.0/8 as loopback. systemd-resolved listens on 127.0.0.53 and .54, which gave a false FAIL.
  - `verify.sh` also fails when `cosmictrading.service` has failed; "enabled" was not enough. On the VM the unit had failed at first boot, at 12:24, because `compose.env` still held the example image tag. Since then the stack had been running only through `deploy.sh`. The unit was started with the finished configuration and became active; a dry run showed first that no container would be recreated. The VM restart itself is still to be done.
  - `bootstrap.sh` adds the firewall rule for the jump network, and it and `staging-init.sh` create `restore-drills.jsonl` group-writable (0660). On the VM that file had been made by hand; on a rebuilt VM the drill would have failed to record its result.
  - `deploy.sh --build` accepts a tag of any length that names HEAD. `release.sh` uses a fixed 7-character tag and compares full commit IDs on the VM. Git picks the default abbreviation length from the packed objects, so the workstation and the VM could have chosen different lengths.
- **Review of these changes** (three reviewers, each finding checked by a skeptic): 8 findings confirmed and fixed, the four above and four in the docs:
  - the rollback did not really stop production on the VM;
  - the docs credited `release.sh` with the running images;
  - DEP-11 was marked done while the Proxmox side is unconfirmed;
  - a second computer's key must also go in the VM's own `authorized_keys`.
  6 findings were refuted.
- **Release `ebe52b0` (2026-10-19, 14:25–14:30 UTC)**, using the same steps on the VM as `scripts/release.sh`. `sudo` read the password from a file, because the session had no terminal, so the script itself has still not been run end to end. CI passed on GitHub.
  - **Staging:** images built on the VM (`deploy.sh --build`), pre-deploy backup, smoke tests, audit chain OK, live trading DISABLED. The VM's own `verify.sh` gave 10/10, VM VERIFIED.
  - **Production:** checked first that no order was working and no bridge command was waiting. `deploy.sh ebe52b0` reused staging's images, so the switch took 33 s, including the pre-deploy backup `cosmic-20261019T142718Z`. Audit chain OK (4,715 events), live trading DISABLED.
  - **Checks after the switch:**
    - `verify.sh` 14/14, including the new boot-unit check;
    - `verify-public.sh`: ALL CHECKS PASSED;
    - all three accounts reconciled with 0 mismatches at 14:30, with no incident opened;
    - the HFM bridge kept reporting, and the bot is polling.
  - The temporary copy of the VM password used for `sudo` was deleted.

## S19 — The staging gate, backup timers that run, environment labels everywhere (2026-10-19 → 2026-10-20)

- **CF-10 (`c41fda4`):** production's `deploy.sh` refuses a tag that has not run on staging. Staging's `deploy.sh` records a tag in its `deploy-history.log` only after its smoke tests pass; a tag production ran before is accepted, as a rollback. `--untested` overrides, with a warning and `UNTESTED` on production's history line. The check is `deploy/tested-on-staging.sh`, which is read-only and matches a fixed string, so a prefix or a dotted tag never matches another tag. `make check`: 711 passed.
- **Backup timers that never ran (found 2026-10-20 at 05:30 UTC):** staging had no backup from the night. `staging-init.sh` had enabled `cosmic-staging-backup.timer` without starting it, and an enabled timer starts only at the next boot; the VM had not rebooted since setup. Production's timer had the same gap on a new VM. It ran only because it was started by hand on 2026-10-19.
  - Now `deploy.sh` starts the root's backup timer after a successful deploy when the timer is enabled but not running, and `staging-init.sh` uses `enable --now`.
  - `verify.sh` checks that the timer is active, not only enabled: production now has 15 checks, staging 11.
  - On the VM, staging's timer starts with the next release. Its worker reports the late backup as an incident in the meantime.
- **DIR-02, environment labels:** a read-only sweep (three surfaces and a completeness critic) found about 70 places where an account, order or alert reached the owner without its environment. The worst were urgent alerts, such as "protective order failed", "order in UNKNOWN state" or "cancel not confirmed", that named neither the account nor whether the money was real.
  - **One shared name:** `cosmic.trading.modes.account_ref` gives `hfm-demo [SIMULATED · DEMO]`, `paper-main [SIMULATED · PAPER]`, or `<alias> [LIVE · real money]` for anything not simulated; it never reads safer than the money is. `account_fields` gives the same as record fields.
  - **Alerts:** `Alert.account` puts the account before the title. The incident title, Telegram and the portal inbox show it, so the incident tables and reports get it too.
    - Every alert about one account passes it: order unknown, execution blocked, protection failed or blocked, below the venue minimum, the illegal venue transition, an order missing at the venue (the test-network reset is mentioned only for TESTNET), protection ended at the venue, cancel unconfirmed, overfill and oversell, fee anomalies, the reconciliation failures, and a test-venue account not opened (named by its own venue: HFM DEMO is not the Binance test network).
    - Alerts covering several accounts (restart, retired venue) and the recovery checklist list each account. Dedup keys are unchanged.
  - **Messages:** order acknowledged, rejected and filled, trade closed, proposal expired and approval requested carry the account; older stored rows keep their `[SIMULATED]` tag.
    - The Telegram REVIEW DETAILS, RISK and PORTFOLIO IMPACT replies, the reject and request-changes replies and the final APPROVED reply name the trade, the account and its environment.
    - The confirmation screen says SIMULATED first.
    - The reports' account lines say `SIMULATED · PAPER`.
  - **Portal and API:**
    - The approval queue (its SIMULATED/LIVE "Money" column became the account), recent proposals, the approval page heading, the confirmation page, the orders list and the order page show the account.
    - So do the home page's waiting trades and account health, and portfolio reviews.
    - The manual proposal form offers the active accounts by name and environment instead of a typed alias.
    - "Needs your attention", shared with `/status` and the daily report, names accounts, and counts UNKNOWN orders and unprotected positions per account.
    - The API returns `account_ref` and `environment` for accounts, intents and orders.
  - **Records:**
    - The order and trade audit events carry the account and its environment: authorised, sent or not, acknowledged or rejected, unknown, final, protective, cancel requested or resent, execution blocked, exit, review created, proposed, re-proposed, challenge, decision, revoke and reconciliation results.
    - `account.created` records the adapter and environment.
    - The per-account reconciliation metric has an `environment` label, and the mission's sizing output names it.
  - **Left:** structured log lines carry order and intent IDs but not the environment. They are for the operator, and adding it means re-wrapping the hot execution paths. Search hits and the mission page show none yet. Proposal packets from before 2026-10-15 lack the adapter; they are all long expired, and they read as "virtual funds", which is true.
  - **How it was done:** an implementation workflow failed on network errors after its first agent had changed the alert producers. Those changes were reviewed by hand and kept; the rest was done directly.
  - **Tests:** new or extended tests for the helpers, the alert headline, the Telegram texts, the incident title, the acceptance flows, the portal flow (detail, queue, home, orders and API), the reports and the metric.

## S20 — The demo auto-trader under a mandate (2026-10-20, ADR-030)

- **Owner's request (2026-10-20):** a simulated trader that trades by itself on the demo balance, risking at most $30 per trade.
  - **Choices:** the HFM demo account; "$30" as the loss at the stop; library rules on 1-hour bars; only Turtle 20/10; password-free deploys.
  - **Measured first:** on daily bars no HFM trade fits $30. The smallest trade risks about $62 (BTC) or $144 (gold) with the library's 3×ATR stop. On 1-hour bars it risks about $12 or $28. No library rule was switched on.
- **Built:**
  - **Migration `0011_demo_mandates`:**
    - the `trading_mandates` table, with one active mandate per account and immutable terms; an ended mandate stays ended and rows are never deleted;
    - a trigger that refuses any account that is not PAPER or SANDBOX;
    - `approvals.mandate_id` and the channel `MANDATE`, with a trigger requiring an active, unexpired mandate for the trade's account.
    - The downgrade refuses while mandates exist.
  - **`cosmic.trading.mandates`:**
    - grant, with a fresh code and checks of the account, rule and instruments; revoke, without a code; expiry;
    - today's usage, and `entry_refusal`, a pure function of every limit;
    - `approve`, which binds an approval to the exact packet like the owner's and is audited and reported.
  - **`research.auto_trader`**, every 5 minutes:
    - computes the rule on the completed 1-hour bars of each instrument's signal series, and handles each bar once;
    - never uses a stale series or a stale price;
    - sizes to the mandate's loss limit with the packet's own estimate, and skips with the reason when even one lot is too much;
    - proposes through the normal risk checks; the mandate then decides.
  - **No approval requests:** mandated proposals are not sent to the owner for approval. Telegram and the inbox show "AUTO TRADE …", "Auto-trader skipped …" and "Auto-trader ON/OFF …".
  - **Operations card:** status, today's use, the grant form and the stop button.
- **Tests:**
  - `tests/trading/test_auto_trader.py` (10): sizing never exceeds the limit; each limit refuses; granting needs a fresh code and a known rule; the database refuses a mandate for a LIVE account and refuses changed terms or a revived mandate.
    - A breakout trades under the mandate: approval on channel MANDATE, no approval request, the entry filled, then the breakdown sells.
    - Revoking or the emergency stop prevents trades, and a MANDATE approval without an active mandate is refused by the database.
  - `tests/e2e/test_auto_trader_portal.py` (2): grant and stop through the portal, and the message texts.
- **Found while testing:** a price below the protective stop makes the stop sell first. The rule's exit then finds nothing to sell and is rejected without overselling, as designed.
- **Release `9c891e5` (2026-10-20, 09:45–09:50 UTC):** the first run of `scripts/release.sh --production` end to end. The owner installed the deploy rule (`/etc/sudoers.d/cosmic-deploy`, checked by `visudo`), so it needed no password. CI passed.
  - **Staging:** built on the VM, pre-deploy backup, migration `0011_demo_mandates`, smoke tests and audit chain OK, live trading DISABLED. `verify.sh` 11/11, including the backup timer that is now running.
  - **Production:** the staging gate (CF-10) passed ("9c891e5 passed staging's smoke tests"); there was a pre-deploy backup and smoke tests passed. Audit chain OK (5,429 events); live trading DISABLED. `verify.sh` 15/15, and `/readyz` is ready through Cloudflare Access.
  - **After the switch:** `research.auto_trader` runs and does nothing without a mandate. No incident is open, and the HFM bridge reports every second.
  - **Mandate:** granting it needs the owner's authenticator code on Operations, so it is the owner's step.
  - `bootstrap.sh` now installs the same deploy rule, checked with `visudo` before it is put in place, so a rebuilt VM has it too.
- **VM restart test (2026-10-20, 10:14 UTC):** the owner rebooted the VM, with the demo mandate active and no order working. This closes AT-20's VM part, DEP-09, TST-06 and B-02.
  - The VM was back about a minute later: booted at 10:14:25, and all 11 containers were healthy at about 10:15.
  - `cosmictrading.service` started at boot and is active (success); it had failed at the first boot on 2026-10-19, before setup. The backup timers and `ssh.socket` are active.
  - `verify.sh`: production 15/15 and staging 11/11. Public `/readyz` is ready through Cloudflare Access, and `ssh cosmictrading` works from the internet path.
  - **Trading:** the execution loop paused 37 s (last cycle 10:14:12, first after 10:14:49). The HFM bridge reconnected on its own, and all three accounts reconciled (PASSED at 10:15:05). No incident opened, and no startup recovery was needed, with 0 orders working, so nothing could be resubmitted.
  - The demo mandate is still active, and `research.auto_trader` runs again.


## S21 — The HFM bridge moves to a dedicated Windows VM (2026-10-21 → 2026-10-22)

- **Why:** the owner asked that MT5 and the bridge run permanently on a Windows VM on their Proxmox host, not on their PC.
  - The PC had already cost two auto-trades: the auto-trader's first two signals were skipped, because the HFM price was stale while the PC slept or MT5 was closed.
    - BTC breakout, 2026-10-20 15:05 UTC;
    - gold breakout, 2026-10-21 12:05 UTC.
  - The PC's MT5 was closed on 2026-10-21 at 11:22 UTC and uninstalled later. Each time, the server raised "bridge reports a non-demo account" (CRITICAL; the terminal reported no account at all), then the bridge went offline and the venue unreachable, and no command was sent.
- **The VM:** Windows 10 Pro (1909) under QEMU/KVM, 6 GB RAM, with OpenSSH and MetaQuotes MT5 build 6274 already installed by the owner.
- **Done:**
  - **Software:** Python 3.11.9 (installer Authenticode-signed by the Python Software Foundation; SHA-256 equal from two downloads) with `MetaTrader5==5.0.6231` and `numpy==2.4.6`, as on the PC.
  - **Bridge folder** (`C:\Users\cosmic\cosmic-bridge`): the program and `config.json` byte-equal to the PC's, start scripts for the VM, and the bridge token and Access token readable only by the user, SYSTEM and administrators. The PC's command journal was copied at the switch.
  - **Windows:** High performance, never sleeps or hibernates, and no automatic update restart while signed in. East Africa time. SSH allowed from the home network only.
  - **Scheduled task** "COSMICTRADING MT5 bridge": runs `start-bridge.bat` in the signed-in session at sign-in and every 5 minutes if not running. It has no time limit, ignores a second start, and the bridge's own lock prevents two copies.
  - **MT5 options:** MT5 refuses a saved login copied from another computer ("Accounts deleted due security reason"), and turns Algo Trading off after an account change. So it was closed gracefully and its Expert options were set: Algo Trading on; no disabling on account or profile change; the Python API allowed. The settings file was backed up first.
- **Owner's steps:** typed the demo password once in MT5 and saved it; set automatic sign-in with `netplwiz`, which stores the password encrypted. Two steps were refused by the tool's permission checks and left to the owner: installing an SSH key for the Proxmox host, and setting up automatic sign-in by the agent.
- **Switch (2026-10-22, 07:11 UTC):** the PC's startup entry was renamed (`… .cmd.disabled-20261022`), and the VM's task was enabled and started.
  - The server saw the bridge connected, DEMO, trading allowed; HFM was OPERATIONAL and both prices fresh.
  - The open incidents closed by themselves, and hfm-demo reconciled (PASSED 07:15:03).
- **Restart test (07:15 UTC):** Windows restarted. It booted at 10:15:52 EAT, signed in by itself, and the task started the bridge at 10:16:16, which started MT5. MT5 logged in at 10:17:40 and the bridge connected at 10:17:42, with trading still allowed and prices fresh. No incident opened, and the auto-trader's mandate is still active.
- **Open, for the owner:** set *Start at boot* for the VM in Proxmox (not visible from inside the VM), and upgrade Windows 10 1909, which receives no security updates. Credential follow-ups were given to the owner directly.

## S22 — The auto-trader from money, ask first, and its own page (2026-10-22)

- **Owner's request:**
  - one easy view of the trading: what each trade won or lost;
  - "just put money in";
  - "the system notifies me when it is time to trade, and handles the risk based on the amount".
- **Built** (ADR-030 addendum):
  - **Migration `0012_mandate_money`:** a mandate's money, care level and mode (AUTO or ASK). They are immutable like its other terms, and the downgrade refuses while they are used.
  - **`terms_from_money`:** Careful, Normal or Bold is 0.5, 1 or 2 % of the money per trade, with a daily stop of three such losses, 5 trades a day and 2 open. The money may not exceed the account's equity.
  - **Never more than the money:** open positions held fully paid may never be worth more than the money. The auto-trader sizes within what is free and skips with the reason otherwise.
  - **New settings** replace the old mandate in one transaction.
  - **ASK mode:** the same checks, then the normal proposal card with "⏰ TIME TO TRADE". The owner approves with the authenticator code; the mandate approves nothing itself.
  - **Heads-up:** when a market comes within half an hourly ATR of the breakout level, at most once per market every six hours.
  - **The `/trader` page,** first in the menu:
    - status, money and limits;
    - total and today, wins and losses, win rate, best and worst;
    - open trades with their result now and the loss if the stop is hit;
    - trades waiting for the owner;
    - each market's distance to its breakout, with a meter, and whether a trade fits;
    - a result curve and a bar per trade, drawn as SVG on the server;
    - the history, with how each trade ended.
    It refreshes itself every minute, uses a new look (gradients, glow, big numbers), and works with no JavaScript under the strict CSP.
  - **The `/trader/setup` plan** shows, for an amount and care level, each market's smallest trade value and risk at today's volatility, and the least money for each care level.
  - **Home page:** an auto-trader card.
  - **Telegram:** `/trader` gives the same in a few lines, and closed trades say "✅ WIN +… USD" or "❌ LOSS …" with the market's plain name.
- **Tests:**
  - `tests/trading/test_auto_trader_money.py` (7):
    - the limits from money, the money limit and the watch level;
    - a money trade from open to closed on the page's read model and the Telegram lines;
    - ASK mode waiting for the owner, then trading after approval;
    - a single heads-up;
    - settings replaced at once, and money above the equity refused.
  - The portal flow: off, then a plan, ASK started, then stopped.
  - The message texts, the bar chart and the TIME TO TRADE card.
  - Both new pages added to the accessibility audit.

## S23 — What to expect, and the market radar (2026-10-22)

- **Owner's request:** "continue with your idea and amaze me".
- **Built** (ADR-030 addendum 2):
  - **`research/library/expect.py`:** the auto-trader's own rule replayed on past hourly bars. It follows the live path:
    - the rule's state, with decisions at the close filled at the next open, at the ask and the bid;
    - the 3×ATR(20) stop, with gaps filled at the open;
    - whole-lot sizing, identical to `auto.size_for_max_loss`;
    - the per-trade limit, trades a day, open positions, the daily stop and the money.
    It sums up the result, trades a week, the win rate, the typical win and loss, the worst losing streak, the deepest drop, the time in a trade, the halves, each month, each market and the skipped signals. Two markets and a year of hours replay in about 0.1 s.
  - **Costs:**
    - the policy's fee for the venue (ASSUMPTION);
    - the venue's own median spread over the last day, from stored quotes (0.1 % ASSUMPTION when none);
    - 0.05 % slippage a fill (ASSUMPTION).
    Swap, delays and requotes are not modelled, and the page says so.
  - **`research.auto_history`** (every 6 hours): backfills the signal series to 390 days of hourly bars once, in pages of 1,000 (one backfill for the source they share).
  - **`/trader/setup`:** a full "What to expect" section with the plan: a result line with its zero line, a bar per month, each market, and "How this was tested". **`/trader`:** a short version for the mandate in force, with a link to the full one.
  - **Telegram `/trader`:** one more line, the past test of the settings in force ("HISTORICAL SIMULATION, not a promise").
  - **The market radar:** each market card on `/trader` draws the last 48 hours of the signal series as SVG on the server. Out of a trade it shows where the rule buys; in a trade, where it sells, the stop and the purchase. Each line has a legend entry and a direct label, and each hour a tooltip. In a trade the card says where it sells and where the stop is.
  - **Charts:**
    - three categorical colour slots, checked for colour-blind readers on both themes;
    - text in text colours, never the series colour;
    - 2 px lines, bars rounded at the data end and square at the baseline;
    - big figures without tabular digits.
- **Tests:**
  - `tests/unit/test_auto_expect.py` (50):
    - next-open fills and the rule exit;
    - the stop, and a gap through it;
    - costs on both sides;
    - waiting for a fresh breakout after a stop;
    - too big is skipped, never resized;
    - the money cap and what is left for another market;
    - trades a day, open trades, the daily stop;
    - 40 random cases where sizing equals the live auto-trader's;
    - nothing before an hour depends on later hours (three cut points);
    - the summary's every number.
  - `tests/trading/test_auto_expect_board.py` (4): the replay from the database with the plan and the measured spread; the Telegram line; too little history said plainly; the radar before and during a trade.
  - `tests/unit/test_charts.py` (3): legend, labels, escaping, the zero line and the rounded bars.
  - The portal flow with history: the plan's test, then the radar and the short test on the page.
  - The backfill job: 30 pages for the three Binance listings, and none once there is enough.
- **`make check`:** format, lint, mypy (207 files), secret scan, 799 tests passed (3 live/sandbox deselected).
- **Seen when rendering the pages** (headless Chromium, desktop and phone widths), and fixed before release:
  - the radar was too large in a single wide card;
  - the streak tile wrapped;
  - the intro said "12 months" while less history was stored.

## S24 — The total loss stop, and the plan to seek profit honestly (2026-10-22)

- **Owner's request:** "I agree, continue with that; help me with automated trading to get profit… do your job with planning".
- **Built** (ADR-030 addendum 3, migration `0013_mandate_total_stop`):
  - Mandates carry `total_loss_limit`: ten trades' worth from money, and the same for older mandates without one. It is immutable, and the status `STOPPED` is new.
  - **Entry check:** the auto-trader's losses so far under the mandate, plus a full loss on this trade and every open one, must stay within the limit.
  - **`stop_due`:** at the start of each cycle it ends a mandate STOPPED, with the amount in the reason and a Telegram message, once the losses leave no room for another trade and nothing is open. Open trades are never cut.
  - **The past-year replay** applies the same rule and ends where the auto-trader would have stopped itself.
  - **Shown on:** `/trader` (a "Stops for good after losing … · lost so far …" tile, and STOPPED ITSELF with the reason), the plan, Operations, and Telegram `/trader`.
- **The plan** (docs/03 §5): a fair competition of the library's rules, with out-of-sample checks and a multiple-testing correction; then a forward record before any switch of rule; then sizing to the evidence; real money only after gate G.
- **Tests:**
  - the limit counts the worst case of open trades, older mandates get ten trades' worth, and profits give room;
  - a full flow where a losing auto-trade leaves no room and the next cycle ends the mandate STOPPED, shown and reported;
  - the replay ends at the stop and skips an entry whose worst case could pass the limit;
  - the database keeps the limit immutable, accepts STOPPED, and refuses the downgrade while either is used;
  - the plan and Operations show the limit.
- **Checked by rendering** the page running and after it stopped itself. The reason was reworded so it says why it stopped: one more full loss would pass the limit.
- **`make check`:** format, lint, mypy (208 files), secret scan, 804 tests passed (3 live/sandbox deselected).

## S25 — The rule competition: a rule earns the auto-trader on unseen data, then forward (2026-10-22)

- **Owner's request:** "GO with mega idea" (the plan's step 3).
- **Built** (ADR-031, migration `0014_auto_competitions`):
  - **`research/library/compete.py`:** all 95 single-market rules of the library are replayed with the auto-trader's mechanics. They run on each auto-trader market, on hourly and daily bars, with the costs it would pay, and results are in R.
  - **The protocol:**
    1. Chosen on the earlier 60 %: at least 30 hourly or 16 daily trades, a positive mean and t ≥ 2.
    2. Checked on the unseen 40 %: positive, at double costs too, with a t-statistic above the Bonferroni threshold for the number of candidates.
    3. Forward from 2026-10-23, fixed before those bars existed. EARNED needs 30 hourly or 10 daily forward trades in profit.
  - Rules that cannot run on the bars are shown as N/A with the reason. Daily rules see calendar dates.
  - **The replay** (`expect.py`) takes a window end and precomputed signals, so each rule's indicators are computed once per series.
  - **`research.auto_competition`:** daily, or at once after a code change. It runs in a thread and appends to `auto_competitions` (append-only) with the code version, data end, trials, costs and the whole leaderboard.
  - **`/trader/competition`:**
    - the verdict and counts;
    - the auto-trader's own rule;
    - the top 25 and all results, each with its earlier, unseen, double-cost and forward numbers;
    - an R line with the start of the unseen part;
    - how it works, and the costs used.
    `/trader` and Telegram `/trader` add one verdict line.
  - **`md.median_spread`** is shared by the past-year test and the competition.
- **First results** (local check on public Binance prices):
  - **Hourly, the last year:** no candidate. Rules positive on the earlier part turned negative later.
  - **Daily bitcoin, 2018–2026:** 10 candidates. 9 stayed positive on the unseen 2023–2026 part and at double costs, but none reached t ≥ 2.58 (best 1.53): PROMISING.
  - **Daily gold:** none.
  - **Run times:** about 20 s for 190 hourly pairs and 6 s for 190 daily pairs.
- **Tests:**
  - `tests/unit/test_auto_compete.py` (8):
    - a real edge qualifies and noise does not;
    - the Bonferroni bar, and the split after the warm-up;
    - a broken rule is N/A and not a trial;
    - only a forward record earns the auto-trader;
    - the report is plain data;
    - the forward date is fixed.
  - `tests/trading/test_auto_competition_store.py` (2):
    - one append-only run a day, rerun after 20 hours as a new row;
    - costs from the venue's quotes, and daily calendar rules;
    - the page, `/trader` and the Telegram line.
  - Migration 0014 append-only and the downgrade refusal; the authz matrix and accessibility audit include `/trader/competition`.
- **Seen when rendering** with real prices: the number columns wrapped, so the headers were shortened and the cells kept on one line.
- **`make check`:** format, lint, mypy (211 files), secret scan, 815 tests passed (3 live/sandbox deselected).

## S26 — Gold: visible, explained, benchmarked (2026-10-22)

- **Owner's request:** "focus on gold trading … research why I lost and how I win … make it easy and visible".
- **Built** (ADR-032):
  - **`research/library/insight.py`:**
    - `diagnose` splits a replayed rule's result into what costs took (a second replay without costs), stop versus rule exits, the UTC session of entry, the daily trend at entry (causal), and the holding times of winners and losers, and writes plain findings;
    - `accumulate` is the benchmark: buy a fixed amount at the first price of each month and hold;
    - also `session_of` and `change_pct`.
  - **`reporting/gold.py` and `/gold`** (menu, READ_ACCOUNT):
    - price and moves, trend and volatility (regime), the session;
    - a one-year chart with the 50- and 200-day averages (smooth guide lines in `price_chart`);
    - one ounce's value and risk, and the least money for each care level;
    - the radar;
    - why the rule lost: tiles, findings, a session chart and table;
    - how to win (levers), gold's competition rows, the 1/3/5-year benchmark with a chart, and your gold trades.
    Telegram `/gold` gives the summary. Money axes no longer go below zero.
- **Seen with real gold prices** (local check, public Binance PAXG): before costs the hourly rule made about +21 R in a year. After costs it was −81 R at the general 0.6 % fee assumption and about +19 R at a 0.02 % spread with no fee: costs decide. Holding for the same year: +2.6 %. Buying 500 a month for 5 years: +73.6 %.
- **Tests:**
  - `tests/unit/test_gold_insight.py` (5): sessions, the split with findings, costs that turn a gain into a loss, the monthly benchmark, changes.
  - `tests/trading/test_gold_page.py` (3): the board from the database, the page's sections and charts, Telegram lines.
  - `/gold` is in the authz matrix and the accessibility audit.
- **`make check`:** format, lint, mypy (214 files), secret scan, 823 tests passed (3 live/sandbox deselected).

## S27 — The overnight swap is counted (2026-10-22)

- **Owner's request:** "yes continue with your mega idea": count the swap HFM charges for holding a CFD overnight.
- **Built** (ADR-032 addendum):
  - **Bridge version 3:** the symbol snapshot adds `swap_mode`, `swap_rollover3days` and `point`. They are additive, so an older bridge still works. The bridge connector keeps every symbol field, so they reach `instruments.metadata` without server changes.
  - **`venue_terms.swap_per_night`:** MT5's swap as a share of the price per night (points, money per lot, yearly rates), with the triple day. `funding_text` names the unit.
  - **`expect.nights` and the replay:** each rollover held over (about 21:00 UTC) is charged. Gold pays nothing at weekends and three nights on the triple day; bitcoin pays every night. `Trade.swap` keeps it.
  - **Where it counts:** the past-year test, the Gold research (`swap_r`, the "of which overnight swap" tile, and a finding when it amounts to 0.05 R or more), and the rule competition (doubled in its double-cost check).
  - **Every cost line** shows the swap, or "not reported yet".
- **Tests:**
  - `tests/unit/test_venue_swap.py` (4):
    - every MT5 mode;
    - an unknown mode is never guessed;
    - the nights charged (rollover, triple day, weekends, every night for crypto);
    - the replay's swap.
  - The bridge snapshot reports version 3 with the swap fields.
  - The Gold page counts and shows the swap, without a triple day for bitcoin.
  - The HFM health lines expect bridge v3.
- **`make check`:** format, lint, mypy (214 files), secret scan, 828 tests passed (3 live/sandbox deselected).
- **Released and deployed:**
  - **Server:** 5fb2ee1, with staging and production checks passed.
  - **Bridge version 3 on the Windows VM (2026-10-22 15:43 UTC):**
    - The version 2 file is kept as `cosmic_mt5_bridge.py.v2-20261022` for rollback.
    - The log shows "bridge hfm-demo 3 starting … demo account: True", and no warning after the start. The 502 warnings just before were production's restart during the release.
  - **First use:** the swap fields reach the instrument data at the next instrument sync, and the tests use them from then on.

## S28 — Size to the evidence: half size after a deep drop (2026-10-23)

- **Owner's request:** "continue", the plan's step 5.
- **Built** (ADR-030 addendum 4):
  - **`mandates.sizing`:** the auto-trader's closed results under the mandate, in order. It switches to half size from a drop of `HALVE_AFTER` (5) trades' worth below the best running result, and back to full below `RESTORE_BELOW` (2.5). The hysteresis means it does not flip with each trade.
  - **`sizing_now` and `results`** come from `post_trade_reviews` of the mandate's own trades; nothing new is stored.
  - **`auto._entry`** sizes to the per-trade limit × the factor, and the thesis, the summary and any skip reason say so. Every limit stays at the full amount.
  - **`tell_size_changes`** runs each cycle and sends one Telegram message per switch (dedup on the trade it happened after).
  - **Shown:** the "Trade size" tile and a "How it works" line on `/trader`, a Telegram `/trader` line while at half, and the rule in "What to expect".
  - **The replay** (`expect.Limits.halve_after` / `restore_below`) applies the same rule.
- **Tests:**
  - the rule with its hysteresis;
  - entries sized to half when the rule says so;
  - the switch told once and shown;
  - the replay sizing the next trade at half after a loss.
- **`make check`:** format, lint, mypy (214 files), secret scan, 832 tests passed (3 live/sandbox deselected).

## S29 — Ideas from the research, judged forward only; the forward race (2026-10-23)

- **Owner's request:** "add all your advanced idea and make me wealthy … continue".
- **Built** (ADR-031 addendum):
  - **`research/library/hypotheses.py`:** four ideas registered 2026-10-23, each a published rule with a causal entry filter:
    - Turtle with the daily trend up;
    - Turtle in London and New York hours;
    - Turtle with both;
    - Keltner with the trend.
    `filtered` enters only on a fresh flat → long change the filter allows; exits are the rule's.
  - **`compete.compete(..., ideas=)`:** tier IDEA, not counted as trials. An idea earns the auto-trader only with a forward record of 30 hourly or 10 daily trades, positive, with t ≥ 2. Hourly-only ideas are skipped on daily bars, and each entry keeps its forward R line.
  - **`compete_store.IDEA_POOL`** is part of the daily run; the code version includes `hypotheses.py`.
  - **`/trader/competition`:** "The forward test: the race on new prices" (everything with forward trades, best first, with forward lines) and "Ideas from the research" (forward numbers, plus the earlier ones marked "seen before it was registered"). Ideas are kept out of the main leaderboard.
- **Tests:**
  - `tests/unit/test_research_ideas.py` (4):
    - filtered entries, with exits as the rule's;
    - every idea causal at two cut points;
    - the trend filter on hourly and daily bars, and the hourly-only refusal;
    - forward-only judging.
  - The stored run and the page include the ideas and the race.
- **`make check`:** format, lint, mypy (215 files), secret scan, 836 tests passed (3 live/sandbox deselected).
- **Honesty:** no idea or rule has earned the auto-trader yet. The race needs weeks of new prices before any result means anything.

## S30 — "Should I take this trade?": a decision guide on every proposal to buy (2026-10-23)

- **Owner's request:** "if he gets information, in which way he can decide to trade … easy to trade small amounts many times by recommendation and automated trading".
- **Built** (ADR-033):
  - **`reporting/decision.py`:** six signs, each FINE, CARE, AGAINST or UNKNOWN:
    - evidence (the competition's verdict on the rule and market);
    - trend (close against its 50-day average);
    - time (the session);
    - cost now (spread against its median over the last day);
    - cost of this trade in R (spread, two fees and two slippages against the stop's risk);
    - size (loss at the stop as a share of equity).
    The summary counts them, and says "a demo test, not a bet" while the rule has no proven edge.
  - **Shown:** on the approval page ("Should I take this trade?") and under the Telegram card, for proposals still to be decided. An exit has none. It is read-only.
- **Tests:**
  - `tests/trading/test_decision_guide.py` (5):
    - six signs on an ask-first proposal;
    - a rejected rule is evidence against;
    - the cost and size thresholds;
    - no guide for an exit;
    - the approval page.
  - The Telegram card carries the guide.
- **`make check`:** format, lint, mypy (216 files), secret scan, 841 tests passed (3 live/sandbox deselected).

## S31 — The morning briefing in the daily report (2026-10-23)

- **Owner's request:** "continue".
- **Built:**
  - **`daily.briefing_lines`** closes the daily Telegram report with "Auto-trader and research":
    - the `/trader` head, money and result lines;
    - the rule competition's verdict line;
    - the forward race leader;
    - gold's first `/gold` lines.
    A part that fails is logged and replaced by a note, and the report still goes out.
- **Checked:**
  - the total loss stop and the half-size rule count each auto-trade once: only intents with a filled entry get a post-trade review, so exits are never counted twice.
- **Tests:** the briefing is in the report, and a failing part leaves the rest of it intact.
- **`make check`:** format, lint, mypy (216 files), secret scan, 842 tests passed (3 live/sandbox deselected).

## S32 — Code review of the session's work, and the fixes (2026-10-23)

- **Owner's request:** "continue" (ultracode). The goal: make the new trading code "perfect" before more is built on it.
- **Review** (a workflow over `git diff 3fe6f46..HEAD`): four reviewers covered trading safety, the replay and research engines, the pages and security, and the database, worker and bridge. One skeptic per area then re-checked every finding against the code. 22 findings were reported; 13 were confirmed and 9 rejected (dust that cannot occur on these venues, a definitional session choice, a page-time efficiency note, a pre-existing Telegram gap, run times far below the job lease, and others).
- **Fixed:**
  1. **Entries not filled yet count as open** in `mandates.usage()`, for the open limit, the total stop's worst case and the money cap; `stop_due` waits for them. Two markets breaking out together can no longer overshoot the total loss limit.
  2. **The grant's audit** records the stored total loss limit.
  3. **The rule competition's backtest** uses only bars before the forward date, with a fixed split. Forward trades are never counted twice, and the hourly forward record always starts at 2026-10-23.
  4. **Gold's weekend close** is respected by every replay; a stop crossed over the weekend fills at the reopening.
  5. **The trend at entry** in the Gold research is off by one day no more.
  6. **The double-cost stress** never doubles a swap credit.
  7. **Weekend spread:** the last stored quotes are used, not the 0.1 % assumption.
  8. **The competition's code version** covers all the code it depends on.
  9. **The decision guide's evidence** is for the proposal's own market and bars, never another market's.
  10. **A stale price** is never the decision guide's "cost now".
  11. **"See the whole test"** replays the mandate's own markets.
  12. **Telegram `/trader` and `/gold`** need READ_ACCOUNT.
- **Tests:**
  - `tests/unit/test_review_fixes_replay.py` (4): the backtest never sees forward bars; a credit is not doubled; the weekend close and the reopening gap; the trend at entry without the day's bar.
  - `tests/trading/test_review_fixes_trading.py` (6): entries not filled count as open; the audit's limit; evidence for this market and these bars only; a stale price; a closed market keeps its spread; the whole-test link.
  - The Telegram permission check is in `test_a_linked_user_without_the_capability_is_refused`.
- **Second check of the fixes** (a workflow: one reviewer per question). It confirmed 10 of the 12 fixes, and found 2 incomplete and 5 small regressions. All are fixed:
  - **A waiting entry** counts by its unfilled part, so a manual position in the same market cannot hide it from the money cap.
  - **The decision guide** never borrows a verdict, not even from a market on the same prices.
  - **The spread fallback** uses only the past week's quotes and is called "recent", not "the last day".
  - **The weekend close and the swap rollover** follow New York time, so they are right in winter too. The pinned image has the time-zone data.
  - **Refusals and pages** say "open or waiting to fill".
  - **The competition's pre-forward history** is loaded by date.
- **More tests:**
  - a waiting entry beside a manual position;
  - New York time in summer and winter;
  - quotes older than a week are not used.
- **`make check`:** format, lint, mypy (216 files), secret scan, 854 tests passed (3 live/sandbox deselected).

## S33 — The real fee, measured from the owner's own fills (2026-10-24)

- **Owner's request:** "continue". The owner's pending step is to confirm the HFM commission (B-08); until then every test assumes 0.6 % a side.
- **Built:**
  - **`trading/measured.py`:** the commission per side that each real venue charged on the owner's entry fills. It skips exit fills (they carry the CFD swap) and the paper simulator (it echoes the policy), and values a base-asset fee at the fill price. `advice` gives the value to set when the measured fee is 5 bps or more from the policy's.
  - **Shown on:** Risk policy → Fee per venue ("Measured from your own trades"), the Gold page's first lever, and the morning briefing.
  - **Read-only:** changing the fee stays the owner's decision, with a fresh code.
- **Tests:**
  - the MT5 emulator's 0.03 USD on 4,100.30 measures 0.07 bps;
  - paper fills are never measured;
  - the advice and its 5 bps threshold;
  - the Risk page in both cases.
- **`make check`:** format, lint, mypy (217 files), secret scan, 858 tests passed (3 live/sandbox deselected).


## S34 — Truthful readiness: gates as evaluated, gate I the owner's own (2026-10-24)

- **Owner's request (2026-10-24):** start real money with $100 on gold one day, rehearsed exactly as real first; the real-money link may be built but stays off (the $100 Gold Plan, S34–S40).
- **Found by the review of that plan:**
  - `live_preconditions` read the latest *recorded* gate rows, so a PASSED record counted even where the build blocks the gate;
  - `cosmic golive record` could record gate I, the owner's activation, as a CLI actor;
  - gates A and E were forced BLOCKED with the B-02 reason, although the VM has been verified since 2026-10-19 and the restart test passed on 2026-10-20;
  - gate C never showed the sandbox contract runs.
- **Built (ADR-010 addendum, 2026-10-24):**
  - live modes require every gate PASSED **as `golive.readiness` evaluates it** (with the given time, or the database clock for the banner);
  - `record_gate` refuses gate I; the CLI offers A–H only;
  - A and E pass on recorded evidence like B, D and H;
  - gate C lists each sandbox adapter's latest contract run, and the last PASSED one when a later run failed; it stays BLOCKED while no live adapter exists;
  - `/golive` shows for each gate what passes it; the runbook (docs/15 §13) gives the commands and the evidence for A, B, D, E and H.
- **Tests:** `tests/e2e/test_command_center.py`:
  - recording A–H PASSED passes A, B, D, E and H, while C, F, G and I stay BLOCKED and appear in the unmet preconditions;
  - recording gate I is refused;
  - gate C shows a FAILED latest run and the last PASSED one.
- **`make check`:** format, lint, mypy (217 files), secret scan, 859 tests passed (3 live/sandbox deselected).
- **Not done:** no gate evidence is recorded on production yet; that is an operator step (runbook §13). Live trading stays DISABLED.

## S35 — Daily rules, and one auto-trader per account (2026-10-24)

- **Why (the $100 Gold Plan):** at $100, hourly rules lose to costs (Turtle 20/10 on Binance gold: −16.5 R over 400 days at the published 0.1 % fee), while several daily trend rules were positive on 2021–2026 gold after costs. A $100 rehearsal needs its own account beside the HFM demo trader, but every page assumed one auto-trader.
- **Built (ADR-030 addendum 5; ADR-031 addendum):**
  - **Migration `0015_mandate_timeframe`:** `trading_mandates.timeframe` may be `1d`; the downgrade refuses while a daily mandate exists.
  - **Rules per timeframe** (`RULES_BY_TIMEFRAME`): hourly Turtle 20/10; daily Turtle 20/10, Turtle 55/20, the 200-day average and the trend ensemble. These are forward tests on simulated money, not EARNED.
  - **The daily auto-trader:**
    - decides once a day on the bar completed at 00:00 UTC, with the whole daily history loaded (400 bars needed; a bar closed more than 26 h ago is ignored);
    - is automatic only, and lasts 90 days by default;
    - without a fresh price, waits rather than lose the day's signal (the owner is told once);
    - retries an exit that did not happen each hour, with one notice per bar;
    - acts on a buy signal once per account, whatever the mandate, so a renewal never trades it twice;
    - is refused on gold CFDs, which close at weekends, until weekend handling (S38).
  - **Reminders:** 7 days and 1 day before a mandate ends; a failed reminder never stops a trading cycle.
  - **One auto-trader per account:**
    - `/trader?account=…` with a switcher, also shown when the page's own auto-trader is off;
    - setup chooses the account, the bars and the rule;
    - start replaces only that account's mandate;
    - stop names the account, with "Stop all";
    - Telegram `/trader`, the morning briefing and the home card show each one;
    - an off board shows and prefills what it last ran.
  - **Timeframe-aware text and figures:**
    - the radar and the heads-up appear for breakout rules only;
    - "what one trade risks" uses the ATR of the mandate's bars and the 0.5 % stop-limit gap where the venue has no market stop;
    - "what to expect" replays a daily rule over up to five years;
    - proposals, the decision guide and the competition verdict name the bars.
- **Reviews:**
  - the plan review's corrections were applied;
  - a page review found 7 problems (the leaderboard and back links lost the account, the off page hid the running trader, the gold page mixed daily and hourly risk, the home card showed one trader, a wrong daily note), all fixed;
  - a trading-logic review found 2 (a Friday gold signal lost at weekends; a renewal could trade the same signal again), both fixed with tests.
- **Visual check:** headless Chromium screenshots of the trader page with two auto-traders, the setup page and the phone layout.
- **Tests:** `tests/trading/test_auto_trader_daily.py` (12):
  - rules per timeframe and the immutable timeframe;
  - a daily breakout handled once; a stale series ignored; waiting for a fresh price;
  - the exit retry key; the daily ATR and the stop gap; the multi-year replay; the reminders;
  - the renewal guard; the weekend refusal;
  - two auto-traders on two accounts through the portal.

  Also a migration test for 0015, and updated portal tests.
- **`make check`:** format, lint, mypy (218 files), secret scan, 872 tests passed (3 live/sandbox deselected).
- **Not done:**
  - The rule exit of a trade still open when its mandate ends is not taken over (the stop stays; the reminders ask for renewal first). Since S49 a renewal takes it over; since S50 the owner is told when the rule of a run not renewed says sell.
  - The weekend handling for gold CFDs on daily bars (S38).
  - The release: deploying needs the owner's permission (the classifier blocks production deploys from this session).

## S36 — Sized within every risk limit, and the policy in the replay (2026-10-24)

- **Why (the $100 Gold Plan review):** the auto-trader sized a trade by the mandate alone. At $100 a Bold trade was sized and then refused by R06 (50 % of equity in one market) and R07 (cash). R08's loss streak counts the account's latest losses with no reset, so after four losses in a row every new trade is refused until the owner changes the policy. The replay showed none of this.
- **Built (ADR-030 addendum 6):**
  - **`trading/risk/rooms.py`:** the room R05, R06, R07 and R19 leave for a new buy, with the checks' own arithmetic; R19 now uses the shared `open_risk`.
  - **Sizing:** the auto-trader sizes within the smallest room. A skip names the binding limit, or R19 ("the loss if every stop is hit").
  - **The replay applies the account's policy:**
    - the share of equity in one market;
    - the loss if every stop is hit;
    - R08's loss streak, drawdown from the equity's high and daily loss.
    It works on the account's equity, marks open trades at the fill moment (never at a close still to come), and skips with `RISK_ROOM` or `RISK_LOSS_LIMITS`. Without the account's equity or money, the equity-based limits are not applied.
  - **The setup page** says where the risk policy binds before the auto-trader's own limits at that money, and which value lets its own limits decide. Its own stop for good needs at least 14 full losses in a row: 5 full, then half size.
- **Review:**
  - Rooms and R19 were confirmed against the checks term by term.
  - 3 findings were fixed with tests: the replay's look-ahead at a fill; equity-based limits applied to an unknown equity; over-optimistic advice for the loss streak and the drawdown.
  - Found by the access tests while building S37: the setup page's notes needed a valid risk policy (409 without one); without one there are no notes, and every check still fails closed.
- **Tests:**
  - `tests/unit/test_risk_rooms.py` (4) and `tests/unit/test_policy_notes.py` (3);
  - in `tests/unit/test_auto_expect.py`: the loss-streak freeze, the share-of-equity cap, marking at the open, no equity limits without equity, and sizing parity with the R19 cap;
  - `test_a_100_dollar_account_is_sized_within_every_risk_limit` (a $100 Bold trade approved within 50.00).
- **Owner's decision (Risk policy, with a code; it applies to the whole account).** At $100 and Bold with the default policy:

  | Setting | Now | Effect | Value that lets the auto-trader decide |
  |---|---|---|---|
  | max_concentration_pct | 50 % | caps a trade at 50.00 | — |
  | max_open_risk_pct | 3 % | one trade at a time | 4 % for two |
  | max_loss_streak | 4 | freezes the account after 4 losses in a row | 14 or more |
  | max_drawdown_pct | 10 % | stops before the 20 % stop for good | 25 % |

## S37 — Start with $100: the rehearsal, treated like the real $100 (2026-10-24)

- **Owner's request (2026-10-24):** "I start with $100 … consider the demo balance like real". The owner chose Bold ($2 a trade), a stop for good after losing $20, and both venues compared.
- **Built (ADR-035):**
  - **`/rehearsal`, "Start with $100" in the menu:**
    1. where $100 can trade gold: each venue's smallest trade and its loss at a daily stop (Binance PAXG and HFM from their own rules; HFM Cent from published terms, said so);
    2. each daily rule replayed with $100 at Bold on Binance gold, with costs and the risk policy;
    3. possible years from 4,000 resamples of the replayed trades;
    4. how money grows with a monthly addition, beside gold bought and held;
    5. what must be ready (the Binance fee, the market in the policy, where the policy binds first);
    6. the start form.
  - **`POST /rehearsal/start`** (owner's fresh code, APPROVE_TRADE and ADMINISTER_SYSTEM):
    - opens the PAPER account `rehearsal-binance-100` with 100 USDT, funded on the paper venue too and reconciled at once;
    - grants a daily, automatic, Bold mandate on PAXG for 90 days, with the stop for good fixed at $80;
    - renews from the money now; a rehearsal that stopped itself is not restarted;
    - refuses until a Binance fee is set.
  - **Telegram `/rehearsal`;** library proposals skip rehearsal accounts.
- **Review (one reviewer, all fixed with tests):**
  - the Operations form and `/ops/paper-account` could bypass the floor; now one guard in `mandates.grant` covers every path;
  - renewal could fail on rounding;
  - renewal with a trade still open could fall below $80; it now waits;
  - the possible years were skewed by a stopped replay;
  - the gold column bought one extra month;
  - the reminder pointed to the wrong page;
  - without a valid policy, the page hid a running rehearsal.
  - The access tests also found that the page needed a valid risk policy; it now says to set one.
- **Visual check:** headless Chromium screenshot of the full page with a gold history.
- **Tests:** `tests/trading/test_rehearsal.py`:
  - starting needs the fee and a fresh code;
  - the terms (daily, Bold, 2.00, floor 80, 90 days);
  - a second start refused;
  - renewal from the money now; renewal rounding down, and waiting for an open trade;
  - a STOPPED rehearsal not restarted;
  - only the rehearsal may grant on its account;
  - the reminder path; the resample and growth arithmetic;
  - the page end to end.

  The page is also in the access matrix, the viewer-refused posts and the accessibility audit.
- **Not done:** the 55-year World Bank series in the system (licence and documented URL first); the HFM Cent side (S38).
- **Owner steps:**
  1. Risk policy → venue_fees → `BINANCE=10`, with your code.
  2. Optionally, the policy values the page suggests.
  3. Start with $100 → choose a rule → Start, with your code.

## S38 — The HFM Cent side of the comparison, and weekend handling (2026-10-27)

- **Why:** the owner chose to compare Binance gold with an HFM Cent account. No public source confirms an HFM Cent demo, and daily rules on gold CFDs lost Friday's signals, so they were refused (S35).
- **Built (ADR-035 addendum; ADR-031 addendum):**
  - **Weekend handling**, the same in the live auto-trader and the replay:
    - a daily rule on a market closed at weekends decides on the last bar in session, against the one before it;
    - it acts at the next bar in session (Monday 00:00 UTC for Friday's bar), and the signal waits unconsumed until then;
    - daily rules on gold CFDs are allowed again.
  - **"HFM Cent terms" market:** the paper-only venue `HFM_CENT` and instrument `CFD:XAU/USD@HFM_CENT`.
    - Prices are HFM's own XAUUSD quotes, stored by the bridge sync under the derived source `hfm_cent_terms`.
    - Lots come from `CentTermsConnector`: volumes ÷ 100, so 0.01 oz.
    - The cross-venue check excludes the mirror (`MIRROR_VENUES`) and compares HFM's gold with PAXG instead.
  - **Swap on paper:** the paper venue charges a CFD's overnight swap at the exit, as HFM books it, with nights from `venue_terms.nights` (now shared with the replay). Only a swap paid is simulated, never a credit.
  - **Two rehearsals:** `rehearsal-hfm-cent-100` (100 USD) beside `rehearsal-binance-100`, from the same page. Each needs its venue's fee set and its market allowed by the policy. The page shows both rehearsals and the chosen rule on both venues, with their costs; Telegram `/rehearsal` lists both.
- **Tests:**
  - `test_a_friday_signal_on_gold_waits_for_monday_as_the_replay_fills_it` and `test_the_replay_keeps_a_flip_that_happens_at_the_weekend`;
  - `tests/trading/test_paper_cfd_swap.py` (the swap at a paper exit; none without terms, for a credit or on spot);
  - the bridge sync test (the Cent price mirrored, lots 0.01, never a second price);
  - `test_the_hfm_cent_rehearsal_needs_its_fee_and_its_market_allowed`.
- **Review (one reviewer, all fixed with tests):**
  - the replay's session rule had reached hourly gold replays; it is now daily only;
  - a step-3 test was deleted by mistake and is restored;
  - a window's first decision could compare across the weekend;
  - a failing mirror could stop HFM's own sync; it now runs after, guarded;
  - a swap test passed for the wrong reason.
- **Owner steps (Risk policy, with your code):**
  1. `venue_fees` `HFM_CENT=0`.
  2. Add `CFD:XAU/USD@HFM_CENT`, `HFM_CENT` and `USD` to the lists.
  3. Start with $100 → HFM Cent terms → Start.
- **Not done:**
  - **A real Cent demo (path A):** a second MT5 terminal and bridge, and the USC currency handling. It waits for the owner to check whether a Cent demo can be opened.
  - **The spread:** the Cent line uses the standard account's prices, so its spread may be narrower than a real Cent account's.

## S39 — The two side by side, and fill prices measured (2026-10-28)

- **Why:** the owner chose "both, then compare". Every replay assumed 5 bps of slippage a fill, and nothing checked that the auto-trader does what its replay says.
- **Built (ADR-035 addendum, 2026-10-28):**
  - **Fill prices** (`trading/measured.py`):
    - Each filled entry is compared with the ask, and each exit with the bid, when it was sent:
      - the quote its pre-submission check recorded, when it went out within 60 s;
      - otherwise the stored quote at the time it was sent.
      A stop is compared with its stop price.
    - Results are in bps by venue and environment: the typical (median) and the worst fill of orders sent at the market price, with stops apart against their stop price. Paper, demo or test network, and real money are kept apart; virtual money is labelled SIMULATED.
    - A sentence appears when the median is 5 bps or more from the replays' assumption: from real money, or from a demo only when it is worse. It covers the last 90 days and is shown on Risk policy and in the morning briefing.
  - **Costs paid:** the commission and swap of an account's fills, in its quote currency (`measured.costs_paid`).
  - **Start with $100 → "The two side by side":** for each started rehearsal:
    - money now, trades, the result;
    - fees and swap paid, its fill prices;
    - signals it could not act on, with the latest reason;
    - its replay over the same days, trade by trade (the day each opened, how and when it closed), with a plain verdict. The replay starts R08 from the account's history then.
  - **The replay of a mandate's own days:** `autotrader.expectation(since=, until=)`, starting at the open of the last bar completed at the grant (`rehearsal.replay_from`). It keeps the replay's trades (`Expectation.trades`).
- **Tests:**
  - `tests/trading/test_fill_quality.py`:
    - the bps arithmetic;
    - paper apart from a real venue;
    - advice only from 3 or more real fills;
    - a paper entry measured against the ask its check recorded;
    - an order sent late measured against the stored price when it went out, and left out without one;
    - the Risk page and the briefing, with demo fills labelled SIMULATED;
    - the replays' 5 bps equal to `expect.SLIPPAGE`.
  - `tests/trading/test_rehearsal.py`:
    - where the replay of a mandate's days starts (daily, hourly, weekends);
    - trades matched by day, and the verdicts;
    - the page.
  - `test_a_live_daily_trade_and_its_replay_open_on_the_same_day`: the live auto-trader and its replay on the same bars. The live trade is "waiting" until the next bar, then matched.
  - `test_a_trade_made_just_before_a_renewal_still_matches_its_replay`;
  - `test_a_replay_of_a_renewed_mandate_starts_from_the_accounts_r08_history` and `test_r08_history_is_read_as_it_stood_when_a_mandate_began`;
  - `test_a_stop_that_gaps_is_shown_apart_and_never_judges_the_replays`, `test_the_rehearsal_and_its_replay_are_matched_by_day_and_exit`, `test_what_the_replay_cannot_have_yet_is_waiting_not_a_difference`.
- **Review (one reviewer; seven findings, all fixed):**
  - demo and test-network fills were shown as real: they are now SIMULATED, with advice only when worse;
  - an exit held while its stop was cancelled was measured from its approval: it is now measured from when it was sent;
  - the paper wording was wrong (the simulator's own 5 bps);
  - the verdict overclaimed: sizes differ too;
  - the table mixed time windows: each is now labelled;
  - a renewal on the day of a buy showed a false mismatch;
  - the Risk page and briefing read every order ever made: now the last 90 days, and only the market part of each check.
- **Review workflow (four reviewers, each finding checked by a second agent):** nine findings, all fixed with tests:
  - a stop that gaps through its level counted as slippage, though the replays already allow for the gap: stops are now shown apart and never set the advice;
  - after a renewal the replay started R08 from nothing: it now starts from the account's high and losing streak when the mandate began (`context_builder.loss_history`, shared with the live checks);
  - the verdict compared only the days trades opened: exits are compared too, and a renewal that changed the rule no longer counts the old rule's trade;
  - paper trades were called "Live" on the page: now "the rehearsal";
  - rows that differ were not highlighted (the style applied to another table only);
  - the 90-day window used the send time: it now uses the fill time;
  - the briefing's demo line lacked its SIMULATED label;
  - the paper-entry test passed whichever price it used: it now proves the check's price is used.
- **Owner steps:** none new. The S38 steps still apply, then start both rehearsals.
- **Not done:** the real-money link for Binance, built and kept off (S40).


## S40a — The real-money locks, built and kept off (2026-10-30)

- **Why:** the owner chose to have the real-money link built now and kept off until every gate passes and they switch it on (the $100 Gold Plan). Until now, only the empty `LIVE_ADAPTERS` and the server's mode cap stood in the way.
- **Built (ADR-034):**
  - **Seven locks, each opened only by the owner:**
    1. `LIVE_ADAPTERS` (a reviewed code constant, still empty);
    2. `COSMIC_MAX_AUTHORITY_MODE`;
    3. the new `COSMIC_LIVE_VENUES` (empty by default; only `BINANCE` is accepted);
    4. gates A–H;
    5. a passing key check under 24 hours old;
    6. the owner's live activation;
    7. each trade approved with a code.
    The go-live page lists each lock with its state now.
  - **Migration 0016 (`live_locks`):**
    - `live_activations` gets its terms: one LIVE account, a cap, an expiry within 90 days, a reason. It accepts only an active owner, and rows are immutable;
    - revocations are kept in their own append-only table;
    - `live_key_checks` is append-only;
    - a LIVE account is accepted only with the adapter `binance_spot_live`;
    - `orders_live_guard`: no real-money order without the account's activation, active by the later of the order's time and the database's clock, and within its cap. Only a reduce-only SELL exit or stop goes without one. A real-money order's account, purpose, side, size and price never change;
    - `trading_mandates_guard` is extended: a real-money mandate only asks, within the activation;
    - the downgrade refuses while any of them exists.
  - **`trading/live.py`:**
    - the key judge: spot trading and reading on, IP-restricted, every other permission off; a missing flag fails;
    - its record: a failure ends every activation and opens a security incident. It is CRITICAL when real money was active, and a WARNING while setting up, so the rehearsals go on;
    - activate: portal only, a fresh code, refused by each lock on its own, the cap at most 1,000;
    - revoke (the owner's needs no fresh code), revoke all, and `enforce`, which also ends an activation whose key check is more than 26 hours overdue;
    - every change takes the authority row's lock first.
  - **Authority:**
    - a live mode needs `COSMIC_LIVE_VENUES` and records its activation;
    - when the last activation ends (revoke, expiry at the worker's sweep, emergency stop, failed key check), the mode falls back to `PAPER_TRADING`;
    - the emergency stop ends every activation.
  - **At startup** a live mode without `COSMIC_LIVE_VENUES` is forced to PROTECTION_ONLY, like a cap breach.
  - **Gate I** passes only while an activation is active and A–H pass; it stays BLOCKED without a live adapter.
  - **R20 "Real-money activation":** on a LIVE account, the activation, and holdings plus resting entries plus the order within its cap. It passes for paper and demo.
  - **Facts checked on 2026-10-30** in `docs/integrations/binance-spot-api.md` §12: `apiRestrictions`, the account and book-ticker responses, and Binance's key rules.
- **Tests:**
  - `tests/trading/test_live_locks.py`, 17 tests: each lock refuses on its own; the database guards on orders, mandates and activations; append-only records; every way an activation ends takes the live mode with it; R20; the page.
  - `test_live_locks_hold_in_the_database_and_block_downgrade`.
  - Existing tests updated for R20 and for the live adapter's name.
- **Review (one reviewer; six findings, all fixed with tests):**
  - **The worst:** the order guard trusted the reduce-only flag, so a BUY marked reduce-only (allowed by the orders CHECK for non-entries) went through without an activation. The test of "an exit is never blocked" cloned a BUY and passed for that wrong reason. Now only a reduce-only SELL exit or stop is exempt, and the test proves both sides.
  - an activation could slip past a concurrent emergency stop, and the stop skipped one stamped a little ahead: activation now takes the authority lock and is refused while stopped, and the stop ends every unrevoked activation;
  - a revoke racing a mode change could leave a live mode on until the next sweep: a revoke now takes the authority lock first;
  - the guards trusted the application's timestamps: they now use the later of those and the database's clock;
  - the banner would have shown live trading as DISABLED during a live mode: the venue lock is now also held at startup;
  - a real-money order could be moved to another account: the guard checks the old account too.
  - **Accepted limits (ADR-034):** the database cap is per order (R20 holds the total), and the key check is per adapter until S40b ties it to the key's fingerprint.
  - **From the same review:** an overdue key check now ends an activation.
- **Unchanged at runtime:** live trading stays DISABLED, gate I stays BLOCKED, and `LIVE_ADAPTERS` is empty.
- **New blocker B-11:** a fixed outgoing address for an IP-restricted key, to check with the owner's internet provider.
- **Next (S40b):**
  - the live adapter (production base through a separate constructor);
  - the key check read from the venue and `cosmic broker binance-live-check`;
  - the R09 live-quote path;
  - LIVE accounts with managed capital;
  - ASK mandates on a LIVE account with day-long proposals;
  - the contract version hashing the adapter code;
  - the activation and revoke forms on the go-live page.

## S40b — The real-money link itself, built and kept off (2026-10-30)

- **Why:** the second part of ADR-034: the adapter that would trade real money, the key check read from Binance, and real-money checks priced by the venue's own book.
- **Built:**
  - **The live link** (`BinanceSpotAdapter.live_link`): the same code as the test network on `https://api.binance.com/api`, which the ordinary constructor still refuses; named `binance_spot_live`, `is_live=True`.
  - **`binance_live.build`** (worker only): None unless locks 1 and 3 are open and both live key files are mounted. No deployment mounts them, and a test checks every compose file.
  - **The registry** serves a LIVE account only through the link, while `LIVE_ADAPTERS` registers it at the moment of the lookup.
  - **The contract version** hashes every file that shapes what is sent (`binance-spot-contract-1+<hash>`). An activation needs a PASSED test-network run of the code as it is. A release that changes it ends real money until the contract is run again.
  - **The key read from Binance:**
    - `key_restrictions` reads `/sapi/v1/account/apiRestrictions`, on the real host only;
    - the worker's `live.key_check` records it every 6 hours, and an unreadable key raises a WARNING;
    - `cosmic broker binance-live-check` does it by hand: read-only, recorded, and only while `COSMIC_LIVE_VENUES` names BINANCE.
  - **R09 for real money:**
    - the execution reads the venue's book through the link just before the pre-submission check, outside any transaction;
    - it is the check's bid and ask: LIVE within a 2-second round trip, at most 5 seconds old after the check's locks, and never stamped in the future;
    - just before sending, an entry whose price is now older than 5 seconds is not sent;
    - it is kept in the check's record and not stored as a quote (a change from the plan, ADR-034);
    - stored public quotes never count for real money (holdings keep their stored marks, as for every account).
  - **Docs:** ADR-034's three parts, the ADR-025 addendum, the LIVE label's definition, the config message, the go-live banner, and the owner's path in the runbook (§13.1).
- **Tests:**
  - `tests/trading/test_live_link.py`, 9 tests: the live link only through its constructor; the build only with locks 1 and 3 open and a usable key; the contract hash over each file; the key read on the real host only, with no order placed; the book, its round trip and unusable answers; R09 priced only by the book just read (5 s, 2 s, the future, UNAVAILABLE); the price age at sending; the registry; the worker's key check and its warning.
  - `test_a_release_that_changes_the_adapter_ends_real_money`, `test_a_failed_step_of_the_live_check_fails_the_record`.
  - `test_no_service_holds_a_real_money_key_or_names_a_live_venue`.
  - The lock tests now open lock 1 with a PASSED contract run of this code, and prove that a run of other code does not count.
- **Review (one reviewer):** nothing let the link be built or used, or a real order be sent, with a lock shut. Two medium and several small findings, all fixed with tests:
  - the contract counted only at activation, so a release that changed the adapter kept trading on code without a run: R20 now refuses at once and the sweep ends every activation;
  - the 5-second limit could never trip, because the age was measured before the check's locks and nothing measured the wait before sending: the age is now taken after the locks, and the price is checked again just before sending;
  - the manual check could record a pass while printing FAILED: a failed step now fails the record;
  - the hash covered only the adapter: it now covers the contract, the broker types and the number formatting too;
  - a NaN or infinite price, or a garbled answer, raised instead of refusing: refused now, and exits no longer read the book;
  - negative ages and round trips passed: refused now, and the round trip is timed by a monotonic clock;
  - docs that overstated (the book as the only price; every service), and an unreadable-key warning that never cleared.
  - **Left for S40c:** an end-to-end test of a real-money entry through `execute_intent`. It needs a LIVE proposal, which the S40c auto-trader creates.
- **Unchanged at runtime:** `LIVE_ADAPTERS` is empty, so the link is never built, the key check does nothing, and live trading stays DISABLED.
- **Owner step when the contract matters:** the test-network contract run of 2026-10-15 was for older code. Real money will need a new run of the deployed code (runbook §13.1, step 5). Nothing else changes for the test-network account.
- **Next (S40c):**
  - LIVE accounts with managed capital and their key's fingerprint;
  - the activation and revoke forms;
  - ASK mandates on a LIVE account, ended with the activation, with day-long proposals priced by the venue's book.

## S40c-1 — The real-money account and the owner's controls, kept off (2026-10-30)

- **Why:** the third part of ADR-034: the account real money would use, and the owner's own buttons to activate and end it.
- **Built:**
  - **Opening the account** (`live.open_live_account`, worker job `trading.open_live_account`):
    - it opens from Binance's balances through the live link, after a PASSED contract run of this code and a passing check, under 24 hours old, of the very key the worker holds;
    - it manages at most 1,000 USDT, chosen by the owner, and books the rest of the assets it trades as unmanaged; reconciliation compares only those and ignores trades from before the opening, as on the test network;
    - its alias starts with `real-`;
    - a refusal becomes an incident, and nothing is created.
  - **Migration 0017:**
    - `accounts.key_fingerprint`, required for a LIVE account and fixed for its life;
    - at most one active LIVE account;
    - a LIVE account stays LIVE;
    - the downgrade refuses while one exists.
  - **Lock 5 per account:** an activation counts only a check of the account's own key, and a failed check ends only the activations of the account whose key failed. The worker trades an account only with that key; a rotated key ends its activation at the next check.
  - **The go-live page controls:**
    - "Open the real-money account" (owner, fresh code), shown and accepted only once locks 1 and 3 are open;
    - "Activate real money" (owner, fresh code), refused with every reason unless each lock before it is open;
    - "End real money now" (owner or emergency-stop holder, no fresh code).
- **Tests:**
  - `tests/trading/test_live_account.py`, 6 tests:
    - the account opens only after every check before it, with all of the Binance account accounted for and its key fixed;
    - lock 5 counts only the account's own key, and only that key may trade it;
    - the worker opens it only through the live link;
    - the owner activates and ends real money on the page;
    - a viewer can do none of it.
  - The authz matrix covers the three new forms.
- **Review (one reviewer; eight findings, all fixed with tests):**
  - **The worst two:**
    - the worker could trade an account with a rotated key whose failed check then ended nothing: the registry now refuses any key but the account's, and the worker's check ends such an activation;
    - a real Binance account holding any other coin, or an earlier trade, could never reconcile, which would have raised a CRITICAL incident every 5 minutes and blocked every account (R15): LIVE now reconciles like SANDBOX, and a test with BNB proves it.
  - a lock-5 test that passed for the wrong reason;
  - migration tests that no longer reached 0016's refusal, and none for 0017;
  - an account could become LIVE by an update (AT-02 did): refused now, and the fingerprint must be 16 hex characters;
  - refused activations were not audited;
  - NaN amounts gave a server error;
  - the revoke form was shown to viewers, was looked up among the newest 500 only, and lock 5 ignored the account's key.
- **Unchanged at runtime:** `LIVE_ADAPTERS` is empty, so the page says the account can open only once locks 1 and 3 are open, and live trading stays DISABLED.
- **Next (S40c-2):** ASK mandates on a LIVE account, ended with the activation, with day-long proposals priced by the venue's book, and an end-to-end real-money trade test.

## S40c-2 — Real-money proposals that ask first, kept off (2026-10-30)

- **Why:** the last part of ADR-034. Real money starts with the owner approving each trade, and a daily gold signal lands at 03:00 in Addis Ababa.
- **Built:**
  - **The grant:** on a LIVE account the auto-trader only asks, on daily bars, within an active activation and its cap, and ends by the activation's expiry. The database refuses the same.
  - **Proposals last through the owner's day:** 18 hours for a real-money daily proposal (`mandates.LIVE_ASK_TTL`); simulated ones keep the policy's lifetime.
  - **Priced by the venue's book:**
    - the worker reads Binance's book for a LIVE account outside any transaction and times the decision after it;
    - the proposal's check uses only that book;
    - without the worker's registry a real-money mandate is skipped.
  - **Ending real money revokes its auto-trader** in the same transaction, audited and told; its waiting proposals end with it and their approvals are revoked.
  - **A late approval past the price band** (0.5 % around the proposal's book) is refused when sent, and the proposal ends: it is never left waiting for a re-approval no one can price. The page and the runbook say so.
  - **`/golive`:** "Let the auto-trader ask me" (owner, fresh code; daily gold, an allowlisted daily rule, money within the cap, care) and "Stop it asking".
- **Tests:**
  - `tests/trading/test_live_trade.py`, end to end on the emulator in real time:
    - the auto-trader asks, priced LIVE by the venue's book, with an 18-hour proposal;
    - the owner approves with a code;
    - the order goes out as real money (`is_simulated` false, the PRE_SUBMISSION price LIVE, within the cap);
    - ending the activation revokes the auto-trader and returns the mode to PAPER_TRADING;
    - stopping the auto-trader ends its waiting proposal;
    - a proposal approved after the price left its band ends, and nothing is sent.
  - The grant rules (no activation, AUTO, hourly, money above the cap, the clamped expiry) and the proposal lifetime.
  - The page form and its stop, and the authz matrix for both.
- **Review (one reviewer; no path to a real-money trade without the owner's approval; nine findings, all fixed):**
  - two tests would have failed from 21:06 UTC that same day (an activation from the frozen test clock checked against the database's real clock): the real-money account tests now run in real time;
  - a late approval left the proposal stuck: it now ends;
  - waiting proposals outlived their stopped auto-trader: they now end with it;
  - a grant racing a revoke: the grant now takes the authority lock;
  - real-money proposals said "demo": they now say real money;
  - the end-to-end test now proves the book is read again at sending;
  - bad form input gave a server error: refused with a message now;
  - ending real money revoked its auto-trader without an audit row or a notice: now through `mandates.end`;
  - the docs overstated what the database refuses and when expiry acts.
- **Unchanged at runtime:** `LIVE_ADAPTERS` is empty, so no LIVE account can open, and live trading stays DISABLED. The real-money link of ADR-034 is complete and kept off.
- **What real money needs from the owner** (runbook §13.1):
  - gates A–H;
  - a static address (B-11);
  - the Binance key;
  - the env file;
  - a new contract run;
  - the key check;
  - the reviewed commit registering the adapter;
  - then the account, the activation, the mode and the auto-trader, each with a code.

## S41 — Paper fills against Binance's real order book; gate G from evidence (2026-10-30)

- **Why:** gate G ("paper period with realistic fill comparison") could not move before real money: it was blocked for want of a live venue to compare against. Binance's order book is public.
- **Built (ADR-034 addendum):**
  - **The keyless read:** `BinanceConnector.fetch_depth` reads Binance's public order book (`GET /api/v3/depth`, 20 levels, weight 5). `parse_depth` refuses a crossed, unordered or malformed book.
  - **The comparison** (`trading/real_book.py`, migration 0018 `fill_book_checks`, append-only):
    - the worker's own job (`paper.book_check`, every 10 s, through the market-data connector) compares every new paper fill on a Binance-priced paper account with the book, read once per symbol outside any transaction;
    - it records what the same quantity would have cost there, through as many levels as it takes;
    - the book must be read after the fill and within a minute of it; a fill not compared in time stays uncompared and is counted as missed;
    - three runs in a row that cannot read the book raise a warning, resolved by the next good read;
    - a venue that does not answer never stops the paper venue.
  - **Gate G** counts it: 20 compared fills, none on a book too thin, at most one in five missed, and their median between −5 bps (the simulator no kinder than the real venue) and +25 bps, with its paper period (20 closed trades over 30 days) and a recorded PASSED. It is no longer BLOCKED without a live adapter; C, F and I still are.
  - **Shown:** on Risk policy (compared fills, typical, kindest, harshest, read delay, gate G state), in the rehearsal side-by-side, and in gate G's reason.
  - **Small fixes carried with it:**
    - the header banner names the mode when every lock is open but the mode is not live, instead of a bare dash;
    - the go-live controls have headings;
    - a failure reading a real-money book cannot stop the auto-trader's other accounts.
- **Tests:**
  - `tests/trading/test_real_book.py`:
    - the depth answer read exactly and refused when wrong;
    - a size priced through the levels;
    - the sign;
    - gate G's limits;
    - a real paper fill on Binance prices compared once, append-only, uncompared after a minute, and shown on Risk policy.
  - `test_the_banner_names_the_mode_when_every_lock_is_open_but_it_is_not_live`.
  - The command-center test now expects G NOT_MET, with C, F and I BLOCKED.
- **Review (one reviewer; no defect in the arithmetic or the choice of fills; eight findings, all fixed):**
  - tests that passed for the wrong reason: a second fill left uncompared now proves the minute and the missed count, and gate G's rule is a tested function;
  - the missed count was wrong both ways: now only fills past their minute, and an unreadable book raises a warning;
  - every page load read all checks: now counted in the database;
  - the timestamp check allowed a read before the fill and any delay: now after it and within a minute, checked before the insert too;
  - the hook sat in the paper tick with its own client: now its own job on the shared connector, inserting in one fixed order;
  - the page showed "realistic" under a gate G heading while G was not met, and ignored thin and missed fills: both now count, and the column says it is part of G;
  - the docs and wording.
- **Owner step:** none. It runs by itself on the rehearsal's fills once deployed; record gate G when Risk policy says the fill comparison holds and the paper period holds.

## S42 — The gap guard (2026-10-30)

- **Why:** a stop-limit sells only at its limit or better. If the price gaps below that limit, the stop cannot fill, and the loss is no longer capped by it, yet the stop still counted as protection and nobody was told. For real money this is the case that matters.
- **Built (ADR-036):**
  - **Each execution cycle** the protection manager checks every open stop against a fresh stored quote. When the bid is below a stop's limit:
    - the protection is marked DEGRADED;
    - an incident tells the owner once, CRITICAL for real money and WARNING otherwise, in plain words;
    - an exit is proposed for the owner to approve: cancel the stop, then sell immediately or not at all, 0.1 % under the bid.
  - **The stop is never widened** by the system (V6 §1822).
  - **Standing down:**
    - a position that closes (its stop filled when the price came back, or the exit sold it) resolves the alert and ends the exit nobody needs;
    - if the price comes back while the stop is open, the protection is ACTIVE again;
    - a later gap is told again.
  - **Venues whose stop sells at market** (HFM's MT5 stop-loss, `AdapterCapabilities.stop_fills_at_market`) are left alone.
- **Tests:**
  - `tests/trading/test_gap_guard.py`:
    - told once, with an exit that sells when approved;
    - the stop fills when the price comes back, and the alert and the exit end;
    - a market-stop venue is left alone.
  - In `tests/trading/test_live_trade.py`: on a real-money position the alert is CRITICAL and the exit waits for the owner's code.
  - From the review: an exit already waiting is reused; cancelling a gapped stop is not the price coming back; a rejected exit is proposed again only after an hour.
- **Review (one reviewer; seven findings, all fixed):**
  - the guard could propose a second exit (after a recovery and a new dip, for two entries at once, or beside the owner's own): it now reuses any exit waiting for the position;
  - the stand-down could end the owner's own exits: it ends only the guard's, and only once nothing they would sell is held, audited;
  - a stop being cancelled was taken for the price coming back: it is not;
  - after the first exit ended unsold, no other was proposed while the gap lasted: now at most once an hour;
  - the guard's exit said "owner-requested": it now says the gap guard proposed it;
  - an error in the guard skipped the position's other protection: now caught apart;
  - the bid in the alert had no label: it now carries its label, source and age.

## S43 — The road to real money (2026-11-01)

- **Why:** seven locks, nine gates and a paper period stand between the rehearsals and real money. The owner should see in one place how far each has come, what is left and what to do next, without reading every gate.
- **Built (`reporting/road.py`):**
  - **A card at the top of `/golive`, and Telegram `/golive`** (the right to read accounts):
    - the gates passed and still to pass, and the locks open;
    - the closed simulated trades and the fills compared with Binance's real book, each against its target of 20, with its recent pace;
    - when gate G's evidence could hold at that pace: both counts, the 30 days since the first closed trade, and a fill comparison with nothing else wrong.
  - **Honest estimates:** the pace is the last 90 days, or the series' own life if it is younger (a week at least). No pace gives no date, and so does any fill-comparison problem other than its count (a thin book, missed fills, a typical fill outside the band). Dates are rounded up to the whole day, in UTC, and called estimates.
  - **The owner's next steps,** in order: let the rehearsals run, or wait out the 30 days; what the fill comparison still needs; record the evidence for the gates that pass on it (G once its evidence holds); then each lock still locked with who opens it, and the static address (B-11) only while the key lock is locked.
  - The paper period is read by one helper (`golive.paper_period`) for both gate G and the card, so they cannot disagree. Gate G now counts its 30 days from the first closed **simulated** trade; before, any closed trade started them.
- **Tests (`tests/trading/test_road.py`):** the estimate and the pace window; the date rounding; a fill-comparison problem gives no date and says why; the steps from waiting to recording G; the steps from the locks; the page and Telegram, and Telegram refuses a viewer.
- **Review (one reviewer; seven findings, all fixed):**
  - the date ignored the fill comparison's other shortfalls: now no date, and the reasons are steps;
  - a young series was paced over 90 days, so far too slowly: now over its own life, a week at least;
  - the steps dropped the 30-day wait and never said to record G: both are steps now;
  - the steps assumed the order of the locks: they now come from the locks still locked and who opens each;
  - B-11 was shown even once the key lock is open: now only while it is locked;
  - the page read the gates and locks twice: the card reuses the page's;
  - the wording: "holds now" once met, dates rounded up and in UTC, "none" when nothing is left.
- **Owner step:** none.

## S44 — A real-money signal lost only to the price is asked again (2026-11-01)

- **Why:** a real-money proposal goes out only while the price is within 0.5 % of the one the owner approved. On a fast day it was refused and the day's signal was lost, and a daily rule signals about twice a month. A proposal also outlived the next daily decision when made late in the day.
- **Built (ADR-034 addendum, `research/library/auto.py`):**
  - **Asked again only when the price was the reason:** every check that refused the send failed on the price (R09's age or R10's band, marked on the check's result), or the price aged past 5 s before sending.
  - **Bounded:**
    - at most twice for a bar, counting attempts that found no trade;
    - at most once in each clock hour;
    - never after the owner rejected one, nor after one expired unanswered;
    - real money only.
  - **Each new proposal is priced afresh** on the venue's book and needs the owner's code.
  - **A proposal never outlives the bar:** 18 hours, or until the next daily decision if sooner. None is made with less than an hour left.
  - **One decision at a time per mandate and market** (an advisory lock); a non-positive proposal lifetime is refused.
- **Tests (`tests/trading/test_live_trade.py`):**
  - asked again twice at each new price, never twice within the hour, never a fourth time, never past the next decision;
  - stopped by the owner's rejection;
  - none in the last hour (and, without that rule, one lasting only 50 minutes);
  - which check results count as "the price moved".
  - The real-money tests run on real time, or from 01:00 UTC the next day when run late in the evening.
  - Not tested on its own: a simulated account. Its refused sends end REQUIRES_REAPPROVAL, not BLOCKED, and the code asks again for real money only.
- **Review (one reviewer; five findings, all fixed):**
  - a proposal could be made already expired, or with seconds to live, around midnight: none is made with less than an hour left, and a non-positive lifetime is refused;
  - a halt, a wide spread, an unreadable quote or a clock that moved counted as "the price moved": now only the checks' own price marks count;
  - the reason text was parsed: the checks' results are read instead;
  - the cap counted proposals, not attempts: attempts that found no trade count too;
  - two overlapping runs could both ask: a lock per mandate and market.
- **Owner step:** none.

## S45 — Told when a rehearsal parts from its replay (2026-11-01)

- **Why:** the rehearsal page shows each rehearsal against its own replay (S39), but nobody was told when they part. Gate G's evidence assumes the system does what its simulation says; a parting is a skipped signal, a limit or a fault to explain while the money is still simulated.
- **Built (ADR-035 addendum, `reporting/replay_watch.py`):**
  - **A worker job, `rehearsal.replay_watch`, every 6 hours,** compares each started rehearsal with its replay exactly as the page does. One rehearsal that cannot be compared never hides the other.
  - **Told once per run:** the first parting opens one WARNING alert naming the days (a day the replay traded and it did not, one it traded and the replay did not, or a trade that closed another way), the account's environment, what is known and what to look at. Each later parting day is told once more, as a notice.
  - **Never twice for a day:** the alert stays until the owner acknowledges it or the run ends or is renewed, so a borderline day that comes and goes in the replay is not told again.
  - **The road to real money** shows an alert not yet acknowledged as a step, and gives no date for gate G's evidence while it stands. Its lock steps now read "Lock 6: … Who opens it: …", and lock 4 (the gates) is left to the steps before it.
  - It changes nothing: no trade, limit or gate is touched.
- **Fixed on the rehearsal page too:**
  - an exit made by the rule was named "exit" live and "rule exit" in the replay, so every rule exit showed as a difference;
  - on HFM Cent, a stop hit at Sunday evening's reopening counts for Monday, where the replay makes it.
- **Tests (`tests/trading/test_replay_watch.py`, `tests/trading/test_road.py`):**
  - told once; not again six hours later; not again for a day that comes and goes; a later day told once; kept while the replay cannot run; ended on renewal and when the run ends;
  - the worker's job on two started rehearsals, one of which cannot be compared; the road's step; acknowledged, it leaves the road and is not told again;
  - a rule exit is the same exit on both sides; closes compared by session on a venue closed at weekends;
  - a parting holds back gate G's estimate; the reworded lock steps.
- **Review (one reviewer; six findings, all fixed):**
  - every rule exit would have been told as a parting (the two sides named it differently);
  - on HFM Cent, a stop hit at Sunday's reopening differed from the replay's Monday by date alone;
  - a day that came and went in the replay would have opened, ended and told again;
  - an alert of a run that ended stayed for ever: it ends with the run, and the road shows only alerts not yet acknowledged;
  - one rehearsal that could not be compared stopped the job for both;
  - the road could ask to find why and say gate G's evidence holds at once.
- **Owner step:** none.

## S46 — Both rehearsals ready in one step (2026-11-01)

- **Why:** before the rehearsals can start, the risk policy needs each venue's fee and HFM Cent's market in its lists, and the page advised three limits at $100. The owner had to type all of it under Risk policy.
- **Built (ADR-035 addendum):**
  - **On Start with $100 → 5. Before they start,** one form lists what the policy lacks for both rehearsals (the fees and the list entries) and the advised limits as checkboxes, unticked. Each item says it applies to every account, a real-money one included.
  - **One risk-policy change with the owner's code,** through the usual workflow, with the list as its reason. The Binance fee is entered (10, or 7.5 with BNB) and checked; the advised limits apply only when ticked, at the values the page's notes give.
  - **Worked out from the policy the page showed:** a change made meanwhile (a second tab, a double click) is refused, also under the policy's lock (`change_policy(expected_version=)`).
  - A rehearsal whose market cannot be traded here is left out and named, never blocking the other.
- **Tests (`tests/trading/test_rehearsal_ready.py`):** what is lacking and advised, and the same values as the notes; only ticked advice applies, unlisted keys are ignored, a set fee is left alone; the portal flow with the fee required, the old page refused, the advice-only form after, nothing left to change, a stale code refused; wrong fees change nothing; an analyst sees no form; a rehearsal that cannot trade here never blocks the other; a change from an older version refused under the lock.
- **Review (one reviewer; six findings, all fixed):**
  - the form was not tied to the policy it showed, and the Binance fee defaulted when absent: it carries the version and the fee is required while needed;
  - the policy was read, then locked later: the version is checked again under the lock;
  - HFM Cent, when its market cannot trade here, blocked Binance's change: it is left out and named;
  - the fee did not say it applies to real money too, nor that the amount is the one entered;
  - the form kept its "get ready" title once only advice was left;
  - the fee accepted -0, 1e-30 and 1_0: it must read like 10 or 7.5.
- **Owner step:** after the deploy, on Start with $100, apply "Get them ready in one step" with your code, then start each rehearsal.

## S47 — How far to trust the history (2026-11-02)

- **Why:** the daily rules were chosen on the same gold history their results come from, a long rise. Before the owner trusts one with real money, the page should say how much of that result is the rule and how much is hindsight.
- **Built (ADR-035 addendum, `reporting/edge.py`):** under Start with $100 → 2. What history says, "How far to trust it":
  - **Nearby settings:** each rule replayed with its numbers a little shorter and longer: how many made money, and their range.
  - **Switching, and restarting:** on each 1 January, the rule that had made the most so far as it stood that day, started afresh, against each rule kept running and each rule restarted every 1 January.
  - **Since the rules were chosen (24 Oct 2026):** each rule's forward record (closed trades, result, an open trade marked), and how often its history did as badly over as many trades, each counted against its own risk.
  - **One bench for the page:** `autotrader.Bench` reads the prices, costs and policy once, works each rule's signals out once, and runs any rule or setting on them; `expectation` is now a bench and one run. The views run off the event loop and are kept for the day.
- **Research (real Binance PAXG, HISTORICAL SIMULATION):** every nearby setting made money; switching each year made −3.18, below all four rules kept running (+16.04 to +59.60); restarted every 1 January, the long-trend rules made −1.96 and −1.78 instead of +59.60 and +54.79. A stop that follows the price up cut the rules' results sharply, so it was not built.
- **Tests (`tests/trading/test_edge.py`):** the nearby settings; switching picks as each rule stood on 1 January, never with a trade that closed later (a table-driven bench), restarting shown apart, nothing picked without a year of results; how usual a record is (seeded), in risk units with half-size history trades; the forward record of closed and open trades; one bench gives the same replay as before (pinned on a seeded series), the views kept for the day, each 1 January value equal to the replay ending that day; the page with and without prices.
- **Review (one reviewer; ten findings, all fixed):**
  - the switching pick saw the future (trades closing later in the year, an open trade valued today), so its first figures and their lesson were wrong: rebuilt as each rule stood on 1 January, with the cost of a restart measured beside it, and the withdrawn figures named in the ADR;
  - the lesson claimed a cause it did not measure, and only one way: the page now shows the rank and the restarted results, and says the sample is small either way;
  - the history mixed full and half-size trades in dollars: it is now in units of each trade's risk;
  - the page ran 31 more replays on every visit: the signals are worked out once, the page's own replays are reused, and the views are kept for the day;
  - the wording for a rule with nothing to vary, the year's "best" with nothing traded, the forward record's real start when the stored prices begin late, and a stop argument ignored without a flag; and tests that compared new code with new code.
- **Owner step:** none. On Start with $100, read "How far to trust it" before choosing the rule.

## S48 — Told when a rule in use does worse than its history (2026-11-02)

- **Why:** a rule that has stopped working shows first as a forward record its own history rarely produced. S47 shows the record on the page; nothing told the owner.
- **Built (ADR-035 addendum, `reporting/rule_watch.py`):**
  - **A daily worker job, `rehearsal.rule_watch`,** sets the forward record of every daily rule an active auto-trader runs on gold (and every rule still flagged) against its history (S47, in risk units, kept for the day).
  - **One WARNING alert per rule** when the record is rarer than 1 run in 20: the numbers, how rare, the accounts running it. Still watched below 1 in 10 with the latest numbers; ended once usual again, used or not. Persisted as decimals.
  - **Real money named:** a real-money auto-trader running a flagged rule is named in the alert, told once on its own, and reminded that each of its trades still waits for the owner's code.
  - **The road to real money** shows an unacknowledged alert as a step, worded by its band; **Telegram `/rehearsal`** shows each rule in use since it was chosen, usual or not, and still answers when the records cannot be worked out.
  - Nothing changes by itself: each auto-trader keeps its limits and its floor.
- **Tests (`tests/trading/test_rule_watch.py`):** told once; still watched at exactly 1 in 20 with its numbers refreshed; ended at exactly 1 in 10; told again on a new bad patch, never rounded up to 5 %; kept while nobody runs the rule, ended when usual; an acknowledged alert not sent again; a real-money account named and told once; the worker's job on the gold rules in use (an hourly bitcoin auto-trader left out), nothing judged without prices, the road's step, Telegram's lines and its answer when a replay fails; the records on stored gold equal the page's own.
- **Review (one reviewer; seven findings, all fixed):**
  - the alert said "none to money while it is simulated" even when a real-money auto-trader ran the rule, and a real-money account starting on a flagged rule was never told: it is named, worded for real money and told once;
  - an alert ended when a rehearsal stopped using the rule, just when the rule might be picked for real money: it now ends only when the record is usual again;
  - Telegram `/rehearsal` replayed on every command, held a connection while it did, and could fail without an answer: the records are kept for the day, the replays run after the connection, and it always answers;
  - one failing rule stopped every other's check: each rule is judged on its own;
  - a mandate on another market was judged on gold: only gold mandates are;
  - the wording: a stale band while still watched, "this badly" for a strong record, "no closed trade" with no history, 4.99 % shown as 5.0 %, and the statistics' limits now stated;
  - tests that never ran the real replay path.
- **Owner step:** none. If the alert comes, read "How far to trust it" and decide at the next renewal.

## S49 — A rehearsal that renews itself (2026-11-02)

- **Why:** a rehearsal's run ended after 90 days and decided nothing until the owner renewed it with a code, once it held no trade; a trade still open meanwhile kept only its stop, its rule's exit not taken. S47 measured what a fresh start costs these rules (restarted every year, the 200-day average made −1.96 instead of +59.60).
- **Built (ADR-035 addendum, migration 0019):**
  - **"Renew it by itself"** on Start with $100: chosen with the code when a run starts, stored with the run's immutable terms (`auto_renew`).
  - **The renewal:** at the end of a run, the auto-trader's housekeeping renews it on the same rule, from the money it holds then, 90 days, audited and told to the owner (`rehearsal.renew_due`, `mandates.renew`, sharing the grant's storing, recording and telling).
  - **A trade still open is taken over** by the new run (the rule keeps managing its exit), its worst case at its stop kept apart so the floor of $80 holds; the owner's own renewal no longer waits until the rehearsal holds no trade either.
  - **Never** after a run that stopped itself or was ended by the owner, never after "Stop renewing" (append-only, no fresh code; it reaches a run renewed a moment before), not while new paper trades are paused, never on real money. It checks again the fee, the policy's markets, the rule, the market and the owner's right to approve trades.
  - **Told when it needs the owner** (at its floor, the fee removed, an error), once; the page says why an ended run has not renewed itself yet. Each rehearsal renews in its own savepoint; a renewal and a stop of the same run lock its row.
  - **The database checks every renewal** (`trading_mandates_guard`): PAPER only, the account's latest run, expired with renewal chosen and not stopped, the same terms and care, never a lower floor, no longer, at most once. The downgrade refuses while any is used and restores 0016's guard.
  - The reminders say what happens to a trade still open at the end, with or without renewal.
- **Tests:** `tests/trading/test_rehearsal_renewal.py` (renewed once on the same rule and floor, audited and told, and again at the next end; a trade still open taken over with its worst case kept apart; at its floor not renewed and told once, the page saying why; an entry on its way and paused trading waiting quietly; one rehearsal's failure not undoing the other's; never after a stop for good, a revoke, or a stop of renewal, which needs no fresh code and reaches a run just renewed; not unless chosen; the database refusing another rule, timeframe, markets, mode, care, trade counts, no money, a lower floor, a loss per trade above the care level, a looser daily stop, a longer run, a renewal that does not renew, a second renewal, a renewal after a stop, and an older run; only a rehearsal renews itself; the page; the reminder); `tests/trading/test_rehearsal.py` (the owner's renewal takes the open trade over); `tests/integration/test_migrations.py` (PAPER only, the downgrade refused); the authorization matrix.
- **Review (one reviewer; eight findings, all fixed):**
  - a "stop renewing" and a renewal of the same run could both pass: the run's row is locked by both, and a stop after a renewal reaches the new run;
  - one rehearsal's failure undid the other's renewal, with only a log line: each renews in its own savepoint, and the owner is told when a renewal needs them;
  - a trade still open at the end kept only its stop until it closed, while the wording said "nothing to do": the new run takes it over, its worst case kept apart, and the wording says what happens;
  - a run at its floor waited for ever without a word: it is told once, and the page says why;
  - the renewal skipped checks a start makes (the fee, the policy's markets, the rule, the market, the owner's rights): made again;
  - "stop renewing" asked for a fresh code, unlike stopping: no longer;
  - the pause check read only the emergency stop: any mode that pauses new paper trades now waits, under the authority's lock;
  - the database could check more: the account's latest run, the loss per trade within the care level, the daily stop.
- **Owner step:** when starting each rehearsal, tick "Renew it by itself" if you want it to keep running.

## S50 — Told when an ended run's rule says sell (2026-11-02)

- **Why:** when a run ended at its expiry without a renewal, a trade its rule still held kept only its protective stop, and the rule's own exit was never taken: the auto-trader ran only active mandates, and the database refuses a mandate approval without a running mandate.
- **Built (ADR-030 addendum 7):**
  - The auto-trader's run also looks at each run that ended at its expiry on simulated money whose rule still holds a trade nothing takes over (`mandates.ended_runs`: no later run of the same rule on the same market, not a run that renews itself).
  - **When the rule says sell, the owner is told once**, with the rule, what it holds and where to close it (Portfolio, with a code). Nothing is sold or bought by itself; the database guard is unchanged. Once told, the run's bars are not read again.
  - It never opens anything, and leaves alone a run the owner ended, one that stopped itself, and real money.
  - The reminder for a run that does not renew itself says so.
- **Tests (`tests/trading/test_ended_run_exit.py`):** a daily breakout bought, the run ended, the rule's exit told once (not again the next day) and nothing sold; an ended run never buys, even when its rule signals a new breakout while it holds; nothing told while the rule holds; a later run of the same rule takes the trade over, one of another rule does not; runs that stopped themselves, were revoked, or renew themselves are left alone, and a renewal stopped by the owner is watched.
- **Review (one reviewer; the first version, which proposed the exit for approval, was replaced):** the proposal expired an hour after a daily signal, before the owner could answer, and could not come back until the next night; it repeated every five minutes when it could not be made; it was claimed after proposing (two runs could propose twice); it offered to sell holdings that were not the rule's; a later run of another rule hid the trade; it forgot the owner's "no"; it said "without a renewal" while a renewal only waited. Telling the owner once removes all of these; the later-run and renewal cases are fixed in `ended_runs`.
- **Owner step:** none. If a run ends without a renewal while it holds a trade, you may be told its rule says sell; close it on Portfolio if you agree.

## S51 — How long you have to approve a real-money signal, and a gap in asking again (2026-11-02)

- **Why:** a real-money daily proposal is bought only while the price is still where its approved limit can buy, and the owner approves after waking; nothing said how often that holds by then.
- **A gap found and fixed (ADR-034 addendum):** an entry's band reached the ask + 0.5 % while its limit is the ask + 0.1 %. A rise between the two let the order go out, rest unfilled and be cancelled after 15 minutes: an execution failure, so the signal was not asked again (S44) and was lost without a word. The band's top is now the limit itself: such a price is refused before sending and the signal asked again at the new price.
- **Built (`reporting/approval_window.py`):** from the stored hourly bars of Binance gold, the share of days the price was still between 0.5 % below the proposal and its limit 1, 3, 6, 12 and 18 hours after the 00:00 UTC open, over all days and after a busy day; rounded down from the counts; on the go-live page's road card (a table) and in Telegram `/golive`, labelled HISTORICAL; kept until a new bar comes; never stopping the road or spoiling its connection.
- **Measured (HISTORICAL, 399 days to 2 Nov 2026):** 66 % after 1 hour, 51 % after 3, 47 % after 6, 38 % after 12, 28 % after 18; after a busy day 55, 37, 40, 28 and 18 %.
- **Tests:** `tests/trading/test_approval_window.py` (bought only between the band and the limit, not on a rise above the limit; too few days says nothing; a day with a missing hour left out; rounding down from the counts; busy days apart; the road, the page's table and Telegram's line; a failing measure leaves the road and its connection working); `tests/trading/test_live_trade.py` (a rise the bound limit cannot buy at is refused for the price and asked again).
- **Review (one reviewer; nine findings, all fixed):** the measure counted orders sent, not trades bought (the limit is the ask + 0.1 %, the band reached + 0.5 %), which uncovered the gap above; the page and the ADR overstated ("asked again", "keeps most signals") and gave a wrong reason against a wider band; an error in the measure could spoil the page's connection; rounding through floats lost a point; the band printed from a float; the figures mixed quiet and busy days; it read 9,600 bars on every view; Telegram's line lacked its day count and "not a promise"; and tests that could not catch an asymmetric band.
- **Owner step:** none. When real money comes, approve soon after the signal; otherwise expect to be asked again.

## S52 — Is the gold auto-trader ready? The home page says so (2026-11-02)

- **Why:** the owner asked for a clear dashboard, and to know whether it is ready for the simulated auto-trader. Readiness was spread across Start with $100, Operations, Sources and Risk policy.
- **Built (`reporting/ready.py`, the home page's first panel):**
  - **Who sees it:** the owner, meaning whoever may approve trades and open accounts, as on Start with $100's start form.
  - **A status:** RUNNING, READY TO START, NEEDS YOU, PROBLEM or ENDED.
  - **A checklist,** read from the places that decide:
    - the worker's own heartbeat (the Telegram bot beats too);
    - whether the mode lets simulated money trade;
    - an open critical incident (R15);
    - the risk policy, each venue's fee and market, and whether each market is set up and tradable.
  - **Each side's prices,** judged as the auto-trader judges its daily bars: the newest one fresh, and the 400 days it needs. Then its live price, with a short grace for a late poll. For HFM, also the bridge's report.
  - **Each run, sorted as Start with $100 sorts them:**
    - not started;
    - running, with its money, rule, end date and renewal;
    - running but refused by the account-level risk checks (R02, R03, R08, shared with the engine);
    - its last trade before it stops for good;
    - ended and renewing itself (or why not yet);
    - ended, so you may renew it;
    - stopped for good at its floor.
  - **The one next step,** with a button to the page where it is done, and where things are.
  - It changes nothing. If it fails, the home page still works and says so in one line; the error is logged.
- **HFM's hours:**
  - Shown as waiting, not as problems, while the bridge reports and is connected:
    - the daily break from 17:00 in New York;
    - the first hour after the Sunday reopening;
    - a quiet market of up to ten minutes.
  - Shown as closed, with the bridge still reporting: the weekend.
  - Problems:
    - a bridge that does not report (also at the weekend);
    - its terminal cut off from HFM;
    - prices that reach the bridge but are not stored;
    - a longer weekday silence ("a market holiday, or a frozen terminal").
- **Fixed on the way:** the home page's System health counted the Telegram bot's heartbeat as the worker's. It now reads the worker's own.
- **Tests:** `tests/trading/test_ready.py`:
  - from nothing set, to ready to start, to running, with real bars, quotes and heartbeats;
  - a new account waiting for its first reconciliation is not refused;
  - the worker (the bot's heartbeat not counted), the emergency stop and the mode come first;
  - a run refused by R08, and every run by an open critical incident;
  - stopped, revoked, renewing and unrenewable runs, and all ended;
  - HFM's daily break in summer and winter time, the Friday close, the weekend with and without the bridge, and the Sunday reopening;
  - a quiet market, prices not stored, a silent bridge, a terminal cut off, a price that never came;
  - Binance's late poll, too little history, a stale daily bar;
  - the home page for the owner and not for an analyst or a viewer, and a failing panel that leaves the page working.
- **Review (one reviewer; 16 findings, all fixed or answered):**
  - **Truthfulness:**
    - the bot's heartbeat passed for the worker's;
    - a run the risk policy refused showed as running;
    - an ended or stopped run read "not started yet", with a Start step the system refuses;
    - any HFM pause was called the daily break;
    - a weekday holiday read as a bare problem;
    - a dead bridge was hidden at the weekend;
    - a late Binance poll turned the pill red;
    - too little history and a market not tradable were not checked.
  - **Wording:** the analyst saw the owner's steps; the emergency stop said "resume with your code" instead of its recovery checklist; the next-decision date could be wrong, so it is no longer shown; "stops for good at 80" became "before one more full loss could take it below 80".
  - **Code:** the panel's failure was silent; a bridge was picked by its newest report rather than by name.
- **Owner step:** none to build it. On the home page, follow its one next step:
  1. "Get them ready";
  2. "Start them" (tick "Renew it by itself").

## S53 — The audit before the rehearsals start (2026-11-03)

- **Why:** before starting the two simulated rehearsals, the owner asked for an evidence-based technical and trading-safety audit of the whole system, with every critical and high finding fixed first. The full checklist is in `docs/reports/2026-11-03-pre-rehearsal-audit.md`.
- **How:** 134 probe tests (counting each parametrised case) were run against the code as released (3b1718e), 128 of them written by three independent reviewers. Each probe exercises behaviour with real paper fills, crashes injected at each step, broker faults, stale and missing data, and forged database rows. Production was checked through the release's own checks, container status and logs, and a restore drill of today's backup. Its database was not read.
- **Fixed (high):**
  - **The Binance gold cross-check compared Binance with a copy of itself.** For PAXG/USDT the only second price R10 could find was the Binance test network's book, which copies Binance's prices (0.0 bps apart on 2026-11-03). So the check could never catch a bad Binance price.
    - Now independent markets come first: HFM's gold, and PAXG/USD where Coinbase and Bitstamp are still polled (this server retired them, ADR-027).
    - The test network's copy is used only when no independent market is fresh: HFM's weekend and daily break, or a silent bridge. That is the same as before.
    - An order on the test network itself is still compared with the real market.
    - Code: `market/data.py`, `sources/registry.py` (`REFERENCE_PAIRS`), `trading/context_builder.py`. ADR-028 addendum.
    - The first version of this fix dropped the test network altogether. The commit's review found that, with Coinbase and Bitstamp retired, the Binance rehearsal would then never have entered. It was corrected before release.
  - **Authenticator codes typed into forms were not rate-limited.** POST /mfa allowed 10 codes in 5 minutes, but a code sent with a privileged form, the approval form or a Telegram approval was checked without a limit.
    - All of them are now limited the same way.
    - A signed-in session has one budget, shared by POST /mfa and the forms. A session that has not passed MFA (all it takes is the password) has another, so it cannot use up the owner's approvals.
    - The Telegram bot keeps its own budget in its own process.
  - **The worker could stall, and run a job twice.**
    - It claimed a job before a slot was free. With four slow jobs running, its main loop waited: no heartbeat, no scheduling. The watchdog then restarted it.
    - A job's 5-minute lease was never renewed, so a job running longer was handed out again while still running.
    - Now a job is claimed only when it can start at once, and background jobs take at most two of the four slots, so the short jobs (prices, the auto-trader, trading) keep the other two.
    - A running job's lease is renewed every minute, and a handler still running after an hour is stopped and failed.
    - With every slot busy and no job finished for ten minutes, the loop stops feeding the watchdog, which restarts the worker.
    - The database's once-only keys had already made a second run harmless for trading (shown by the crash and concurrency probes).
- **Fixed (medium and low):**
  - A closed trade that lost more than its plan at the stop now says so in the owner's notice, with the amount. The plan leaves out HFM's overnight swap and a price that jumps past the stop. Before, only the result was shown.
  - An auto-trader exit's notice read "Loss at the stop at most None".
- **Verified, unchanged (selection):**
  - **The floor, with real paper fills:**
    - 13 losing trades took the Binance rehearsal from 100 to 81.44, and it stopped for good there. Each loss was 0.99 of its plan, fees and slippage included, and half size came after the 10 drop.
    - After a renewal from 94.11, the new run's total stop was 14.11, so the floor stayed at 80. That run stopped for good at 81.29, on the same single account, with every step audited.
    - The paper venue's books and the ledger agreed to the last digit.
  - **Crashes and duplicates:** a crash at each step of a trade never sent a second order or sold twice. Four concurrent runs on one bar made one entry.
  - **Isolation:** every attempt to reach real money was refused, with each lock refusing on its own. The Binance adapter accepts only the test network, and the HFM adapter only demo terminals.
  - **Data:** stale, missing and invalid data never trades.
- **Left for the owner's decision** (each changes trading behaviour; see the report):
  1. a gap through a stop-limit leaves the trade open until the owner approves the exit or the rule sells;
  2. HFM's swap is not in the plan, so a long-held last trade can end a little below 80;
  3. a daily entry refused for a passing data reason (a reconciliation too old, the cross-check unavailable) loses that day's signal;
  4. the risk policy's defaults freeze a $100 rehearsal after four losses or a 10 drop, unless the advised limits are ticked in "Get them ready".

  Before any real money: the cross-check does not yet require the checked venue's own price among the prices it compares. For simulated accounts R09 refuses a stale own quote; for a live account, the venue's live price would have to be compared.

  Smaller hardening items were reported to the owner directly.
- **Tests:**
  - `tests/trading/test_rehearsal_floor.py` (the walk to the floor, renewal, a gap, HFM's swap, the policy's defaults, the cross-check);
  - `tests/trading/test_cross_check.py`;
  - `tests/integration/test_queue.py` (busy slots, a long job's lease, a hung job, a stuck worker);
  - `tests/security/test_web_auth.py` and `tests/trading/test_telegram.py` (the code limit);
  - `tests/unit/test_display_format.py` (the notices).
- **Production (2026-11-03):**
  - release 3b1718e; 26/26 server checks; live trading DISABLED;
  - no warnings or errors in the logs since the release;
  - restore drill of today's backup: PASS, 10/10, with a tampered copy and a wrong key refused.
  - Not repeated: a VM reboot (last 2026-10-20); it needs the owner.

## S54 — The owner's three choices after the audit (2026-11-03)

- **Why:** the audit left three questions that change how trades are made. The owner chose the recommended answer to each (AskUserQuestion, 2026-11-03). ADR-035 addendum.
- **Built:**
  - **HFM Cent stops at market, as HFM's MT5 does.**
    - It is sized to the stop and filled at the bid less slippage once hit, also in a gap.
    - The gap guard stands down for it, and the replay and the pages use market stops for it.
    - Binance keeps its stop-limit.
  - **The swap is kept aside** (`trading/swap.py`):
    - The stop for good, the entry check, a renewal and the home panel count the open positions' swap so far.
    - They also count what one more full-size trade would pay over 90 nights: about 0.90 on the HFM rehearsal, nothing on Binance.
  - **A daily entry refused only for missing data is tried again each hour that day:**
    - "Missing data" means every refusing check said UNKNOWN.
    - The owner is told once; a real refusal is not retried; a bar is never bought twice.
- **Found on the way, unchanged:** at half size the smallest HFM Cent trade (0.01 oz) can risk more than the 1.00 allowed, so that rehearsal may wait after a 10 drop (told as a skip). It is in the user guide.
- **Review (one reviewer; 2 medium, 4 low and 1 earlier defect, corrected before release):**
  - the retry is for simulated money only: a real-money signal is never re-asked;
  - the swap reserve applies to the $100 rehearsals only, not the HFM demo auto-trader;
  - a retry needs every refusing reason to be a check that said UNKNOWN, not a lot or price-step problem;
  - a renewal needs room for a full loss and the swap it keeps aside;
  - a renewal that takes a trade over keeps that trade's worst case out of the new run's money, so the floor stays exactly 80. Before, the database refused the next renewal after it (from S49).
  - the home panel no longer fails on an invalid risk policy.
  - Known, small, left as is: the replays do not model the reserve; a check that errs is retried hourly; and the panel counts a taken-over trade's swap twice while that trade is open (ADR-035 addendum).
- **Tests** (`tests/trading/test_rehearsal_floor.py`):
  - a gapped HFM Cent stop sells at market, with no gap guard left waiting;
  - Binance keeps its stop-limit;
  - HFM Cent walks down to its stop for good with the swap kept aside, and ends at or above 80, equal in both records;
  - a missed morning is tried again within the day, once an hour, bought once and told once;
  - a real refusal is not tried again;
  - only a rehearsal keeps swap aside.
- **Other tests:**
  - a real-money signal refused for missing data is not asked again (`tests/trading/test_live_trade.py`);
  - a second renewal after one that took a trade over (`tests/trading/test_rehearsal_renewal.py`);
  - an invalid risk policy with a rehearsal running (`tests/trading/test_ready.py`).

## S55 — HFM gold first: the home page answers five questions (2026-11-03)

- **Why:** the owner made HFM gold (XAUUSD) on a real HFM demo the priority (2026-11-03). They chose a Cent demo with $100, SELL as closing a buy, the rule's exit as take profit, and evidence from the server.
- **Found first, from the server's database (read-only, 2026-11-03 14:50 UTC):**
  - **Bridge:** it reports every second; MT5 is connected; the account is DEMO, USD, balance and equity 100,000.00, leverage 1:1000; nothing is open.
  - **Prices:** HFM's XAUUSD quotes arrive about 5,300 a day; the account is reconciled with HFM every 5 minutes (PASSED).
  - **No HFM demo order, ever:** the auto-trader was granted from $100. On this standard account the smallest gold trade is 1 oz (about 3,907), so every signal was skipped as more than its money. A Cent account trades 0.01 oz.
  - **The paper rehearsals:** both running since 2026-11-02, with the risk policy's defaults (4 losses in a row, 10 % drawdown).
- **Built:**
  - **`reporting/hfm_home.py`:** the HFM account panel, first on the home page.
    - It answers five questions: is the system working, is the broker connected, is automatic trading running, is the money protected, does anything need me.
    - It shows the account (DEMO or REAL), balance and equity, today's and the total result, open trades with HFM's stop-loss, closed trades, HFM's gold price and whether the market is open, the latest decision (BUY, SELL or WAIT and why), the last successful check, the next decision, and STOP ALL NEW TRADES.
    - Figures come from HFM's own report; nothing simulated is shown there.
  - **The paper rehearsals** are folded below, marked SIMULATED and "not HFM money". The auto-trader card is not repeated for the HFM account.
  - **Fail-closed money check:** the HFM adapter refuses orders and reconciliation while the account's money is not USD. A Cent account reports US cents, which would be read as dollars, so it is refused until Cent accounts are handled.
- **Tests:**
  - `tests/trading/test_hfm_home.py`, on the in-process Windows bridge loop and the MT5 emulator:
    - the account at a glance;
    - an open trade with HFM's stop-loss;
    - 1 oz too big for its money, as on the owner's demo;
    - a REAL account, an account in cents, and a silent bridge, each a problem;
    - the page itself.
  - `tests/trading/test_hfm.py`: an account in cents is never traded or reconciled.
- **Visual check:** headless Chromium, desktop and phone, rendered locally from a copy of the server's real HFM report of 2026-11-03 15:06 UTC (account number and fingerprint removed).

## S56 — A real trade cycle on the HFM demo (2026-11-03)

- **Why:** the owner asked for evidence that orders really reach HFM's demo and that our records agree with HFM's own history, not a dashboard-only simulation. Until now the contract check only placed a resting buy far below the market and cancelled it; no HFM order had ever filled.
- **Built:** `cosmic broker hfm-trade-check` (`trading/brokers/hfm_trade_check.py`, contract version `hfm-mt5-trade-1`). It runs on a demo where no HFM account is open here, before one is opened (see the review below), on the DEMO account only, with the smallest volume:
  - HFM rejects a buy below its minimum volume, and nothing is bought;
  - a market buy carrying its stop-loss fills, and HFM holds the stop on the position;
  - the same order sent again buys nothing more;
  - the fill is read back from HFM's deals;
  - a sell naming the trade closes it;
  - HFM's balance moves by exactly what the two deals booked (profit, commission, swap, fees), and nothing is left open.
  - Whatever fails, it closes what it bought. Each run is recorded with the account's fingerprint.
- **The MT5 emulator** now rejects a volume below the minimum (retcode 10014), as HFM does.
- **Tests** (`tests/trading/test_hfm.py`): the full cycle passes on the emulator; a real account is never traded.
- **Review (four reviewers on S55 and S56; every finding below reproduced, then fixed):**
  - **The trade check would have broken the open account (high):**
    - Run on the demo `hfm-demo` is open on, as first planned, its deals were unknown to that account's books. Reconciliation would fail for good (2 unknown fills, a 0.36 cash gap): a CRITICAL accounting incident whose R15 refuses every account's trades, the paper rehearsals' too.
    - It now refuses while an HFM account is open on its demo; it is the check for a new demo before an account is opened there.
    - The evidence on `hfm-demo` will come from its own trades, approved by the owner.
  - **It could leave a position open (high):**
    - **The causes:** an ambiguous buy outside the cleanup, a single close attempt with a tight limit, a lost answer, a lagging history buying twice.
    - **Now:**
      - every buy is inside the cleanup;
      - the cleanup closes what HFM holds under the check's IDs, retrying with fresh IDs and a wide price;
      - the re-send waits until HFM's history shows the buy;
      - it does not start near the daily break or the weekend, or while an earlier check position is open;
      - its last step names anything left open, with what to do.
  - **The exit's read-back (medium):** the closing deal must be read back as the sell's. This proves how HFM tags an exit; booked to the entry, it would overfill it in real trading.
  - **The contract gate (low):** a passed trade check no longer counts as the broker contract that lets an account open, nor shows as "the latest contract run".
  - **Another MT5 account (found on the way):** the adapter and the server's command hand-out refuse an MT5 account other than the one the open account was opened on. On the server on 2026-11-03, the reported fingerprint matched the opening contract run.
  - **Commands and causes:** commands also go only to a USD account. Each unreachable cause (silent, money, another account) is its own incident with its own advice.
  - **The panel (second review, 13 findings):**
    - its facts count only from a connected MT5 with an account;
    - trades the bridge does not follow are found from HFM's equity and margin;
    - REAL and the money are named before Algo Trading;
    - "running" is no without the worker, the broker or the check;
    - an hourly skip's reason is found;
    - a buy shows its own entry fill, and a sell its exit;
    - a daily retry is said;
    - a check in progress is not "failed";
    - a mandate not on gold is said;
    - the right limit is named;
    - hourly decisions skip the closed market;
    - every figure has its currency;
    - the footnote follows DEMO or REAL.
- **Tests:**
  - `tests/trading/test_hfm.py` (24): the trade check's full cycle, and its refusal on a demo with an account open (reconciliation stays clean), near the break, at the close, and after the reopening; an ambiguous buy closed; a lost and a late close retried until closed; an exit tagged as the entry failing the check; a passed trade check not opening an account; another MT5 account refused; a new reason as a new incident.
  - `tests/trading/test_hfm_home.py` (22): one test per panel finding.
- **On the real demo:** see the next section.

## S57 — One test trade on the HFM demo, the owner's way (2026-11-04)

- **Why:**
  - The owner's evidence list needs real orders on the HFM demo, reconciled against HFM's history.
  - The trade check (S56) cannot run on `hfm-demo`, because an account is open there.
  - The auto-trader cannot trade it either: its $1,000 is less than the smallest gold trade, 1 oz (about 4,000).
  - On the server (read-only, 2026-11-04 05:19 UTC), `hfm-demo` manages 20,000 USD of the demo's 100,000, and policy v3 allows HFM gold. A 1 oz test with a stop 3 % below (about 120 USD at risk) fits every limit.
  - The manual proposal form asks for twenty fields; the owner asked for simple.
- **Built:**
  - **`trading/hfm_test_trade.py`:** `refusal(conn, now)` says why a test trade is not possible now. It is not possible when:
    - the account is not active, or its last check did not pass;
    - the bridge's report is stale, or not DEMO, USD and the account it was opened on;
    - MT5 is not connected, or Algo Trading is off;
    - gold closes or breaks within the hour;
    - something is open or in flight, or our records still hold a trade HFM has closed.

    `propose` asks again under an advisory lock, then proposes the smallest gold buy at the ask + 0.1 %, with a stop-loss 3 % below and plain texts that say it is a TEST, not a market view. The buy is IOC (filled at once or not at all), and the proposal lasts 15 minutes. So once it is approved, there are at least 45 minutes to close it before a break or the weekend.
  - **The home page:**
    - **Test one demo trade** when it is possible, or the reason in words when it is not.
    - **Close the gold trade** (the existing approved exit), offered only when:
      - our records hold gold;
      - HFM reports it;
      - the broker answer is yes;
      - no sale is already waiting (then that is said instead).
    - Both lead to the approval page; nothing is sent without the owner's code.
  - **`mt5_bridge.report_problem` and `trading_off`:**
    - The adapter (orders, reconciliation, its venue status) and the test trade share these two rules.
    - The home page's "Is my broker connected?" asks the same questions in its own words; its account check is now as strict as the adapter's.
    - `_state` raises exactly what `report_problem` returns, so the reconciliation's cause matching is unchanged.
  - **A slow report is not an outage.**
    - **What the server showed** (read-only, the week to 2026-11-04): 21 of 2,016 checks of `hfm-demo` failed, every one "venue unreachable".
      - 18 found the report 15–17 s old: a slow report, not a silent bridge.
      - 3 were real outages of 4–5 minutes (01 Nov 20:35, 02 Nov 11:30, 03 Nov 06:20 UTC).
      - Each opened a WARNING that closed by itself within 5–10 minutes, and the next check passed.
    - **Now:** a read that finds the report 15–30 s old waits up to 10 s for the next one. An older report is refused at once, as before.
  - **The MT5 emulator** has an Algo Trading switch; off, it answers 10027, as MT5 does.
- **Review** (one reviewer; every finding reproduced in the code, then fixed):
  - **A buy that could rest at HFM (medium):** sent GTC with the ask above its limit, the bridge places a buy limit with no expiry, which could fill hours later. The buy is now IOC.
  - **The trading-hours rule (medium):** it was asked only when proposing, while the proposal lived an hour. The proposal now lasts 15 minutes, and gold must trade for the next hour.
  - **Numbers with 18 decimals (low):** the texts read "1.000000000000000000 oz". They now read as people write them.
  - **The close button (low):** it showed for any gold HFM reported, even another account's, or while a close already waited (a second press made a second exit).
  - **The docs (low):** they claimed more sharing than the code had.
- **Tests** (`tests/trading/test_hfm_test_trade.py`, 5, on the in-process bridge loop and the emulator; 20 of 20 runs passed):
  - **The full way:**
    - offered on a Wednesday;
    - proposed from the page, with nothing sent before approval;
    - 1 oz (0.01 lot) at most 0.1 % above the ask, IOC, lasting 15 minutes, with the stop 3 % below;
    - pressed again: refused (422);
    - approved, filled, HFM holding the stop;
    - the page then offers only the close; while the close waits, it says so and offers nothing;
    - the close is approved, and nothing is left open;
    - reconciliation passes, and the ledger equals HFM's balance to the cent;
    - it is offered again.
  - **Refused:** on a Saturday, within the hour before Friday's close, with Algo Trading off, on another MT5 account, a REAL account, or an account in cents, and when the bridge is silent. The page shows why, and the action itself refuses, with nothing proposed.
  - **The ask past the limit:** nothing bought, nothing left resting at HFM, the order EXPIRED.
  - **A slow report:** a 20 s old report followed by a new one is read; with none following, it is refused after the wait; a minute old, it is refused at once.
  - The viewer is refused (authorization matrix).
- **On the real demo:** after the owner releases it, they press it once, approve, close and approve. The evidence (orders, HFM deals, reconciliation) is read from the server, read-only, and reported.

## S58 — The way to a real HFM account: a checklist from evidence (2026-11-04)

- **Why:** the owner asked for a safe way from the demo to a real account, without rebuilding:
  - separate credentials, and a verified account identity and type;
  - sizes from the real account's own rules;
  - their explicit authorization;
  - a separate real-account readiness checklist;
  - the switch tested without a real-money order;
  - and "do not claim production readiness until the evidence supports it".
- **Built:** `trading/hfm_ready.py`, eleven checks, each PASSED, NOT MET or BLOCKED from what is recorded, never from a setting:
  1. connected;
  2. reliable over a week;
  3. gold prices arriving;
  4. an order filled with HFM holding its stop;
  5. a trade closed with the ledger equal to HFM's balance after it;
  6. recovery from outages;
  7. a Cent demo checked;
  8. 30 days on it;
  9. the real account on its own bridge, identified;
  10. sizes from its own rules;
  11. the owner's authorization.

  Checks 7 to 11 are BLOCKED in this build: a Cent account is refused, and no real-account bridge or HFM real-money adapter exists.
- **Where it shows:**
  - on Go-live readiness, for whoever may read the account (its evidence is the account's own trades);
  - as one line on the home page's HFM panel, "N of 11 checks passed".
- **The switch to a real account, tested without a real order:** MT5 logs into a real account while an approved demo trade waits to be sent. Nothing reaches MT5, and each layer refuses on its own:
  - the adapter makes no command;
  - the server's hand-out has nothing to give;
  - the bridge program refuses with "the bridge trades demo accounts only".

  The home page says PROBLEM ("a REAL account: every order is refused"), and the test trade and the checklist refuse too.
- **On the server now** (computed read-only with the checklist's own rules, 2026-11-04 06:12 UTC, before the release):
  - **connected:** PASSED.
  - **prices:** PASSED: 5,285 HFM gold prices in 24 hours, the latest 3 s old.
  - **reliable:** NOT MET.
    - 2,016 checks in 7 days, 1,995 passed (98.96 %), the latest PASSED.
    - Every one of the 21 failures "could not reach HFM"; none found our records and HFM's apart.
    - The slow-report wait (S57) addresses the 18 failures on a report 15–17 s old.
  - **filled and closed:** NOT MET. No HFM demo order has ever filled; the test trade (S57) gives this evidence.
  - **recovers:** PASSED, on these figures:
    - 103 warnings of HFM unreachable in 30 days, every one closed by itself;
    - the longest in the last 7 days: 10 minutes;
    - the long ones (up to 38 hours) were all before 22 Oct, when the bridge still ran on the owner's PC.
  - **checks 7–11:** BLOCKED.
- **Tests** (`tests/trading/test_hfm_real_ready.py`, 3):
  - the checklist passes only on evidence, before and after a test trade, on the page too;
  - recovery is shown from an outage the test makes and ends;
  - the switch to a real account sends nothing.

## S59 — Your own trades by hand, kept apart (2026-11-04)

- **What happened** (read-only, 2026-11-04):
  - Between 06:53 and 06:59 UTC the owner traded gold by hand from the MT5 app on the demo the system manages: magic 0, deal reason 1 (mobile).
  - The system had sent no order or command. Its check with HFM failed at 06:55 and 07:00, with 4 fills unknown to the account, a cash gap and a position.
  - That raised a CRITICAL incident, which blocks every account's new trades (R15), the paper rehearsals' too. It does not clear by itself: those deals stay in HFM's history.
  - Asked, the owner said the trades were theirs, and chose to keep their trades apart (AskUserQuestion, 2026-11-04).
- **Built:**
  - **`mt5_bridge.by_hand`:** a trade by hand is a position opened without the system's magic number. Its deals are decided by that position, because HFM tagged the owner's stop-outs with magic 0 and may do so for the system's. A deal by hand on the system's own position stays the system's, a disagreement: fail closed.
  - **The adapter** leaves the owner's orders, fills and positions out of what it reports. `own_deals` gives each of their deals with what it did to HFM's balance.
  - **The reconciliation** books each such deal once, as UNMANAGED against CAPITAL (`venue_own_trade`), so the comparison with HFM's balance stays exact to the cent.
  - **The home page:**
    - marks them "(yours, by hand)", and "sell" for a short;
    - gives their count and result after costs;
    - counts them in "Is my money protected?". One without a stop-loss is a PROBLEM, with "set one in MT5, or close it", not "see Portfolio".
  - **The test trade** is offered beside an open trade of the owner's.
- **Tests** (`tests/trading/test_hfm_hand_trades.py`, 5):
  - **Trades by hand:** a buy and a sell, then a buy left open. The check passes and the system's cash is untouched; booked once, checked again, nothing new. The system then trades beside them, and only its own trade is offered to close.
  - **A hand close of the system's trade:** still a disagreement, not booked as the owner's.
  - **The system's stop-out tagged magic 0:** still the system's; the stop is FILLED and the check passes.
  - **HFM's real report of 2026-11-04:** all 9 deals and the open sell are the owner's.
  - **A trade by hand without a stop:** said, with where to fix it.
- **The evidence on the real report** (07:10 UTC):
  - the rule gives all 9 deals and the open sell to the owner;
  - their results: +13.05, +2.40, −3.92 and −12.40 USD, −0.87 in all, which equals HFM's balance of 99,999.13;
  - **Checked after the release (production, read-only, 2026-11-04):** released at 08:53:02 UTC.
    - The first check with HFM on the new code (08:55:03) PASSED with 0 disagreements; the four before, on the old code, had failed with 11.
    - The CRITICAL incident closed by itself at 08:55:03.
    - 4 deals were booked as the owner's (the others had no result), −0.87 USD in all.
    - Ledger cash plus unmanaged was 99,999.13, equal to HFM's balance.
    - The owner's open sell stayed open, untouched.
- **Not covered:** a deposit or withdrawal on the demo (a balance deal) is still a disagreement, as before.

## S60 — You decide the amount; the system sizes the gold (2026-11-04)

- **The owner's correction (2026-11-04):** do not ask for a larger demo balance; let me set the amount; work out the size; show it before a trade; run small amounts in an internal simulation, kept apart from HFM.
- **Why every signal had been skipped** (the investigation):
  - **Fully paid:** the auto-trader's money had to cover the whole value of its open trades (ADR-028), so 1 oz (about 4,100) never fit in $100 or $1,000.
  - **A $1 minimum risk:** `terms_from_money` refused any amount whose risk per trade was under 1.00.
  - **HFM's minimum is real:** its 1 oz (0.01 lot × 100) comes from HFM's own report, not from our code; no amount or balance is hard-coded.
  - **The texts:** said "the smallest trade (1.000000000000000000)"; now "(1)".
- **Built:**
  - **The money is a risk budget for CFDs** (ADR-028 addendum): gold may be held up to 10 × the amount (`mandates.CFD_EXPOSURE`), each trade sized by the loss at its stop. Spot coins stay fully paid.
  - **Your own limits:** risk per trade (0.1–5 %), most trades open (1–5) and most a day may lose; no minimum beyond 0.01.
  - **`auto.size_entry`:** the auto-trader's own sizing, now one function. The preview calls it, so what the owner sees is what it would buy.
  - **The replay** applies the same multiple, so replay and live stay equal.
  - **Gold auto-trader settings** (`/hfm/settings`, first in the menu, linked from the home page):
    - **The choices:** internal simulation or HFM demo; the amount; the risk; the daily loss; the trades open at once; automatic or ask me.
    - **"Calculate"** shows, from today's HFM price and volatility:
      - the account's money; the most a trade and a day may lose;
      - the price and spread; the stop (3 × ATR) and its distance;
      - the smallest trade and the size, in ounces and lots;
      - the value, and HFM's margin at its leverage;
      - spread, commission and swap; the loss at the stop.
    - When nothing fits, it says why, with one amount that fits, a risk that would (if 5 % or less), and the simulation.
    - **Start** needs the owner's code; one gold auto-trader runs at a time.
  - **Internal simulation `hfm-sim`:** a PAPER account on HFM Cent terms (HFM's prices, 0.01 oz steps, measured swap, 0.05 % slippage a fill), with virtual 10,000, opened on first start, labelled SIMULATED. Nothing in it reaches HFM.
- **Tests** (`tests/trading/test_your_amount.py`, 4). Gold at 4,100.00 / 4,100.30, hourly volatility 10, so the stop is 30 an ounce below a buy at 4,104.40:
  - **Simulation at 2 %:**

    | Amount | Size | Loss at the stop |
    |---|---|---|
    | $10 | nothing: 0.01 oz would lose 0.30, more than 0.20 | — |
    | $20 | 0.01 oz | 0.30 |
    | $50 | 0.03 oz | 0.90 |
    | $100 | 0.06 oz | 1.80 |
    | $200 | 0.13 oz | 3.90 |

    $10 says "a risk of 3.00 % per trade, or an amount of 15.00 or more", and at 3 % it trades 0.01 oz.
  - **HFM demo at 2 %:** $10–$200 say that HFM's smallest trade (1 oz = 0.01 lot) would lose 30.00 at the stop, that it fits from 1,500.00, or in the simulation. At $1,500: 1 oz, 30.00 at the stop, value 4,104.40, margin 4.10 at 1:1000.
  - **A full simulated cycle, $50 at 2 %,** started from the page with the owner's code:
    - WAIT on quiet hours;
    - BUY 0.03 oz on a breakout, filled at 4,102.35 (the ask plus 0.05 % slippage), with nothing sent to HFM;
    - SELL by the rule's exit at 4,105.95;
    - result +0.1078755 USD, and the account's cash equals 10,000 plus it.
  - **The rules, pure:** no $1 floor; the bounds; 10 × for gold, 1 × for a coin.
- **Still to come:** the signal still reads Binance's PAXG prices (market data only); HFM's own candles replace it next.

## S61–S62 — HFM gold only: HFM's candles, the reading, and the internal simulation (2026-11-04, ADR-037)

- **Why:** the owner's correction of 2026-11-04:
  - choose their own balance, amount and risk;
  - an internal simulation on HFM's prices with fractional ounces, BUY and SELL, stop-loss and take-profit, correct P/L;
  - HFM's own candles (M1, H1, D1), never Binance's prices;
  - two separate workflows (simulation, HFM demo);
  - honest statuses;
  - Binance out;
  - never a raised risk or balance.
- **The incident and the ten questions:** answered in S59 and S60 with server evidence; summarised in the owner's report.
- **Built:**
  - **HFM's own candles** (`market/hfm_candles.py`):
    - live from each bridge report (1 s), into 1-, 5-, 15-minute, hourly and daily candles;
    - history rebuilt daily from the HFM quotes stored since 2026-10-15;
    - the broker's own through bridge version 4's read-only `RATES` (hourly job; MT5's times converted through New York + 7 h, right in summer and winter);
    - migration 0020 allows the `RATES` command.
  - **The reading** (`gold/analysis.py`), a forward test:
    - per timeframe: EMA 8/21 trend, RSI 14, ATR 14, support and resistance;
    - BUY, SELL or WAIT with the reasons (H1 trend, D1 filter from 21 days, the M1 RSI turn);
    - stop 1.5 × the hourly ATR.
  - **The internal simulation** (`gold/sim.py`, migration 0020, worker job every second):
    - its own accounts, settings, decisions, positions and ledger (append-only where they must be, with database guards);
    - **size:** risk × amount ÷ stop, in 0.01-ounce steps;
    - **too small:** "WAIT — Your selected trading amount is too small for this opportunity at your chosen risk level", with from which amount at that risk;
    - **limits, each a WAIT with its reason:** total open risk, trades open, the day's loss, the drop from the best, selling off, free margin;
    - **fills:** at HFM's ask and bid;
    - **exits:** stop, target or rule, checked each second;
    - **costs:** HFM's swap per night (triple on its day), no commission;
    - **records:** one decision per 1-minute candle, so no duplicate after a restart.
  - **Pages:**
    - **Gold analysis** (`/gold/analysis`): the price, each timeframe and the decision.
    - **Simulation** (`/gold/sim`):
      - start with a starting balance;
      - settings, each checked on its own with a reason;
      - stop (closes everything at HFM's price) and run again;
      - balance, equity, today, total, win rate, drop from best, margin;
      - open and closed trades, and the last 30 decisions.
    - **HFM demo** (`/hfm/settings`):
      - HFM's own account, positions with tickets and deals;
      - whether HFM would take a trade from the amount at the owner's risk;
      - the execution test.
  - **The dashboard:**
    - the eight statuses, each yes only on evidence, under a headline of ALL GOOD or NEEDS ATTENTION;
    - HFM positions with ticket, who opened them, side, size, entry, stop-loss and take-profit;
    - a manual position without a stop-loss named with its ticket.
  - **The menu:** HFM gold first (Dashboard, Gold analysis, Simulation, HFM demo, AI, Risk settings, System health); the rest under "More", Binance's pages marked paused.
  - **Binance paused** (`mandates.PAUSED_VENUES`): no auto-trader acts on Binance, or on a market whose signal is Binance's prices (both paper rehearsals and the earlier HFM auto-trader). Records kept.
- **Tests:**
  - **`tests/trading/test_gold_sim.py` (15):**
    - the size at $10, $20, $50, $100, $200 and $500, with the WAIT text and the amount it would trade from;
    - each setting refused with its reason;
    - BUY, SELL and WAIT readings;
    - a BUY at HFM's ask closed by its take-profit (+3.612);
    - a SELL stopped through a jump (−2.10, more than the 1.80 planned);
    - too small;
    - once per candle across a restart;
    - each limit;
    - the rule's exit;
    - HFM's swap, once a night and three on Wednesday;
    - no price, no action;
    - stopping;
    - the pages;
    - Binance paused as shipped.
  - **`tests/trading/test_hfm_candles.py` (7):**
    - the server clock in summer and winter;
    - the trading day;
    - live candles from reports;
    - a frozen price makes none;
    - history rebuilt, never twice;
    - MT5's own through bridge version 4;
    - an older bridge told to update.
- **Review** (one reviewer; every finding reproduced, then fixed):
  - **Stopping with no current price (medium):** stopping, or starting a new simulation, while HFM had no current price left its trades open and unwatched.
    - Now every open position keeps its stop, target and swap checks, whatever its account's state, and a stopped account's trades close at HFM's next price.
    - Stop, start and resume take the account's lock; a decision re-checks that the account still runs.
  - **The bridge's journal (medium):** version 4 would have journaled every candle answer forever (about 9 MB a day). A read-only answer is now sent once and never kept.
  - **AI ANALYSIS WORKING (medium):** it counted failed AI calls. It now counts only successful ones.
  - **The Binance pause (low):** it also stopped the rule's exit of trades already held. Now only new entries pause; exits and stops run.
  - **Swap at a stop-out (low):** a night held was not paid when the first price after it crossed the stop. It is booked first now.
  - **A limit at once below one trade's risk (low):** it is refused with the reason.
  - **Floats in the swap (low):** the swap is counted in Decimal from HFM's own terms.
  - **A candle still forming when asked for (low):** it could have been stored as complete; the cut-off is now the time of asking.
  - **HFM's clock:** if it is not New York + 7 hours, MT5's candles are refused rather than placed wrongly.
- **Not yet:**
  - automatic HFM demo orders from the reading (next);
  - the AI's second opinion (the AI shows "not switched on");
  - the daily filter until 21 HFM daily candles exist (at once with bridge version 4, an owner step on the VM).
