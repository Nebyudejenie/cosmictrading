# 03 — Product Scope and Roadmap

## 1. First release scope

**In:** single operator; crypto spot (BTC/USD, ETH/USD) research with keyless public data from Coinbase Exchange and Bitstamp; provenance and freshness; deterministic indicators, regime, backtests and walk-forward; governed research missions with a free-only AI gateway; human-approved paper trading through the full approval → execution → reconciliation path; kill switch; append-only audit; operator portal; Telegram handlers (live bot when a token exists); deployment artefacts for a Proxmox VM with Cloudflare Tunnel/Access; backups and restore drills.

**Out (non-goals for this release):** live order submission (no broker selected; LIVE unreachable by construction); strategy-level authorisation (designed, disabled); Telegram Mini App; equities/FX/macro/news connectors; global "World Brain"/knowledge graph (baseline missing, B-01); multi-user teams; high-frequency or sub-minute trading; any paid service.

## 2. Actors

Owner/operator (human, all authority, MFA) · Analyst/viewer (optional, limited roles) · Research worker identity (propose only) · Execution authority (submit only approved intents) · External: data sources, AI providers, Telegram, Cloudflare, future broker.

## 3. Slices

Every slice: objectives, dependencies, resource impact, deliverables, tests (including failure tests), security review, rollback (revert the slice's commits + `alembic downgrade` of its revision), acceptance evidence recorded in `IMPLEMENTATION-STATUS.md`.

| Slice | Objective | Depends on | Deliverables | Acceptance |
|---|---|---|---|---|
| S1 Foundation | Reproducible base with security primitives | — | uv project, Makefile, compose.dev, config tiers, logging/redaction, migrations + roles, identity (argon2, sessions, TOTP, CSRF, rate limit), RBAC, audit chain, health, owner bootstrap CLI | Fresh setup + migrate; authz negatives; audit tamper rejected; no secrets in repo |
| S2 Market & provenance | Truthful market data | S1 | Source registry, Coinbase/Bitstamp/status connectors, ingestion jobs, freshness, quality, cross-source check, instrument master, indicators | Idempotent ingestion; stale never current; indicator reference tests |
| S3 Ledger, authority, risk | Deterministic financial core | S1, S2 | Money/precision, ledger, positions, authority modes, risk policy + checks R01–R16, sizing | Property tests; each check pass/fail/unknown |
| S4 Orders & execution | Safe execution path | S3 | Order state machine, PaperBroker, execution authority, startup recovery, reconciliation, kill switch, approved exits, protection state | AT-01/02/10/11/12/15/17/20 |
| S5 Proposals & approvals | Human authority | S4 | Intents, packets, challenges, approvals, portal approval UI, Telegram handlers | AT-03…09, AT-18; Telegram tests |
| S6 Free AI gateway | $0 AI under policy | S1 | Registry, attestations, approvals, adapter, fake provider, breaker, quota, cache, routing, safety, health page | Paid routes rejected; AT-13; AT-16 (prompts) |
| S7 Workforce & research | One full research mission | S2, S5, S6 | Task engine, 7 roles, mission pipeline, strategies, backtests, walk-forward, opportunities, reports, post-trade review | Mission integration test end to end |
| S8 Portal completion | Decision-oriented UI | S1–S7 | All views, readiness report, metrics | E2E + button inventory + no-secret HTML |
| S9 Deployment | Repeatable deploy | S1–S8 | Dockerfile, compose.prod, local verify, VM scripts, tunnel template, backups, restore drill | Local prod run, restart + crash tests, restore drill (AT-19) |
| S10 Hardening & handover | Release quality | all | Scans, full acceptance run, runbook, user guide, final report | Report with real evidence |

## 4. Next phases after this release (ordered by risk and value)

1. Owner supplies B-04/B-05 → live AI narratives and Telegram approvals in PAPER mode; run the paper/shadow period (Gate G).
2. Owner selects a broker (B-06) → sandbox adapter + contract suite → Gate C.
3. VM access (B-02) and domain (B-03) → production deployment and public verification.
4. Gate review → readiness packet → owner decides on live activation (Gate I).
5. Additional data sources and asset classes per the capability map; strategy-level authority only after all of the above.

## 5. The auto-trader: the path to evidence (owner's goal 2026-10-22: "get profit")

No rule can be promised to make money. The plan below makes it likely that real money only ever follows evidence that survives honest testing, and that a bad run never costs much.

1. **Protect (done, S24):**
   - the per-trade and daily limits;
   - the total loss stop;
   - never more than the money;
   - demo money only, enforced by the database.
2. **See before trading (done, S23):** the auto-trader's own rule replayed on the last 12 months with the owner's plan. It showed a loss on hourly bars after costs.
3. **A fair competition (done, S25, ADR-031):**
   - Replay the library's published long-or-flat rules with the auto-trader's mechanics (stop, sizing, limits), on hourly and on daily bars.
   - Pick on an earlier period and check on a later one that was never looked at.
   - Correct for trying many rules (deflated Sharpe, as the library does).
   - Only a rule that is positive after costs out of sample, with enough trades, may be offered to the auto-trader. Its rule list stays a reviewed code change (ADR-030).
4. **Prove it forward (running since 2026-10-23, ADR-031):** from a fixed start date, every candidate is scored on new hours as they arrive, so there is no hindsight. A switch of rule needs a forward record, for example 30 trades positive after costs.
5. **Size to the evidence:** keep the per-trade risk small until the forward record is long enough. Halve it automatically after a deep drawdown.
6. **Real money:** only after gate G (a demo record) and the owner's separate activation (ADR-010). There is no shortcut.

