# 02 — Requirements Traceability Matrix

**Source:** `COSMICTRADING_QUANT_MASTER_BUILD_PROMPT.md` (1,468 lines, read in full on 2026-10-10). The owner designated this file as both the product vision and the execution directive. It cites a companion baseline (`cosmictrading.md`, "Sections 0–1803") that does not exist in the repository or anywhere on the workstation. Requirements below come only from the text that exists. See [GOV-18](#gov) and `BLOCKERS.md` B-01.

Section references: `§n` = directive sections 1–24 (lines 1–718). `V6 §n` = the V6 trading-engine addendum (lines 720–1468, sections 0, 1, 1804–1835).

**Priority:** P0 = safety, financial integrity, security (first release, non-negotiable) · P1 = core function for first release · P2 = after the first release · P3 = long-term capability map.

**State:** `DONE` (process item completed) · `PLANNED` · `IN_PROGRESS` · `IMPLEMENTED` (code + passing tests; evidence in `IMPLEMENTATION-STATUS.md`) · `PARTIAL` · `BLOCKED` (external dependency, see `BLOCKERS.md`) · `DEFERRED` (with ADR) · `N/A`.

**Slice:** target implementation slice from `03-product-scope-and-roadmap.md` (S1–S10). The state column is updated after each slice.

---

## GOV — Governance and engineering process {#gov}

| ID | Requirement | Source | Pri | Module / artefact | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| GOV-01 | Read the whole vision file in chunks; keep a requirement index | §2.1, §23.2 | P0 | this document | 1,468/1,468 lines read; every section mapped below | — | DONE |
| GOV-02 | Read repository instructions, manifests, env examples, migrations, tests | §2.2 | P0 | `01-current-state-audit.md` | Audit lists every file (only the prompt existed) | — | DONE |
| GOV-03 | Inspect tree, Git status, branch, history; never overwrite user work | §2.3, §23.1 | P0 | 01 audit, ADR-001 | Repo was inside a commitless home-dir repo; standalone repo created, prompt untouched | — | DONE |
| GOV-04 | Identify runtime, frameworks, DB, auth, integrations, deployment | §2.4 | P0 | 01 audit | None existed (greenfield) | — | DONE |
| GOV-05 | Search for TODO/FIXME/mock/stub/bypass/unsafe fallback | §2.5 | P0 | 01 audit; S10 rerun | No code existed; re-run as a release check | S10 | DONE (baseline) |
| GOV-06 | Find duplicates, dead files, hard-coded secrets, drift | §2.6 | P0 | 01 audit; secret scan in S10 | None at baseline | S10 | DONE (baseline) |
| GOV-07 | Run baseline tests/lint before changes; record pre-existing failures | §2.7 | P0 | 01 audit | No tests existed; recorded as such | — | DONE |
| GOV-08 | Preserve useful working components | §2.8 | P0 | 01 audit | Nothing to preserve except the prompt (kept verbatim) | — | DONE |
| GOV-09 | Traceable requirements inventory with ID, source, dependency, priority, state, verification, limitation | §2.9 | P0 | this document | Updated after each slice | all | IN_PROGRESS |
| GOV-10 | Resolve contradictions through ADRs; stricter security/accounting/risk rule wins | §2.10 | P0 | `17-ADRs/` | ADR-002, ADR-010, ADR-014 | — | DONE |
| GOV-11 | Gate A design package (docs 00–16, ADRs, diagrams) before broad coding | §3 | P0 | `docs/` | Files exist and are cross-referenced | Gate A | IN_PROGRESS |
| GOV-12 | Gate B: build verified vertical slices; living `IMPLEMENTATION-STATUS.md` | §3 | P0 | status log | Entry per slice with real test output | all | PLANNED |
| GOV-13 | Record significant decisions as ADRs | §1 | P1 | `17-ADRs/` | ADR index | all | IN_PROGRESS |
| GOV-14 | Ask only for genuinely external inputs; write precise BLOCKER records and continue | §1, §21 | P0 | `BLOCKERS.md` | Each blocker has the six required fields | all | IN_PROGRESS |
| GOV-15 | 11-part factual report after each substantial session | §22 | P0 | `reports/` | Report file per session | S10 | PLANNED |
| GOV-16 | Per-phase report fields (IMPLEMENTED, NOT_IMPLEMENTED, TESTS_RUN, ...) | V6 §1833 | P1 | status log | Present in each slice entry | all | PLANNED |
| GOV-17 | Precedence: law/licences/security > e-stop & hard risk > human authorization > strategy mandates > operational policy > research/AI | V6 §0 | P0 | ADR-010, `trading/authority` | Tests: a lower layer cannot override a higher one (AT-07, AT-08) | S3–S5 | PLANNED |
| GOV-18 | Include the V4/V5 baseline (Sections 0–1803) with V6 | V6 §0 | P1 | ADR-002 | Baseline missing | — | BLOCKED (B-01) |
| GOV-19 | Never claim "100% perfect"; "done" needs acceptance evidence | §1, §4.13 | P0 | all reports | Reviewed in each report | all | PLANNED |
| GOV-20 | Each phase states objectives, dependencies, budget, deliverables, tests, failure tests, security review, rollback, acceptance | V6 §1832 | P1 | `03-product-scope-and-roadmap.md` | Roadmap table per slice | Gate A | IN_PROGRESS |

## PRIN — Product principles and hard limits (§4)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| PRIN-01 | Market claims show sources, timestamps, freshness, method, uncertainty | §4.1 | P0 | `evidence`, `sources`, portal | Report/packet tests assert provenance fields present | S2, S7 | PLANNED |
| PRIN-02 | Balances, positions, orders, arithmetic, permissions, limits are deterministic code | §4.2 | P0 | `trading/*`, `core/money` | Unit + property tests; no AI import in trading modules (import-contract test) | S3–S4 | PLANNED |
| PRIN-03 | Research, signals and agent consensus cannot grant authority | §4.3 | P0 | `trading/approvals` | AT-08; research role lacks capabilities | S5, S7 | PLANNED |
| PRIN-04 | Start in research/paper; live disabled until gates pass and operator enables | §4.4 | P0 | `trading/authority` | Default mode test; live transition refused (AT-01, Gate I) | S3 | PLANNED |
| PRIN-05 | Free cloud AI only; no paid calls, no Ollama, no local LLM | §4.5 | P0 | `ai` | Paid-route rejection tests; dependency scan for local-LLM packages | S6 | PLANNED |
| PRIN-06 | Label LIVE, DELAYED, SIMULATED, STALE, UNKNOWN, UNAVAILABLE | §4.6 | P0 | `sources/freshness`, portal | Freshness classification tests; UI label tests | S2, S8 | PLANNED |
| PRIN-07 | Least privilege, authentication, secrets, auditability, threat model from the start | §4.7 | P0 | `identity`, `audit`, doc 09 | Security test suite | S1 | PLANNED |
| PRIN-08 | No big-bang; small hardened slices | §4.8 | P1 | roadmap | Slice log | all | PLANNED |
| PRIN-09 | Modular monolith + workers | §4.9 | P1 | ADR-003 | Architecture review | Gate A | DONE |
| PRIN-10 | No unnecessary technology; ADR for major choices | §4.10 | P1 | ADRs | ADR-004/005/006 | Gate A | DONE |
| PRIN-11 | No destructive guessing (data, DB, DNS, nameservers, admin exposure, orders) | §4.11 | P0 | deploy scripts | Scripts default to dry-run; reviewed in S9 | S9 | PLANNED |
| PRIN-12 | No secret leakage into code, logs, commits, docs, prompts | §4.12 | P0 | `core/logging`, `ai/safety` | AT-16; secret scan; redaction tests | S1, S6, S10 | PLANNED |
| PRIN-13 | Honest completion; BLOCKED / NOT_TESTED / UNKNOWN with reasons | §4.13 | P0 | status log | Report review | all | PLANNED |
| PRIN-14 | Cost-effective: caching, deterministic work, batching, rate-aware queues | §4.14 | P1 | `ai`, `worker` | Cache/dedupe tests | S6 | PLANNED |
| PRIN-15 | Human retains capital authority | §4.15 | P0 | `trading/approvals` | AT-03, AT-04 | S5 | PLANNED |

## AI — Free cloud AI gateway (§5, V6 §1819)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| AI-01 | Provider-neutral gateway; adapters, no provider calls scattered in code | §5 | P0 | `ai/gateway`, `ai/adapters` | Import-contract test: only `ai.adapters` imports HTTP for providers | S6 | PLANNED |
| AI-02 | Verify $0 pricing, quota, eligibility, data handling, context, terms from official docs; record review date | §5 | P0 | `config/ai_registry.toml`, doc 06 | Registry entries carry `evidence_url`, `reviewed_on`; review dated 2026-10-10 | S6 | PLANNED |
| AI-03 | Explicit allowlist of providers and model IDs | §5 | P0 | registry + `ai_model_approvals` | Unlisted model rejected test | S6 | PLANNED |
| AI-04 | Reject models whose free status is not verified | §5 | P0 | `ai/policy` | Unverified/expired attestation rejected test | S6 | PLANNED |
| AI-05 | No paid endpoints or paid fallback routing | §5 | P0 | `ai/policy`, adapter | OpenRouter non-`:free` and priced models rejected; no `models` fallback sent | S6 | PLANNED |
| AI-06 | Never upgrade plans, add credits, attach cards, create chargeable resources | §5 | P0 | design | No billing API used anywhere (code review + grep) | S6 | PLANNED |
| AI-07 | Inference budget exactly $0; fail closed when eligibility uncertain | §5, §18 | P0 | `ai/policy` | HTTP 402 or reported cost > 0 disables provider + incident (test) | S6 | PLANNED |
| AI-08 | Per-provider circuit breaker | §5 | P1 | `ai/breaker` | Breaker state-machine tests | S6 | PLANNED |
| AI-09 | Quota tracker and token/request usage ledger | §5 | P1 | `ai_usage`, `ai/quota` | Usage rows per call; quota exhaustion test | S6 | PLANNED |
| AI-10 | Timeouts, bounded retries with jitter, `Retry-After` support | §5 | P1 | `ai/adapters` | Fake-transport tests for 429 + Retry-After | S6 | PLANNED |
| AI-11 | No retry storms; respect terms; never multiply accounts to evade limits | §5 | P0 | `ai/quota`, doc 06 | Max-attempt test; policy text | S6 | PLANNED |
| AI-12 | Task routing: small models for classification, stronger for research, code for math, reviewer only when justified | §5 | P1 | `ai/routing` | Routing table tests | S6 | PLANNED |
| AI-13 | Cache with explicit expiry; never present expired output as current | §5 | P1 | `ai_cache` | Expiry test | S6 | PLANNED |
| AI-14 | Batch, deduplicate, compress context; pass evidence references | §5 | P2 | `ai/gateway`, `workforce` | Dedupe-by-key test | S6–S7 | PLANNED |
| AI-15 | AI optional for safety; queue AI work and report degraded status when all providers fail | §5 | P0 | `ai`, `workforce` | AT-13 | S6 | PLANNED |
| AI-16 | Keys server-side only; never in browser, Telegram, logs, prompts | §5 | P0 | `core/config`, `ai/safety` | AT-16 | S6 | PLANNED |
| AI-17 | Do not send credentials, sensitive personal data or account details to providers | §5 | P0 | `ai/safety` (classification + outbound scanner) | Outbound-scan block test | S6 | PLANNED |
| AI-18 | Tests prove paid model IDs/routes are rejected; fake provider; no live calls in unit tests | §5 | P0 | `tests/ai` | Test suite | S6 | PLANNED |
| AI-19 | Provider health page: model, review date, last success, rate-limit state, degraded, last error — never the key | §5 | P1 | portal `/ai` | E2E page test asserts no key material | S6, S8 | PLANNED |
| AI-20 | No "unlimited" promises; prioritise by decision value under finite quota | §5 | P1 | `ai/quota`, docs | Priority queue test | S6 | PLANNED |
| AI-21 | Obey `FREE_CLOUD_AI_ONLY`; disclose quota/privacy volatility | V6 §1819 | P0 | config, portal | Policy enum has only `FREE_CLOUD_AI_ONLY`; UI disclosure | S6 | PLANNED |
| AI-22 | On exhaustion: pause AI-researched proposals; use cached verified research; deterministic controls continue | V6 §1819 | P0 | `workforce`, `ai` | AT-13 | S7 | PLANNED |
| AI-23 | AI never required for hard risk, approval validation, order persistence, idempotency, reconciliation, secrets, e-stop, audit | V6 §1819 | P0 | import contracts | Test: `trading` and `audit` packages do not import `ai` | S3–S6 | PLANNED |
| AI-24 | Show execution/data costs separately from free AI | V6 §1819 | P1 | packet, doc 16 | Packet shows fees; cost doc | S5 | PLANNED |

## DATA — Sources, provenance and evidence (§6, V6 §1829)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| DATA-01 | Treat global-coverage sections as a capability map, not first-release scope | §6 | P1 | doc 03 | Scope doc | Gate A | DONE |
| DATA-02 | Source connectors behind a common interface | §6 | P0 | `sources/contract` | Protocol conformance tests | S2 | PLANNED |
| DATA-03 | Per-source metadata (publisher, jurisdiction, language, access, licence, frequency, timestamps, units, quality, missingness, bias, last success, freshness limit, fallback, owner) | §6 | P0 | `sources` table | Schema test; registry seed has all fields | S2 | PLANNED |
| DATA-04 | Source registry and provenance first | §6.1 | P0 | `sources` | Every market row references a source + fetch run | S2 | PLANNED |
| DATA-05 | Small set of lawful, accessible sources for first markets | §6.2 | P0 | Coinbase Exchange public, Bitstamp public, Coinbase status (ADR-015) | Recorded fixtures; terms notes | S2 | PLANNED |
| DATA-06 | Validation, normalisation, dedup, timezone/unit handling | §6.3 | P0 | `sources/normalize` | OHLC sanity + idempotent upsert tests | S2 | PLANNED |
| DATA-07 | Historical snapshots and source-vintage awareness | §6.4 | P1 | `fetch_runs`, `bars.fetch_run_id` | Backtest records data vintage | S2, S7 | PLANNED |
| DATA-08 | Material-change detection; evidence and claim registry | §6.5 | P1 | `market/events`, `evidence` | Detector tests; claim status tests | S7 | PLANNED |
| DATA-09 | Canonical entity/instrument identity (ticker is not an ID) | §6.6 | P0 | `market/instruments` | Listing-map tests | S2 | PLANNED |
| DATA-10 | Search, claim verification, contradictions, change-first reports | §6.7 | P2 | `evidence`, `research/report` | Report test (search beyond SQL filters deferred) | S7 | PLANNED |
| DATA-11 | More asset classes only after licence/quality/value evaluation | §6.8 | P3 | doc 07 | Capability map | — | DEFERRED |
| DATA-12 | Respect ToS, rate limits, robots; no prohibited scraping; no false real-time claims | §6 | P0 | connectors | Rate limiter tests; data labelled DELAYED | S2 | PLANNED |
| DATA-13 | Unavailable/paid source → adapter contract + blocker + labelled alternative | §6 | P1 | `BLOCKERS.md` | Kraken/Binance unreachable recorded (B-09) | S2 | PLANNED |
| DATA-14 | Reports answer: changed, matters, verified, inferred, contradicts, unknown, timestamps, freshness, why | §6 | P1 | `research/report` | Report schema test | S7 | PLANNED |
| DATA-15 | Per provider/instrument: licence, entitlement, frequency, latency, timezone, currency, revisions, fields | V6 §1829 | P1 | `sources`, `instruments` | Registry completeness test | S2 | PLANNED |
| DATA-16 | Distinguish broker values, live licensed, delayed public, historical, estimates, simulated | V6 §1829 | P0 | `sources/freshness`, `core/labels` | Label tests | S2 | PLANNED |
| DATA-17 | Never show simulated as actual; timestamp + freshness next to market values; block dependent actions | V6 §1829 | P0 | portal, risk | AT-14; UI tests | S2, S8 | PLANNED |
| DATA-18 | Expose coverage gaps rather than imply global coverage | V6 §1829 | P1 | portal `/sources` | Page lists NOT_CONFIGURED sources | S8 | PLANNED |

## WF — AI workforce and task orchestration (§7)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| WF-01 | Digital employees are governed roles and workflows | §7 | P1 | `workforce/roles` | Role registry tests | S7 | PLANNED |
| WF-02 | Each role declares mission, scope, inputs, output schema, tools, data class, permissions, SLA, retry, budget, evidence, gates, reviewer, metrics, failure modes, retirement | §7 | P1 | `workforce/roles` | Completeness test over every role | S7 | PLANNED |
| WF-03 | Start with 7 roles: orchestrator, source/data analyst, market researcher, quant analyst, risk reviewer, task reviewer, ops/security monitor | §7 | P1 | `workforce/roles` | Registry contains exactly these | S7 | PLANNED |
| WF-04 | Task engine: owner, status, priority, deadline, dependencies, blocker, evidence, artefact, acceptance criteria, attempts, reviewer, idempotency, escalation, expiry, reopening | §7 | P1 | `workforce/tasks` | Task lifecycle tests | S7 | PLANNED |
| WF-05 | Completion requires verification, not an agent claim | §7 | P0 | `workforce/review` | Task cannot reach DONE without passing review | S7 | PLANNED |
| WF-06 | Prevent loops, duplicates, cross-agent injection, uncontrolled tools, unbounded delegation | §7 | P0 | `workforce/tasks` | Depth/budget/idempotency tests | S7 | PLANNED |
| WF-07 | Web pages, PDFs, tool output and commentary are untrusted data | §7 | P0 | `ai/safety` | Injection fixture test | S6–S7 | PLANNED |
| WF-08 | One complete research mission: detection → assignment → evidence → review → report → archive | §19 Ph5 | P1 | `workforce/missions` | Integration test with fake AI | S7 | PLANNED |

## QNT — Market research and quantitative system (§8, V6 §1826)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| QNT-01 | Instrument master: venue, symbol, currency, tick/lot, multiplier, sessions, timezone, expiry, settlement | §8 | P0 | `market/instruments` | Sync from Coinbase product metadata; precision tests | S2 | PLANNED |
| QNT-02 | Bars/quotes with source timestamp, ingestion timestamp, delay, quality state | §8 | P0 | `bars`, `quotes` | Schema + ingestion tests | S2 | PLANNED |
| QNT-03 | Deterministic indicators with reference tests | §8 | P1 | `market/indicators` | Hand-computed and cross-implementation reference tests | S2 | PLANNED |
| QNT-04 | Multi-timeframe market state and regime classification with explicit method | §8 | P1 | `market/regime` | Method documented in doc 07; tests | S7 | PLANNED |
| QNT-05 | Fundamentals and macro calendar where sources exist | §8 | P2 | — | Not applicable to the first crypto slice | — | DEFERRED |
| QNT-06 | Volatility, correlation, exposure, drawdown | §8 | P1 | `market/stats`, `trading/risk` | Reference tests | S2–S3 | PLANNED |
| QNT-07 | Position-sizing helpers with caps and uncertainty haircuts | §8 | P0 | `trading/sizing` | Property tests: size never exceeds caps | S3 | PLANNED |
| QNT-08 | Strategy/experiment registry, versioning, reproducible backtests, walk-forward/OOS | §8 | P1 | `research` | Reproducibility test (same inputs → same hash/result) | S7 | PLANNED |
| QNT-09 | Realistic fee, spread, slippage, funding, borrow, liquidity assumptions | §8 | P1 | `research/backtest`, paper broker | Assumptions recorded per run; labelled ASSUMPTION | S4, S7 | PLANNED |
| QNT-10 | Look-ahead, survivorship, corporate-action, multiple-testing safeguards | §8 | P1 | `research/backtest` | Leakage test (future perturbation does not change past decisions); trial count disclosed | S7 | PLANNED |
| QNT-11 | Performance decomposition: thesis, timing, sizing, execution, fees, luck | §8 | P2 | `trading/review` | Post-trade review fields | S7 | PARTIAL target |
| QNT-12 | Opportunity lifecycle IDEA → … → REVIEW | §8 | P1 | `research/opportunities` | State-machine tests | S7 | PLANNED |
| QNT-13 | No false profitability claims; disclose sample limits, drawdown, costs, uncertainty | §8 | P0 | backtest report | Report includes disclaimers + limits | S7 | PLANNED |
| QNT-14 | Post-trade review: planned vs actual (thesis, timing, costs, risk, execution, authorization, decision vs outcome) | V6 §1826 | P1 | `trading/review` | Review created after close | S7 | PLANNED |
| QNT-15 | Never rewrite original forecasts; learning cannot silently change a live mandate | V6 §1826 | P0 | `trading/review` | Immutable original packet; review is a separate row | S7 | PLANNED |

## TRD — Human-approved trading engine (§9, V6 §1804–§1818, §1821–§1822, §1827, §1830, §1834)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| TRD-01 | Seven authority modes, persisted and audited | §9, V6 §1804 | P0 | `trading/authority` | Mode transition tests; audit rows | S3 | PLANNED |
| TRD-02 | RESEARCH_ONLY and PAPER_TRADING have no live submission authority | V6 §1804 | P0 | `trading/execution` | AT-01, AT-02 | S4 | PLANNED |
| TRD-03 | APPROVAL_REQUIRED default for live; STRATEGY_AUTHORIZED disabled by default | V6 §1804 | P0 | `trading/authority` | AT-08 | S3 | PLANNED |
| TRD-04 | Configuration change cannot silently make the mode more permissive | V6 §1804 | P0 | `trading/authority`, `core/config` | Env cap test: DB mode above cap forces PROTECTION_ONLY + incident | S3 | PLANNED |
| TRD-05 | Separate capabilities: READ_MARKET_DATA, READ_ACCOUNT, RESEARCH, PROPOSE_TRADE, APPROVE_TRADE, SUBMIT_ORDER, CANCEL_ORDER, MANAGE_APPROVED_POSITION, CHANGE_RISK_LIMITS, ENABLE_STRATEGY_AUTHORITY, RECONCILE, ADMINISTER_SYSTEM | V6 §1804 | P0 | `identity/capabilities` | Capability matrix test (one does not imply another) | S1 | PLANNED |
| TRD-06 | LLM/research workers get no broker secrets or order submission; execution accepts only typed, validated, authorised intents | V6 §1804, §1820 | P0 | `workforce`, `trading/execution` | Import contract + capability tests | S4, S7 | PLANNED |
| TRD-07 | Candidate trade carries identity, venue, currency, account, session, direction, exposure, horizon, thesis, evidence + timestamps, contradictions, red team, entry, order types, quantity, price constraints, costs, portfolio impact, risk, liquidity, events, exit, invalidation, expiry, re-approval conditions, operational checks, reasons for WAIT/NO_TRADE | V6 §1805 | P0 | `trading/proposals` | Packet schema completeness test | S5 | PLANNED |
| TRD-08 | Weak evidence, unverifiable price, ambiguous identity, unsupported venue, no risk calc → NON_EXECUTABLE, no approval request | V6 §1805 | P0 | `trading/proposals` | Negative tests | S5 | PLANNED |
| TRD-09 | Pre-approval deterministic validation of the 12 listed areas | V6 §1806 | P0 | `trading/risk` | One test per check, pass and fail | S3 | PLANNED |
| TRD-10 | Any failed mandatory check blocks or marks NON_EXECUTABLE; plain-language and technical reasons | V6 §1806 | P0 | `trading/risk`, portal | AT-18 | S3, S5 | PLANNED |
| TRD-11 | AI cannot override risk; limit changes only via separate authenticated MFA workflow with reason | V6 §1806 | P0 | `trading/risk/policy` | Policy-change tests (MFA required, reason required) | S3 | PLANNED |
| TRD-12 | Approval packet contents (TRADE_ID, account alias, mode, instrument, venue, direction, horizon, quantity, order types, limit/band, slippage, notional, fees, holdings, impact, max risk + assumptions, loss warning, thesis, evidence, contradictions, scenarios, red team, entry, exit, invalidation, management permissions, prohibited actions, check states, expiries, consequences) | §9, V6 §1807 | P0 | `trading/proposals/packet` | Schema test | S5 | PLANNED |
| TRD-13 | Plain language first; detail on demand; no single opaque score | V6 §1807 | P1 | portal, Telegram | UI tests | S5 | PLANNED |
| TRD-14 | Approval impossible if packet fields missing or stale; no generic approval text | V6 §1807 | P0 | `trading/approvals` | Negative tests | S5 | PLANNED |
| TRD-15 | Approval bound to TRADE_ID, packet hash, account, venue, instrument, side, quantity, exposure effect, order types, price band, slippage, max notional, policy version, validation result, management actions, times, operator, nonce | V6 §1808 | P0 | `approvals` table | AT-04, AT-05 | S5 | PLANNED |
| TRD-16 | Silence, expiry, timeout, ambiguity, "looks good" never approve; rejected/revoked/expired/consumed/superseded unusable | V6 §1808 | P0 | `trading/approvals` | Negative tests | S5 | PLANNED |
| TRD-17 | Server-side single-use approval record; client status and Telegram callback data are references, not credentials | V6 §1808 | P0 | `trading/approvals`, `notify/telegram` | Forged-callback test | S5 | PLANNED |
| TRD-18 | Material change after approval → stop and require new approval; no silent resize/reverse/substitute/reprice | V6 §1808 | P0 | `trading/execution` | AT-05 | S4–S5 | PLANNED |
| TRD-19 | Execution independently re-checks authorisation and risk immediately before submission | V6 §1808 | P0 | `trading/execution` | AT-07 | S4 | PLANNED |
| TRD-20 | 12-step execution procedure (load, reconcile, revalidate, unique intent/client ID, build from approved params only, submit, persist before/after, monitor, reconcile fills, approved exits only, notify actual state, audit + review) | V6 §1809 | P0 | `trading/execution` | E2E paper test | S4 | PLANNED |
| TRD-21 | Never claim placed/accepted/filled/cancelled/protected without broker confirmation | V6 §1809 | P0 | `trading/orders`, notify | AT-17 | S4 | PLANNED |
| TRD-22 | Only explicitly approved management actions; prohibited actions list enforced | V6 §1810 | P0 | `trading/management` | Reduce-only and quantity-cap tests | S4 | PLANNED |
| TRD-23 | Strategy-level authorisation: separate, versioned, bounded mandate; disabled by default | V6 §1811 | P2 | `trading/authority` | Mode refuses activation (AT-08); mandate execution not built | S3 | PLANNED (disabled) |
| TRD-24 | Order state machine with broker-grounded transitions and full event history | V6 §1812, §9 | P0 | `trading/orders` | Transition table tests | S4 | PLANNED |
| TRD-25 | Uncertain state: mark, block, query by client ID, reconcile, resolve from evidence, escalate; never resubmit blindly | V6 §1812 | P0 | `trading/execution`, `reconcile` | AT-11, AT-20 | S4 | PLANNED |
| TRD-26 | Duplicate/race defence: immutable IDs, idempotency keys, DB constraints, single serialized execution authority; eight listed races | V6 §1813 | P0 | `trading/execution` | AT-09; race tests per listed scenario | S4 | PLANNED |
| TRD-27 | Broker adapter interface documenting operations, auth, freshness, order types, idempotency, rate limits, venue status, partial fills, fees, precision, reconciliation | V6 §1814 | P0 | `trading/brokers/base` | Contract test suite run against PaperBroker | S4 | PLANNED |
| TRD-28 | No claimed integration without implementation and tests against official docs/sandbox | V6 §1814 | P0 | docs | Only `paper` adapter exists | S4 | PLANNED |
| TRD-29 | Credential rules: dedicated account, minimum scopes, no withdrawals, secret storage, rotation/revocation, network allowlist | V6 §1814 | P0 | doc 08, `core/config` | Design + config validation; live keys BLOCKED | S9 | BLOCKED (B-07) for live |
| TRD-30 | Portfolio-level veto evaluated on the post-trade portfolio immediately before submission | V6 §1815 | P0 | `trading/risk/portfolio` | AT-07 | S3–S4 | PLANNED |
| TRD-31 | Each limit defines source, currency, timezone/session, reset, fee treatment, unrealised treatment, unavailable-data action; unknown risk ≠ zero | V6 §1815 | P0 | `trading/risk/policy` | Policy schema test; UNKNOWN ⇒ FAIL tests | S3 | PLANNED |
| TRD-32 | Limit changes audited and never retroactive | V6 §1815 | P0 | `trading/risk/policy` | Approval bound to policy version; version change invalidates approval | S3, S5 | PLANNED |
| TRD-33 | Honest monitoring cadence; actual vs estimated; protective-order status; material alerts with prescribed content | V6 §1816 | P1 | `trading/monitor`, notify | Alert content test | S4 | PLANNED |
| TRD-34 | Kill switch: 8-step procedure, portal and Telegram, LLM-independent | V6 §1817 | P0 | `trading/killswitch` | AT-12; Gate F | S4 | PLANNED |
| TRD-35 | Kill switch does not blindly cancel protective orders or liquidate; owner-defined emergency behaviour | V6 §1817 | P0 | `trading/killswitch` | Protective order survives test | S4 | PLANNED |
| TRD-36 | Reconcile broker orders → fills → positions → cash → ledger → account state; broker authoritative; no guessed repair; stay protected until verified; explicit recovery event | V6 §1818 | P0 | `trading/reconcile` | AT-15; recovery checklist tests | S4 | PLANNED |
| TRD-37 | Validate min/max quantity, lot, tick, precision, order type, TIF, venue rules with decimals | V6 §1821 | P0 | `trading/precision` | Property tests | S3 | PLANNED |
| TRD-38 | Pre-submission: session, identity, price band, spread/liquidity, rounding, TIF, halts/incidents | V6 §1821 | P0 | `trading/risk` | Tests per check | S3–S4 | PLANNED |
| TRD-39 | Never round into materially different exposure; delayed data only if policy + operator accept | V6 §1821 | P0 | `trading/precision`, policy | Rounding-tolerance test; `allow_delayed_data` flag | S3 | PLANNED |
| TRD-40 | Partial fills are real exposure; recompute from broker values | V6 §1822 | P0 | `trading/orders`, ledger | AT-10 | S4 | PLANNED |
| TRD-41 | Cancel request ≠ cancellation; replace ≠ replaced; cancel/fill race → reconcile | V6 §1822 | P0 | `trading/orders` | Race test | S4 | PLANNED |
| TRD-42 | Protective order failure: mark degraded, block risk, alert, bounded deterministic retry, never widen | V6 §1822 | P0 | `trading/management` | Rejected-stop test | S4 | PLANNED |
| TRD-43 | Support venues without bracket/OCO; no false atomic claim | V6 §1822 | P1 | `trading/management` | Separate-order protection with overfill guard test | S4 | PLANNED |
| TRD-44 | Live release gates (§9 list) | §9 | P0 | `trading/golive` | Readiness report shows each gate | S8 | PLANNED |
| TRD-45 | Go-live gates A–I recorded with evidence | V6 §1830 | P0 | `golive_gates` | Readiness report | S8, S10 | PLANNED |
| TRD-46 | Live enabled only by a separate authenticated owner action after readiness report; never automatic | V6 §1830 Gate I | P0 | `trading/authority` | Live transition refused while any gate unmet | S3, S8 | PLANNED |
| TRD-47 | Default sequence DISCOVER → … → LEARN; strategy sequence only when enabled | V6 §1834 | P0 | `trading`, `workforce` | E2E test follows sequence | S7 | PLANNED |
| TRD-48 | Absolute invariants (13 listed) | V6 §1834 | P0 | `trading` | Mapped one-to-one to tests in doc 13 | S3–S7 | PLANNED |
| TRD-49 | Failure modes and default safe behaviour (V6 §1827 list) | V6 §1827 | P0 | `trading`, `worker` | Failure-mode test matrix in doc 13 | S4–S10 | PLANNED |
| TRD-50 | Explicit emergency policy for open positions; fail-closed ≠ liquidate | V6 §1827 | P0 | risk policy `emergency` block | Config + test | S4 | PLANNED |
| TRD-51 | Risk/accounting vetoes for unknown positions/orders/cash, failed reconciliation, stale prices, missing data, limits, market access, venue outage, unauthorised instrument, liquidity, compliance, security incident, e-stop | §9 | P0 | `trading/risk` | One test per veto | S3 | PLANNED |
| TRD-52 | Proposal, approval, decision, policy version, checks, request, response, fills, exits, fees, positions, P&L, reconciliation, review are audit artefacts | §9 | P0 | `audit`, `order_events` | Audit completeness test on E2E flow | S4–S5 | PLANNED |
| TRD-53 | Without a configured broker: finish contract, simulator, tests, UI, docs; keep LIVE disabled; name the missing configuration | §9 | P0 | `trading/brokers/paper` | Only paper adapter registered | S4 | PLANNED |

## ARC — Application architecture (§10)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| ARC-01 | Simplest architecture: modular monolith + worker processes | §10 | P1 | ADR-003 | Doc 04 | Gate A | DONE |
| ARC-02 | Boundaries: portal, Telegram, API, scheduler/worker, AI gateway, connectors, research/backtest, risk/approval, broker/execution, ledger/reconciliation, evidence, audit/observability, admin | §10 | P1 | package layout | Import-contract tests | S1+ | PLANNED |
| ARC-03 | Domain logic separate from web, chat, SDKs, DB; no duplicated logic in bot and portal | §10 | P0 | services layer | Bot and portal call the same service functions (test) | S5 | PLANNED |
| ARC-04 | Explicit contracts, migrations, config validation, typed schemas | §10 | P0 | `db/migrations`, pydantic | Schema-drift test | S1 | PLANNED |
| ARC-05 | PostgreSQL; Redis only on measured need | §10 | P1 | ADR-005 | Compose has no Redis | S1 | PLANNED |
| ARC-06 | Language/framework rationale in ADRs | §10 | P1 | ADR-004, ADR-006 | — | Gate A | DONE |
| ARC-07 | Domain entities (users … notifications) | §10 | P1 | doc 05 | ERD | Gate A | IN_PROGRESS |
| ARC-08 | Canonical IDs; UTC storage; source/local timezone metadata | §10 | P0 | `core/ids`, schema | `timestamptz` everywhere (schema test) | S1 | PLANNED |
| ARC-09 | Decimal/fixed precision for money; no binary float for ledger truth | §10 | P0 | `core/money`, `NUMERIC` | Schema test: no float columns in financial tables | S3 | PLANNED |
| ARC-10 | Unique constraints, FKs, idempotency keys, transaction boundaries; store minimal sensitive data | §10 | P0 | schema | Constraint tests | S1–S4 | PLANNED |

## UX — Operator portal (§11, V6 §1824)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| UX-01 | Home answers: what changed, what matters, verified vs inferred, uncertain/contradictory, active risks/incidents, work running/blocked/overdue, trades awaiting approval, human attention, health/freshness | §11 | P1 | portal `/` | E2E page test checks each panel | S8 | PLANNED |
| UX-02 | Views: command center; sources/evidence; research/tasks; market/strategy; portfolio/risk/ledger; approval queue; orders/fills/reconciliation/audit; AI providers; operations/settings/access | §11 | P1 | portal | Route tests | S8 | PLANNED |
| UX-03 | Every button has real behaviour and states; no fake metrics, charts or simulated-as-live data | §11 | P0 | portal | Button inventory test (each form posts to a handled route) | S8 | PLANNED |
| UX-04 | Accessibility, responsive layout, validation, readable tables, keyboard navigation, useful errors | §11 | P1 | templates/CSS | Semantic HTML, labels, focus styles; manual check documented | S8 | PLANNED |
| UX-05 | Trading control center: mode + live indicator, broker health, balances/positions/orders/fills/last reconciliation, proposal queue, approval detail, order lifecycle, exposure/P&L/drawdown/limits, mandates, kill switch/protection/recovery, mismatches/errors/provider status/freshness/audit | V6 §1824 | P0 | portal `/trading` | E2E | S8 | PLANNED |
| UX-06 | Simple language default; drill-down; blocked orders never hidden behind green; app health ≠ account health | V6 §1824 | P0 | portal | UI test separates the two health panels | S8 | PLANNED |

## TG — Telegram (§11, V6 §1823)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| TG-01 | Verify operator by immutable Telegram user ID; optional extra authentication | §11 | P0 | `notify/telegram` | Wrong-user test; TOTP-in-chat option | S5 | PLANNED |
| TG-02 | Never approve from group or untrusted messages | §11 | P0 | handlers | Group-chat rejection test | S5 | PLANNED |
| TG-03 | Approval actions expire, resist replay, are audited | §11 | P0 | handlers | Replay test | S5 | PLANNED |
| TG-04 | Portal works when bot credentials are unavailable | §11 | P0 | portal | Bot disabled by default; portal E2E | S5 | PLANNED |
| TG-05 | Proposal message: summary + REVIEW DETAILS, EVIDENCE, COUNTERARGUMENTS, RISK, PORTFOLIO IMPACT views | V6 §1823 | P1 | handlers | Handler tests | S5 | PLANNED |
| TG-06 | APPROVE EXACT TRADE / REJECT / REQUEST CHANGES; exact terms on confirmation; second confirmation | V6 §1823 | P0 | handlers | Two-step test | S5 | PLANNED |
| TG-07 | No ambiguous controls; approvals never inferred from another channel | V6 §1823 | P0 | handlers | Free-text "ok" ignored test | S5 | PLANNED |
| TG-08 | No secrets/unneeded identifiers in chats; beginner + detail views; distinct status labels | V6 §1823 | P0 | handlers | Output scan test | S5 | PLANNED |
| TG-09 | Kill switch in Telegram | V6 §1817 | P0 | handlers | Test | S5 | PLANNED |
| TG-10 | Telegram Mini App as a primary interface | V6 header | P2 | — | ADR-021 | — | DEFERRED |
| TG-11 | Live bot operation | — | P1 | `bot` process | Needs bot token + operator ID | S5 | BLOCKED (B-05) |

## SEC — Security and privacy (§12, V6 §1820, §1825)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| SEC-01 | Threat model covering the 14 listed threat classes before public exposure | §12 | P0 | doc 09 | Review | Gate A | IN_PROGRESS |
| SEC-02 | Strong auth, RBAC, secure cookies, CSRF, rate limits, input validation | §12 | P0 | `identity`, `web` | Security suite | S1 | PLANNED |
| SEC-03 | No hard-coded credentials; `.env.example`; gitignore; secret scanning | §12 | P0 | repo | Secret scan in `make check` | S1 | PLANNED |
| SEC-04 | Dependency pinning and scanning | §12 | P0 | `uv.lock`, pip-audit | Scan output in status | S1, S10 | PLANNED |
| SEC-05 | Security headers | §12 | P0 | `web/security` | Header test | S1 | PLANNED |
| SEC-06 | Audit logging for privileged and financial actions | §12 | P0 | `audit` | Audit-coverage test | S1+ | PLANNED |
| SEC-07 | Encryption in transit; DB access control | §12 | P0 | tunnel TLS; DB roles | Role-grant tests | S1, S9 | PLANNED |
| SEC-08 | Backups protected; log redaction | §12 | P0 | backup scripts; logging | Redaction test; encrypted dump | S1, S9 | PLANNED |
| SEC-09 | MFA for privileged administration | §12 | P0 | `identity/mfa` | Step-up tests | S1 | PLANNED |
| SEC-10 | Separate dev/test/paper/live credentials | §12 | P0 | `core/config` | Config validation test | S1 | PLANNED |
| SEC-11 | Workers get allowlisted tools; research has no broker tool | §12 | P0 | `workforce/tools` | Role-tool test | S7 | PLANNED |
| SEC-12 | Research output never executes without typed validation + authorisation + risk | §12 | P0 | `trading` | AT-08 | S5–S7 | PLANNED |
| SEC-13 | Untrusted content cannot change permissions, approvals, limits, strategy authority, config, credentials | V6 §1820 | P0 | `ai/safety` | Injection test | S6–S7 | PLANNED |
| SEC-14 | No shell/DB-write tools for research; LLM never builds broker payloads; rebuild from approved data | V6 §1820 | P0 | `trading/execution` | Builder ignores extra fields test | S4 | PLANNED |
| SEC-15 | SSRF protection for research URL fetching | §12 | P0 | `sources/http` | Private-IP and scheme block tests | S2 | PLANNED |
| SEC-16 | Tamper-evident append-only execution audit with the listed contents | V6 §1825 | P0 | `audit` (hash chain + triggers) | Tamper test | S1 | PLANNED |
| SEC-17 | No credentials in audit; stable IDs, timezone timestamps, correlation IDs; detect gaps/ordering | V6 §1825 | P0 | `audit` | Gap-detection test | S1 | PLANNED |
| SEC-18 | Application code cannot rewrite, delete or backdate audit history | V6 §1825 | P0 | DB grants + triggers | UPDATE/DELETE/TRUNCATE rejected test | S1 | PLANNED |
| SEC-19 | Incident response | §12 | P1 | `incidents`, doc 15 | Incident creation tests; runbook | S4, S10 | PLANNED |
| SEC-20 | Webhook forgery, flooding, quota exhaustion | §12 | P0 | no inbound webhooks (long polling); rate limits | Rate-limit test | S1, S5 | PLANNED |

## DEP — Proxmox VM deployment (§13, V6 §1828)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| DEP-01 | Dedicated VM, not the hypervisor host; no destructive hypervisor changes | §13 | P0 | doc 12 | Design | S9 | PLANNED |
| DEP-02 | Inspect VM inventory only where access exists; don't assume credentials | §13 | P0 | 01 audit | No Proxmox access exists | — | BLOCKED (B-02) |
| DEP-03 | Supported Linux distribution | §13 | P1 | Ubuntu 24.04 LTS (ADR-017) | — | S9 | PLANNED |
| DEP-04 | Realistic CPU/RAM/disk: minimum, recommended, observed | §13 | P0 | doc 16 | Observed numbers from local run | S9 | PLANNED |
| DEP-05 | Separate storage for database, app data, backups | §13 | P1 | compose volumes, VM disks | Compose review | S9 | PLANNED |
| DEP-06 | Non-root application user | §13 | P0 | Dockerfile | `docker inspect` user check | S9 | PLANNED |
| DEP-07 | Docker Compose with pinned images (no `latest`) | §13 | P0 | `deploy/compose.prod.yaml` | Digest pin check script | S9 | PLANNED |
| DEP-08 | Health checks, restart policies, limits, named volumes, clean shutdown | §13 | P0 | compose | Local verification | S9 | PLANNED |
| DEP-09 | VM autostart on host boot; services start on VM boot | §13 | P1 | `qm set --onboot`, systemd unit | Local restart test; VM part BLOCKED | S9 | PLANNED |
| DEP-10 | DB, Redis, metrics, SSH, internal APIs never on public hostnames | §13 | P0 | compose networks, tunnel ingress | No published ports test | S9 | PLANNED |
| DEP-11 | Guest and Proxmox firewalls consistent; avoid lockout | §13 | P1 | `deploy/vm/firewall.sh` | Dry-run output | S9 | PLANNED |
| DEP-12 | Document IP, routing, DNS, outbound access, recovery | §13 | P1 | doc 12 | Doc | S9 | PLANNED |
| DEP-13 | Idempotent deploy/update scripts with dry-run/confirmation | §13 | P0 | `deploy/deploy.sh` | Run twice locally | S9 | PLANNED |
| DEP-14 | No destructive operations without confirmation and rollback | §13 | P0 | scripts | Review | S9 | PLANNED |
| DEP-15 | Measure real resource use; reduced profile if needed | §13 | P0 | doc 16 | `docker stats` capture | S9 | PLANNED |
| DEP-16 | 10 GB RAM / 200 GB SSD plan with hard budgets; background work yields to safety | V6 §1828 | P0 | compose limits, `worker` pressure checks | Budget table + pressure test | S9 | PLANNED |
| DEP-17 | Bounded queues, backpressure, timeouts, breakers, log rotation, storage thresholds, incremental ingestion | V6 §1828 | P1 | `worker`, compose logging | Tests + config | S2, S9 | PLANNED |
| DEP-18 | Actual deployment to the Proxmox VM | §13, §19 Ph10 | P0 | — | Needs host access | S9 | BLOCKED (B-02) |

## CF — Cloudflare Tunnel, DNS and domain (§14)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| CF-01 | Don't guess a domain or reuse other apps' domains | §14.1 | P0 | ADR-018 | No domain configured | — | DONE (none used) |
| CF-02 | No nameserver changes or record deletion without authorisation, inventory, rollback | §14.2 | P0 | doc 12 | Procedure documented | S9 | PLANNED |
| CF-03 | Dedicated hostnames only as needed; private services stay private | §14.3 | P0 | ingress template | Template review | S9 | PLANNED |
| CF-04 | Origin bound to private network; no bypass of the tunnel | §14.4 | P0 | compose (no published ports) | Port test | S9 | PLANNED |
| CF-05 | Tunnel token is a secret file; least-privilege tokens; never printed | §14.5 | P0 | `--token-file` | Compose review | S9 | PLANNED |
| CF-06 | Catch-all rule rejects unknown hostnames; explicit routes | §14.6 | P0 | `deploy/cloudflared/config.example.yml` | `cloudflared tunnel ingress validate` locally | S9 | PLANNED |
| CF-07 | Application auth even behind Cloudflare; Access for operator surfaces | §14.7 | P0 | `identity`; Access policy doc | App auth tests | S9 | PLANNED |
| CF-08 | Verify CNAME, tunnel health, TLS, auth, origin isolation, unapproved hostnames | §14.8 | P0 | verification script | Needs domain | S9 | BLOCKED (B-03) |
| CF-09 | Document manual steps when API access is missing; don't pretend DNS is done | §14.9 | P0 | doc 12 | Doc | S9 | PLANNED |
| CF-10 | Separate DEV/STAGING/PRODUCTION; never point production at an untested build | §14.10 | P1 | doc 12 | Procedure | S9 | PLANNED |
| CF-11 | No public SSH hostname without client-side access + strong auth | §14.11 | P0 | doc 12 | Not routed | S9 | PLANNED |
| CF-12 | Minimal public routes; no debug/stack traces | §14.12 | P0 | `web/errors`, ingress | Error-page test | S1, S9 | PLANNED |
| CF-13 | Test tunnel, DNS, app health, Access independently; "deployed" only when all verified | §14 | P0 | doc 12 | — | S9 | BLOCKED (B-03) |

## CFG — Configuration and environments (§15)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| CFG-01 | Environments LOCAL_DEVELOPMENT, TEST, PAPER_TRADING, STAGING, LIVE_DISABLED/LIVE_PENDING_APPROVAL, LIVE | §15 | P0 | `core/config` | Enum test | S1 | PLANNED |
| CFG-02 | Validated config, safe defaults, early failure with non-secret messages | §15 | P0 | `core/config` | Invalid-config tests | S1 | PLANNED |
| CFG-03 | `.env.example`, configuration docs, startup validation | §15 | P0 | repo root, doc 15 | File present | S1 | PLANNED |
| CFG-04 | No keys in browser storage or frontend builds | §15 | P0 | portal | AT-16 HTML scan | S8 | PLANNED |
| CFG-05 | Production cannot use dev secrets, test broker or seeded demo accounts by accident | §15 | P0 | `core/config` | Validation tests | S1 | PLANNED |
| CFG-06 | Feature flags for integrations; disabled shows NOT_CONFIGURED | §15 | P0 | `core/config`, portal | Status page test | S1, S8 | PLANNED |

## TST — Testing and quality gates (§16, V6 §1830, §1831)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| TST-01 | Unit tests: domain, normalisation, indicators, money, permissions, limits, expiry, idempotency, transitions, source trust, routing | §16 | P0 | `tests/unit` | `make check` | all | PLANNED |
| TST-02 | Integration: migrations, transactions, handlers, worker lifecycle, adapters with fakes, connectors, Telegram mocks, broker contracts, audit persistence | §16 | P0 | `tests/integration` | `make check` | all | PLANNED |
| TST-03 | E2E: login, navigation, tasks, ingestion, evidence, AI exhaustion, proposal, approve/reject/expire, order simulation, reconciliation failure, kill switch, recovery | §16 | P0 | `tests/e2e` | `make check` | S8 | PLANNED |
| TST-04 | Security: authz matrix, IDOR, CSRF/XSS/injection, secret exposure, replay, prompt injection, SSRF, rate limits, isolation, revocation | §16 | P0 | `tests/security` | `make check` | S1–S10 | PLANNED |
| TST-05 | Trading simulations: accept/reject, partial, slippage, out-of-band price, timeout unknown, disconnect, duplicate webhook, cancel race, stale balance, limit breach, kill switch, restart | §16 | P0 | `tests/trading` | `make check` | S4 | PLANNED |
| TST-06 | Reliability: DB down, AI down, quota exhausted, stale source, worker crash, disk nearly full, tunnel down, VM restart, restore, migration rollback | §16 | P0 | `tests/reliability`, S9 scripts | Results recorded; VM restart BLOCKED | S9–S10 | PLANNED |
| TST-07 | Release: formatter, lint, types, migrations, tests, scans, container build, smoke | §16 | P0 | `Makefile` | Output captured | S10 | PLANNED |
| TST-08 | No credentials or real money for tests; deterministic fixtures and simulated brokers | §16 | P0 | tests | No network in unit tests (socket guard) | S1 | PLANNED |
| TST-09 | Property-based tests where practical | V6 §1830 Gate B | P1 | hypothesis | Tests | S3 | PLANNED |
| TST-10 | Go-live gates A–I evidence | V6 §1830 | P0 | readiness report | Gates C (broker sandbox), G (shadow period), I (activation) need external inputs | S10 | PLANNED |

### Required automated acceptance tests (V6 §1831)

| ID | Test | Pri | Test location (planned) | Slice | State |
|---|---|---|---|---|---|
| AT-01 | In RESEARCH_ONLY all live-order attempts are rejected | P0 | `tests/trading/test_acceptance.py::test_at01_research_only_rejects_orders` | S4 | PLANNED |
| AT-02 | In PAPER_TRADING simulated orders cannot reach a live endpoint | P0 | `::test_at02_paper_cannot_reach_live` | S4 | PLANNED |
| AT-03 | In APPROVAL_REQUIRED nothing is submitted before authenticated approval | P0 | `::test_at03_no_submission_without_approval` | S5 | PLANNED |
| AT-04 | Approval applies to one exact packet and cannot be replayed | P0 | `::test_at04_approval_single_use` | S5 | PLANNED |
| AT-05 | Expired or materially changed packet cannot execute | P0 | `::test_at05_expired_or_changed_packet` | S5 | PLANNED |
| AT-06 | Approved trade executes only within approved quantity, price, instrument, account, order type | P0 | `::test_at06_execution_within_bounds` | S5 | PLANNED |
| AT-07 | Risk veto blocks an approved trade when hard limits fail | P0 | `::test_at07_risk_veto_after_approval` | S5 | PLANNED |
| AT-08 | Strategy authority disabled by default; LLM/research worker cannot enable it | P0 | `::test_at08_strategy_authority_locked` | S5 | PLANNED |
| AT-09 | Duplicate callback/retry/timeout creates no duplicate exposure | P0 | `::test_at09_no_duplicate_exposure` | S5 | PLANNED |
| AT-10 | Partial fills update positions and permitted exit quantities | P0 | `::test_at10_partial_fill_exit_quantities` | S4 | PLANNED |
| AT-11 | Unknown order state blocks new exposure and triggers reconciliation | P0 | `::test_at11_unknown_state_blocks` | S4 | PLANNED |
| AT-12 | Kill switch blocks new entries and preserves audit/order state | P0 | `::test_at12_kill_switch` | S4 | PLANNED |
| AT-13 | AI outage/quota exhaustion leaves hard risk and reconciliation working | P0 | `::test_at13_ai_outage_safety` | S6 | PLANNED |
| AT-14 | Missing/stale market data blocks actions needing current data | P0 | `::test_at14_stale_data_blocks` | S3 | PLANNED |
| AT-15 | Account mismatch or accounting uncertainty blocks new exposure | P0 | `::test_at15_account_mismatch_blocks` | S4 | PLANNED |
| AT-16 | Credentials absent from prompts, frontend assets, Telegram output, logs | P0 | `tests/security/test_secret_exposure.py` | S6–S8 | PLANNED |
| AT-17 | Broker-confirmed state distinguished from local request state | P0 | `::test_at17_broker_vs_local_state` | S4 | PLANNED |
| AT-18 | Owner can inspect exactly why an order was blocked or permitted | P0 | `::test_at18_decision_explainability` | S5 | PLANNED |
| AT-19 | Restore from backup preserves approvals, strategy versions, orders, fills, audit, reconciliation | P0 | `scripts/restore-drill.sh` + `tests/reliability` | S9 | PLANNED |
| AT-20 | VM restart recovers safely without resubmitting an ambiguous order | P0 | `::test_at20_restart_recovery` (process-level); container restart in S9; VM restart BLOCKED (B-02) | S4, S9 | PLANNED |

## OBS — Observability, backup and recovery (§17)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| OBS-01 | Structured logs, correlation IDs, redaction | §17 | P0 | `core/logging` | Tests | S1 | PLANNED |
| OBS-02 | Metrics: API, queue age, worker failures, freshness, DB, AI usage/errors/cost, broker, reconciliation, resources | §17 | P1 | `ops/metrics` | Metrics endpoint test | S8 | PLANNED |
| OBS-03 | Alerts actionable, rate-limited, owned, expiring | §17 | P1 | `incidents`, notify | Dedupe test | S4, S8 | PLANNED |
| OBS-04 | Liveness and readiness | §17 | P0 | `/healthz`, `/readyz` | Tests | S1 | PLANNED |
| OBS-05 | Source/model/provider health page | §17 | P1 | portal | E2E | S8 | PLANNED |
| OBS-06 | Database and queue monitoring | §17 | P1 | `ops` | Status page | S8 | PLANNED |
| OBS-07 | Order/position reconciliation alarms | §17 | P0 | `reconcile` | Mismatch → incident test | S4 | PLANNED |
| OBS-08 | Log rotation and disk-space alerts | §17 | P1 | compose logging; `ops/resources` | Disk threshold test | S9 | PLANNED |
| OBS-09 | Encrypted, database-aware backups plus config backup | §17 | P0 | `deploy/backup` | Encrypted artefact produced | S9 | PLANNED |
| OBS-10 | Documented restore drills to an isolated target | §17 | P0 | `scripts/restore-drill.sh` | Drill executed, results recorded | S9 | PLANNED |
| OBS-11 | Retention policy and backup inventory | §17 | P1 | doc 14 | Doc + script | S9 | PLANNED |
| OBS-12 | RPO/RTO | §17 | P1 | doc 14 | Doc | S9 | PLANNED |
| OBS-13 | Tested migration and rollback instructions | §17 | P1 | Alembic downgrade test | Test | S1, S9 | PLANNED |
| OBS-14 | Incident runbook and postmortem template | §17 | P1 | doc 15 | Doc | S10 | PLANNED |
| OBS-15 | No `.env`/tokens in unencrypted backups; preserve incident evidence | §17 | P0 | backup script | Archive content check | S9 | PLANNED |

## COST — Cost and resource efficiency (§18)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| COST-01 | AI inference budget exactly $0 | §18 | P0 | `ai/policy` | AI-07 tests | S6 | PLANNED |
| COST-02 | Report total operating cost separately (domain, hardware, power, internet, backups, data, broker fees, notifications) | §18 | P1 | doc 16 | Doc | Gate A | IN_PROGRESS |
| COST-03 | Cost controls: deterministic first, dedupe, cache with expiry, pause low-priority work, no swarms, smallest model, measure, optional subsystems off | §18 | P1 | `ai`, `worker` | Tests + measurements | S6, S9 | PLANNED |

## REPO — Repository quality (§20)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| REPO-01 | Cohesive modules, directional dependencies, no circular imports, no giant files, no hidden globals | §20 | P1 | import-contract test | Test | S1+ | PLANNED |
| REPO-02 | Types and schemas at trust boundaries | §20 | P0 | pydantic + mypy strict | `make check` | S1+ | PLANNED |
| REPO-03 | Migrations only; no manual schema edits | §20 | P0 | Alembic | Drift test | S1 | PLANNED |
| REPO-04 | Graceful shutdown, idempotent jobs, bounded retries | §20 | P1 | `worker` | Tests | S2+ | PLANNED |
| REPO-05 | Pinned dependencies; documented versions | §20 | P0 | `uv.lock` | Present | S1 | PLANNED |
| REPO-06 | Reproducible commands; single test entry point | §20 | P0 | `Makefile` (`make check`) | Present | S1 | PLANNED |
| REPO-07 | Generated artefacts and local state stay out of Git | §20 | P0 | `.gitignore` | Review | S1 | DONE |
| REPO-08 | API endpoints, permissions, contracts, errors documented | §20 | P1 | doc 10 | Doc | Gate A | IN_PROGRESS |
| REPO-09 | Useful errors without stack traces or secrets | §20 | P0 | `web/errors` | Test | S1 | PLANNED |
| REPO-10 | Real integrations only where credentials exist; otherwise testable adapters + setup instructions | §20 | P0 | adapters, doc 15 | Review | all | PLANNED |
| REPO-11 | No empty TODOs for high-priority requirements | §20 | P0 | grep in `make check` | Check | S10 | PLANNED |
| REPO-12 | No gratuitous rewrites | §20 | P1 | — | N/A (greenfield) | — | N/A |

## MIS — Mission-level capability map (V6 §1, North Star, §1835)

| ID | Requirement | Source | Pri | State |
|---|---|---|---|---|
| MIS-01 | Governed, evidence-driven, auditable financial intelligence institution: observe, find, verify, research, test, challenge, rank, coordinate, explain, remember | V6 §1 | P1 | PLANNED (first slice: crypto spot) |
| MIS-02 | Serve beginners without hiding risk; forensic access for advanced users | V6 §1 | P1 | PLANNED (plain/detail views) |
| MIS-03 | Global Intelligence OS, Search Fabric, World Brain, knowledge graph, broad asset coverage (baseline Sections 0–1803) | V6 §0 | P3 | BLOCKED (B-01) / DEFERRED |
| MIS-04 | Prime directive: when authorization, data, account state or risk is uncertain, block new exposure, verify, escalate | V6 §1835 | P0 | PLANNED (covered by TRD-48/49) |
