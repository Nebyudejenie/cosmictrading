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
  - The rule exit of a trade still open when its mandate ends is not taken over (the stop stays; the reminders ask for renewal first).
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
