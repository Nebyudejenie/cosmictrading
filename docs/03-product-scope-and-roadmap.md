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
