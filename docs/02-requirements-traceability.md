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
| GOV-05 | Search for TODO/FIXME/mock/stub/bypass/unsafe fallback | §2.5 | P0 | 01 audit; S10 rerun | Re-run 2026-10-11: no TODO/FIXME/stub markers; now enforced by `make lint` | S10 | DONE |
| GOV-06 | Find duplicates, dead files, hard-coded secrets, drift | §2.6 | P0 | 01 audit; secret scan in S10 | Secret scan in `make check`; duplicate/dead-file review at handover | S10 | DONE |
| GOV-07 | Run baseline tests/lint before changes; record pre-existing failures | §2.7 | P0 | 01 audit | No tests existed; recorded as such | — | DONE |
| GOV-08 | Preserve useful working components | §2.8 | P0 | 01 audit | Nothing to preserve except the prompt (kept verbatim) | — | DONE |
| GOV-09 | Traceable requirements inventory with ID, source, dependency, priority, state, verification, limitation | §2.9 | P0 | this document | All rows re-assessed against tests at handover (S10) | all | DONE |
| GOV-10 | Resolve contradictions through ADRs; stricter security/accounting/risk rule wins | §2.10 | P0 | `17-ADRs/` | ADR-002, ADR-010, ADR-014 | — | DONE |
| GOV-11 | Gate A design package (docs 00–16, ADRs, diagrams) before broad coding | §3 | P0 | `docs/` | Files exist and are cross-referenced | Gate A | DONE |
| GOV-12 | Gate B: build verified vertical slices; living `IMPLEMENTATION-STATUS.md` | §3 | P0 | status log | IMPLEMENTATION-STATUS: Gate A + S1–S10 with real test output | all | DONE |
| GOV-13 | Record significant decisions as ADRs | §1 | P1 | `17-ADRs/` | ADR-001…022 | all | DONE |
| GOV-14 | Ask only for genuinely external inputs; write precise BLOCKER records and continue | §1, §21 | P0 | `BLOCKERS.md` | B-01…B-09, six fields each | all | DONE |
| GOV-15 | 11-part factual report after each substantial session | §22 | P0 | `reports/` | `docs/reports/2026-10-11-session-report.md` | S10 | DONE |
| GOV-16 | Per-phase report fields (IMPLEMENTED, NOT_IMPLEMENTED, TESTS_RUN, ...) | V6 §1833 | P1 | status log | Slice entries: implemented / verified / not verified / bugs / limitations; the session report uses the full field list | all | PARTIAL |
| GOV-17 | Precedence: law/licences/security > e-stop & hard risk > human authorization > strategy mandates > operational policy > research/AI | V6 §0 | P0 | ADR-010, `trading/authority` | `test_config_cap_forces_protection`, `test_emergency_stop_not_via_mode_change`, `test_at07_risk_veto_after_approval`, `test_at08_strategy_authority_locked` | S3–S5 | IMPLEMENTED |
| GOV-18 | Include the V4/V5 baseline (Sections 0–1803) with V6 | V6 §0 | P1 | ADR-002 | Baseline missing | — | BLOCKED (B-01) |
| GOV-19 | Never claim "100% perfect"; "done" needs acceptance evidence | §1, §4.13 | P0 | all reports | Reports state BLOCKED/NOT VERIFIED items explicitly | all | DONE |
| GOV-20 | Each phase states objectives, dependencies, budget, deliverables, tests, failure tests, security review, rollback, acceptance | V6 §1832 | P1 | `03-product-scope-and-roadmap.md` | Roadmap table per slice | Gate A | DONE |

## PRIN — Product principles and hard limits (§4)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| PRIN-01 | Market claims show sources, timestamps, freshness, method, uncertainty | §4.1 | P0 | `evidence`, `sources`, portal | Provenance on every quote/bar/claim; packet `market_at_proposal`; `test_market_pages_show_labelled_data` | S2, S7 | IMPLEMENTED |
| PRIN-02 | Balances, positions, orders, arithmetic, permissions, limits are deterministic code | §4.2 | P0 | `trading/*`, `core/money` | `test_money.py`, `test_trading_primitives.py`, `test_schema_rules.py`; `test_forbidden_imports` | S3–S4 | IMPLEMENTED |
| PRIN-03 | Research, signals and agent consensus cannot grant authority | §4.3 | P0 | `trading/approvals` | `test_at08_strategy_authority_locked`, `test_research_worker_cannot_approve_submit_or_change_authority` | S5, S7 | IMPLEMENTED |
| PRIN-04 | Start in research/paper; live disabled until gates pass and operator enables | §4.4 | P0 | `trading/authority` | `test_default_is_research_only`, `test_live_modes_unreachable`, `test_golive_readiness_never_passes_without_broker` | S3 | IMPLEMENTED |
| PRIN-05 | Free cloud AI only; no paid calls, no Ollama, no local LLM | §4.5 | P0 | `ai` | `test_no_local_llm_or_paid_sdk_dependencies`, `test_paid_or_local_ai_policy_refused`, AI gateway tests | S6 | IMPLEMENTED |
| PRIN-06 | Label LIVE, DELAYED, SIMULATED, STALE, UNKNOWN, UNAVAILABLE | §4.6 | P0 | `sources/freshness`, portal | `test_labels`, `test_market_pages_report_stale_data` | S2, S8 | IMPLEMENTED |
| PRIN-07 | Least privilege, authentication, secrets, auditability, threat model from the start | §4.7 | P0 | `identity`, `audit`, doc 09 | Security suite (authz matrix, web auth, audit, secret exposure), doc 09 | S1 | IMPLEMENTED |
| PRIN-08 | No big-bang; small hardened slices | §4.8 | P1 | roadmap | S1–S10 slices, each committed after `make check` | all | DONE |
| PRIN-09 | Modular monolith + workers | §4.9 | P1 | ADR-003 | Architecture review | Gate A | DONE |
| PRIN-10 | No unnecessary technology; ADR for major choices | §4.10 | P1 | ADRs | ADR-004/005/006 | Gate A | DONE |
| PRIN-11 | No destructive guessing (data, DB, DNS, nameservers, admin exposure, orders) | §4.11 | P0 | deploy scripts | Deploy scripts dry-run by default; restore only into a new DB; DNS script GET-only | S9 | IMPLEMENTED |
| PRIN-12 | No secret leakage into code, logs, commits, docs, prompts | §4.12 | P0 | `core/logging`, `ai/safety` | `test_secret_exposure.py`, `test_redaction.py`, outbound scanner, secret scan | S1, S6, S10 | IMPLEMENTED |
| PRIN-13 | Honest completion; BLOCKED / NOT_TESTED / UNKNOWN with reasons | §4.13 | P0 | status log | BLOCKERS + status log | all | DONE |
| PRIN-14 | Cost-effective: caching, deterministic work, batching, rate-aware queues | §4.14 | P1 | `ai`, `worker` | AI cache with expiry, per-event dedupe, deterministic analytics first, batched upserts | S6 | IMPLEMENTED |
| PRIN-15 | Human retains capital authority | §4.15 | P0 | `trading/approvals` | `test_at03_no_submission_without_approval`, `test_at04_*` | S5 | IMPLEMENTED |

## AI — Free cloud AI gateway (§5, V6 §1819)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| AI-01 | Provider-neutral gateway; adapters, no provider calls scattered in code | §5 | P0 | `ai/gateway`, `ai/adapters` | `test_only_adapters_import_http_clients` | S6 | IMPLEMENTED |
| AI-02 | Verify $0 pricing, quota, eligibility, data handling, context, terms from official docs; record review date | §5 | P0 | `config/ai_registry.toml`, doc 06 | Registry carries evidence URLs and review dates; live verification needs keys (B-04) | S6 | PARTIAL |
| AI-03 | Explicit allowlist of providers and model IDs | §5 | P0 | registry + `ai_model_approvals` | `test_admin_actions_need_mfa_and_free_model_ids` | S6 | IMPLEMENTED |
| AI-04 | Reject models whose free status is not verified | §5 | P0 | `ai/policy` | `test_attestation_and_approval_required_then_ok_and_cached` | S6 | IMPLEMENTED |
| AI-05 | No paid endpoints or paid fallback routing | §5 | P0 | `ai/policy`, adapter | `test_paid_model_row_injected_directly_is_still_rejected`, `test_openrouter_requires_fresh_zero_price_and_no_fallbacks` | S6 | IMPLEMENTED |
| AI-06 | Never upgrade plans, add credits, attach cards, create chargeable resources | §5 | P0 | design | No billing API in code (checked 2026-10-11) | S6 | IMPLEMENTED |
| AI-07 | Inference budget exactly $0; fail closed when eligibility uncertain | §5, §18 | P0 | `ai/policy` | `test_billing_signal_disables_provider` | S6 | IMPLEMENTED |
| AI-08 | Per-provider circuit breaker | §5 | P1 | `ai/breaker` | `test_breaker_opens_after_failures` | S6 | IMPLEMENTED |
| AI-09 | Quota tracker and token/request usage ledger | §5 | P1 | `ai_usage`, `ai/quota` | `test_local_quota_budget`; usage ledger | S6 | IMPLEMENTED |
| AI-10 | Timeouts, bounded retries with jitter, `Retry-After` support | §5 | P1 | `ai/adapters` | `test_rate_limit_falls_back_to_other_free_route` (429 + Retry-After) | S6 | IMPLEMENTED |
| AI-11 | No retry storms; respect terms; never multiply accounts to evade limits | §5 | P0 | `ai/quota`, doc 06 | Bounded attempts; one account per provider (policy) | S6 | IMPLEMENTED |
| AI-12 | Task routing: small models for classification, stronger for research, code for math, reviewer only when justified | §5 | P1 | `ai/routing` | SMALL for classify/extract, LARGE→SMALL for research; exact numbers by deterministic code only; red-team only for entry signals; `test_task_routing_uses_the_tier_for_the_task` | S6 | IMPLEMENTED |
| AI-13 | Cache with explicit expiry; never present expired output as current | §5 | P1 | `ai_cache` | Cache expiry test in `test_attestation_and_approval_required_then_ok_and_cached` | S6 | IMPLEMENTED |
| AI-14 | Batch, deduplicate, compress context; pass evidence references | §5 | P2 | `ai/gateway`, `workforce` | Curated fields with item IDs as evidence references; fair, marked context budget (`test_ai_context.py`); request-hash cache; one mission per event | S6–S7 | IMPLEMENTED |
| AI-15 | AI optional for safety; queue AI work and report degraded status when all providers fail | §5 | P0 | `ai`, `workforce` | `test_at13_ai_outage_does_not_affect_safety` | S6 | IMPLEMENTED |
| AI-16 | Keys server-side only; never in browser, Telegram, logs, prompts | §5 | P0 | `core/config`, `ai/safety` | `test_keys_only_in_auth_header_never_in_usage_or_body`, AT-16 | S6 | IMPLEMENTED |
| AI-17 | Do not send credentials, sensitive personal data or account details to providers | §5 | P0 | `ai/safety` (classification + outbound scanner) | `test_outbound_scanner_blocks` | S6 | IMPLEMENTED |
| AI-18 | Tests prove paid model IDs/routes are rejected; fake provider; no live calls in unit tests | §5 | P0 | `tests/ai` | Fake provider only; socket guard | S6 | IMPLEMENTED |
| AI-19 | Provider health page: model, review date, last success, rate-limit state, degraded, last error — never the key | §5 | P1 | portal `/ai` | `/ai` page; secret exposure test covers it | S6, S8 | IMPLEMENTED |
| AI-20 | No "unlimited" promises; prioritise by decision value under finite quota | §5 | P1 | `ai/quota`, docs | `workforce/priority.py`: waiting narratives ordered by decision value then deadline (`test_decision_value_ordering`, `test_runnable_tasks_follow_priority_then_deadline`) | S6 | IMPLEMENTED |
| AI-21 | Obey `FREE_CLOUD_AI_ONLY`; disclose quota/privacy volatility | V6 §1819 | P0 | config, portal | `test_paid_or_local_ai_policy_refused` | S6 | IMPLEMENTED |
| AI-22 | On exhaustion: pause AI-researched proposals; use cached verified research; deterministic controls continue | V6 §1819 | P0 | `workforce`, `ai` | `test_no_ai_means_no_ai_researched_proposal` | S7 | IMPLEMENTED |
| AI-23 | AI never required for hard risk, approval validation, order persistence, idempotency, reconciliation, secrets, e-stop, audit | V6 §1819 | P0 | import contracts | `test_forbidden_imports` | S3–S6 | IMPLEMENTED |
| AI-24 | Show execution/data costs separately from free AI | V6 §1819 | P1 | packet, doc 16 | Packet cost fields + note (`test_approval_packet_contains_every_material_term`) | S5 | IMPLEMENTED |

## DATA — Sources, provenance and evidence (§6, V6 §1829)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| DATA-01 | Treat global-coverage sections as a capability map, not first-release scope | §6 | P1 | doc 03 | Scope doc | Gate A | DONE |
| DATA-02 | Source connectors behind a common interface | §6 | P0 | `sources/contract` | `SourceConnector` protocol; `test_connectors.py` | S2 | IMPLEMENTED |
| DATA-03 | Per-source metadata (publisher, jurisdiction, language, access, licence, frequency, timestamps, units, quality, missingness, bias, last success, freshness limit, fallback, owner) | §6 | P0 | `sources` table | `test_registry_idempotent_with_full_provenance` | S2 | IMPLEMENTED |
| DATA-04 | Source registry and provenance first | §6.1 | P0 | `sources` | Every quote/bar has `source_id` + `fetch_run_id` (NOT NULL) | S2 | IMPLEMENTED |
| DATA-05 | Small set of lawful, accessible sources for first markets | §6.2 | P0 | Coinbase Exchange public, Bitstamp public, Coinbase status (ADR-015); Binance public market data and system status (ADR-023) | Captured fixtures for every listing; `test_binance_*`; live smoke `test_live_binance_and_gold_listings` | S2, S12 | IMPLEMENTED |
| DATA-06 | Validation, normalisation, dedup, timezone/unit handling | §6.3 | P0 | `sources/normalize` | `test_bar_validation`, `test_quote_validation`, `test_bars_idempotent_open_bar_and_revision` | S2 | IMPLEMENTED |
| DATA-07 | Historical snapshots and source-vintage awareness | §6.4 | P1 | `fetch_runs`, `bars.fetch_run_id` | Backtests store data vintage; `test_version_hash_changes_with_params` | S2, S7 | IMPLEMENTED |
| DATA-08 | Material-change detection; evidence and claim registry | §6.5 | P1 | `market/events`, `evidence` | `research.events` detector; claims VERIFIED/INFERRED/CONTRADICTED (`test_complete_mission_from_signal_to_execution`) | S7 | IMPLEMENTED |
| DATA-09 | Canonical entity/instrument identity (ticker is not an ID) | §6.6 | P0 | `market/instruments` | Canonical keys `SPOT:BTC/USD@COINBASE`; `test_instrument_rules_and_change_event` | S2 | IMPLEMENTED |
| DATA-10 | Search, claim verification, contradictions, change-first reports | §6.7 | P2 | `evidence`, `research/report` | Claims with VERIFIED/INFERRED/CONTRADICTED, contradictions, change-first reports; full-text search over claims, reports and proposals (migration 0007 GIN indexes; `test_search.py` incl. index use and hostile input) | S7 | IMPLEMENTED |
| DATA-11 | More asset classes only after licence/quality/value evaluation | §6.8 | P3 | doc 07 | Capability map | — | DEFERRED |
| DATA-12 | Respect ToS, rate limits, robots; no prohibited scraping; no false real-time claims | §6 | P0 | connectors | Fixed polling intervals; 429/Retry-After handled (`test_source_outage_recorded_and_incident`); data labelled DELAYED | S2 | IMPLEMENTED |
| DATA-13 | Unavailable/paid source → adapter contract + blocker + labelled alternative | §6 | P1 | `BLOCKERS.md` | B-09 recorded; Coinbase/Bitstamp used instead | S2 | IMPLEMENTED |
| DATA-14 | Reports answer: changed, matters, verified, inferred, contradicts, unknown, timestamps, freshness, why | §6 | P1 | `research/report` | Mission report sections; `test_complete_mission_from_signal_to_execution` | S7 | IMPLEMENTED |
| DATA-15 | Per provider/instrument: licence, entitlement, frequency, latency, timezone, currency, revisions, fields | V6 §1829 | P1 | `sources`, `instruments` | Registry rows carry licence/terms/latency fields | S2 | IMPLEMENTED |
| DATA-16 | Distinguish broker values, live licensed, delayed public, historical, estimates, simulated | V6 §1829 | P0 | `sources/freshness`, `core/labels` | `test_labels`; broker vs simulated tagging in packets and orders | S2 | IMPLEMENTED |
| DATA-17 | Never show simulated as actual; timestamp + freshness next to market values; block dependent actions | V6 §1829 | P0 | portal, risk | `test_at14_stale_data_blocks`, `test_market_pages_report_stale_data` | S2, S8 | IMPLEMENTED |
| DATA-18 | Expose coverage gaps rather than imply global coverage | V6 §1829 | P1 | portal `/sources` | `/sources` lists every source incl. NOT_CONFIGURED integrations | S8 | IMPLEMENTED |

## WF — AI workforce and task orchestration (§7)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| WF-01 | Digital employees are governed roles and workflows | §7 | P1 | `workforce/roles` | `test_workforce_roles.py` | S7 | IMPLEMENTED |
| WF-02 | Each role declares mission, scope, inputs, output schema, tools, data class, permissions, SLA, retry, budget, evidence, gates, reviewer, metrics, failure modes, retirement | §7 | P1 | `workforce/roles` | `test_role_declares_everything` | S7 | IMPLEMENTED |
| WF-03 | Start with 7 roles: orchestrator, source/data analyst, market researcher, quant analyst, risk reviewer, task reviewer, ops/security monitor | §7 | P1 | `workforce/roles` | `test_exactly_the_seven_initial_roles` | S7 | IMPLEMENTED |
| WF-04 | Task engine: owner, status, priority, deadline, dependencies, blocker, evidence, artefact, acceptance criteria, attempts, reviewer, idempotency, escalation, expiry, reopening | §7 | P1 | `workforce/tasks` | Task engine exercised by mission tests | S7 | IMPLEMENTED |
| WF-05 | Completion requires verification, not an agent claim | §7 | P0 | `workforce/review` | Review before DONE (mission tests) | S7 | IMPLEMENTED |
| WF-06 | Prevent loops, duplicates, cross-agent injection, uncontrolled tools, unbounded delegation | §7 | P0 | `workforce/tasks` | Depth/attempt/budget limits; idempotent per-event missions; `test_toolbox_enforces_allowlist` | S7 | IMPLEMENTED |
| WF-07 | Web pages, PDFs, tool output and commentary are untrusted data | §7 | P0 | `ai/safety` | `test_invalid_output_and_injection_contained` | S6–S7 | IMPLEMENTED |
| WF-08 | One complete research mission: detection → assignment → evidence → review → report → archive | §19 Ph5 | P1 | `workforce/missions` | `test_complete_mission_from_signal_to_execution` | S7 | IMPLEMENTED |

## QNT — Market research and quantitative system (§8, V6 §1826)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| QNT-01 | Instrument master: venue, symbol, currency, tick/lot, multiplier, sessions, timezone, expiry, settlement | §8 | P0 | `market/instruments` | `test_coinbase_product_rules`, `test_instrument_rules_and_change_event` | S2 | IMPLEMENTED |
| QNT-02 | Bars/quotes with source timestamp, ingestion timestamp, delay, quality state | §8 | P0 | `bars`, `quotes` | `test_quotes_idempotent_and_labelled` | S2 | IMPLEMENTED |
| QNT-03 | Deterministic indicators with reference tests | §8 | P1 | `market/indicators` | `test_indicators.py` | S2 | IMPLEMENTED |
| QNT-04 | Multi-timeframe market state and regime classification with explicit method | §8 | P1 | `market/regime` | `test_regime_method`; doc 07 | S7 | IMPLEMENTED |
| QNT-05 | Fundamentals and macro calendar where sources exist | §8 | P2 | — | Not applicable to the first crypto slice | — | DEFERRED |
| QNT-06 | Volatility, correlation, exposure, drawdown | §8 | P1 | `market/stats`, `trading/risk` | `test_volatility_drawdown_correlation` | S2–S3 | IMPLEMENTED |
| QNT-07 | Position-sizing helpers with caps and uncertainty haircuts | §8 | P0 | `trading/sizing` | `test_sizing_respects_every_cap` (property) | S3 | IMPLEMENTED |
| QNT-08 | Strategy/experiment registry, versioning, reproducible backtests, walk-forward/OOS | §8 | P1 | `research`, `research/library` (100 rules, ADR-024) | `test_walk_forward_discloses_trials_and_insufficient_data`, version hashes (library versions include a code hash); `test_library_run_stores_every_rule`; development/validation split per series | S7, S12 | IMPLEMENTED |
| QNT-09 | Realistic fee, spread, slippage, funding, borrow, liquidity assumptions | §8 | P1 | `research/backtest`, paper broker | `test_costs_reduce_returns_and_are_disclosed`; fee 60 bps labelled ASSUMPTION | S4, S7 | IMPLEMENTED |
| QNT-10 | Look-ahead, survivorship, corporate-action, multiple-testing safeguards | §8 | P1 | `research/backtest`, `research/library` | `test_backtest_no_lookahead`, `test_no_lookahead_property`; every library rule recomputed on truncated histories (`test_library_causality.py`, with a peeking rule shown to fail); deflated Sharpe ratio over all trials (`test_library_stats.py`, `test_noise_does_not_qualify`, `test_genuine_skill_would_qualify`) | S7, S12 | IMPLEMENTED |
| QNT-11 | Performance decomposition: thesis, timing, sizing, execution, fees, luck | §8 | P2 | `trading/review` | `trading/attribution.py`: market move (thesis+luck, stated as inseparable per trade), delay, entry/exit execution, fees — parts sum exactly (`test_parts_always_add_up_to_the_realised_result`); sizing vs plan; shown on the trade page | S7 | IMPLEMENTED |
| QNT-12 | Opportunity lifecycle IDEA → … → REVIEW | §8 | P1 | `research/opportunities` | `test_opportunity_paths` | S7 | IMPLEMENTED |
| QNT-13 | No false profitability claims; disclose sample limits, drawdown, costs, uncertainty | §8 | P0 | backtest report | `test_backtest_result_carries_the_past_performance_disclaimer` | S7 | IMPLEMENTED |
| QNT-14 | Post-trade review: planned vs actual (thesis, timing, costs, risk, execution, authorization, decision vs outcome) | V6 §1826 | P1 | `trading/review` | `test_stop_loss_fills_and_creates_loss_review` | S7 | IMPLEMENTED |
| QNT-15 | Never rewrite original forecasts; learning cannot silently change a live mandate | V6 §1826 | P0 | `trading/review` | Packets and reviews are append-only tables | S7 | IMPLEMENTED |

## TRD — Human-approved trading engine (§9, V6 §1804–§1818, §1821–§1822, §1827, §1830, §1834)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| TRD-01 | Seven authority modes, persisted and audited | §9, V6 §1804 | P0 | `trading/authority` | `test_authority.py`, `test_mode_matrix` | S3 | IMPLEMENTED |
| TRD-02 | RESEARCH_ONLY and PAPER_TRADING have no live submission authority | V6 §1804 | P0 | `trading/execution` | AT-01, AT-02 | S4 | IMPLEMENTED |
| TRD-03 | APPROVAL_REQUIRED default for live; STRATEGY_AUTHORIZED disabled by default | V6 §1804 | P0 | `trading/authority` | Live modes unreachable (no live adapter); `test_at08_strategy_authority_locked` | S3 | IMPLEMENTED |
| TRD-04 | Configuration change cannot silently make the mode more permissive | V6 §1804 | P0 | `trading/authority`, `core/config` | `test_config_cap_forces_protection` | S3 | IMPLEMENTED |
| TRD-05 | Separate capabilities: READ_MARKET_DATA, READ_ACCOUNT, RESEARCH, PROPOSE_TRADE, APPROVE_TRADE, SUBMIT_ORDER, CANCEL_ORDER, MANAGE_APPROVED_POSITION, CHANGE_RISK_LIMITS, ENABLE_STRATEGY_AUTHORITY, RECONCILE, ADMINISTER_SYSTEM | V6 §1804 | P0 | `identity/capabilities` | `test_capabilities.py` | S1 | IMPLEMENTED |
| TRD-06 | LLM/research workers get no broker secrets or order submission; execution accepts only typed, validated, authorised intents | V6 §1804, §1820 | P0 | `workforce`, `trading/execution` | `test_forbidden_imports`, `test_research_worker_cannot_approve_submit_or_change_authority` | S4, S7 | IMPLEMENTED |
| TRD-07 | Candidate trade carries identity, venue, currency, account, session, direction, exposure, horizon, thesis, evidence + timestamps, contradictions, red team, entry, order types, quantity, price constraints, costs, portfolio impact, risk, liquidity, events, exit, invalidation, expiry, re-approval conditions, operational checks, reasons for WAIT/NO_TRADE | V6 §1805 | P0 | `trading/proposals` | `test_approval_packet_contains_every_material_term` | S5 | IMPLEMENTED |
| TRD-08 | Weak evidence, unverifiable price, ambiguous identity, unsupported venue, no risk calc → NON_EXECUTABLE, no approval request | V6 §1805 | P0 | `trading/proposals` | `test_each_check_blocks`; Candidate validation | S5 | IMPLEMENTED |
| TRD-09 | Pre-approval deterministic validation of the 12 listed areas | V6 §1806 | P0 | `trading/risk` | `test_each_check_blocks`, `test_baseline_passes_all_checks` | S3 | IMPLEMENTED |
| TRD-10 | Any failed mandatory check blocks or marks NON_EXECUTABLE; plain-language and technical reasons | V6 §1806 | P0 | `trading/risk`, portal | `test_at18_owner_can_see_why` | S3, S5 | IMPLEMENTED |
| TRD-11 | AI cannot override risk; limit changes only via separate authenticated MFA workflow with reason | V6 §1806 | P0 | `trading/risk/policy` | `test_default_policy_and_change_workflow`, `test_risk_policy_change_via_portal` | S3 | IMPLEMENTED |
| TRD-12 | Approval packet contents (TRADE_ID, account alias, mode, instrument, venue, direction, horizon, quantity, order types, limit/band, slippage, notional, fees, holdings, impact, max risk + assumptions, loss warning, thesis, evidence, contradictions, scenarios, red team, entry, exit, invalidation, management permissions, prohibited actions, check states, expiries, consequences) | §9, V6 §1807 | P0 | `trading/proposals/packet` | `test_approval_packet_contains_every_material_term` | S5 | IMPLEMENTED |
| TRD-13 | Plain language first; detail on demand; no single opaque score | V6 §1807 | P1 | portal, Telegram | Packet `plain_language` first; portal shows details on demand | S5 | IMPLEMENTED |
| TRD-14 | Approval impossible if packet fields missing or stale; no generic approval text | V6 §1807 | P0 | `trading/approvals` | AT-04, AT-05 | S5 | IMPLEMENTED |
| TRD-15 | Approval bound to TRADE_ID, packet hash, account, venue, instrument, side, quantity, exposure effect, order types, price band, slippage, max notional, policy version, validation result, management actions, times, operator, nonce | V6 §1808 | P0 | `approvals` table | AT-04, AT-05 | S5 | IMPLEMENTED |
| TRD-16 | Silence, expiry, timeout, ambiguity, "looks good" never approve; rejected/revoked/expired/consumed/superseded unusable | V6 §1808 | P0 | `trading/approvals` | AT-05; `test_confirmation_expires`; free text ignored | S5 | IMPLEMENTED |
| TRD-17 | Server-side single-use approval record; client status and Telegram callback data are references, not credentials | V6 §1808 | P0 | `trading/approvals`, `notify/telegram` | `test_other_user_cannot_use_callbacks`; server-side challenge | S5 | IMPLEMENTED |
| TRD-18 | Material change after approval → stop and require new approval; no silent resize/reverse/substitute/reprice | V6 §1808 | P0 | `trading/execution` | `test_at05_materially_changed_conditions` | S4–S5 | IMPLEMENTED |
| TRD-19 | Execution independently re-checks authorisation and risk immediately before submission | V6 §1808 | P0 | `trading/execution` | `test_at07_risk_veto_after_approval` | S4 | IMPLEMENTED |
| TRD-20 | 12-step execution procedure (load, reconcile, revalidate, unique intent/client ID, build from approved params only, submit, persist before/after, monitor, reconcile fills, approved exits only, notify actual state, audit + review) | V6 §1809 | P0 | `trading/execution` | `test_happy_path_paper_trade_end_to_end`; local run | S4 | IMPLEMENTED |
| TRD-21 | Never claim placed/accepted/filled/cancelled/protected without broker confirmation | V6 §1809 | P0 | `trading/orders`, notify | `test_at17_broker_confirmed_vs_local` | S4 | IMPLEMENTED |
| TRD-22 | Only explicitly approved management actions; prohibited actions list enforced | V6 §1810 | P0 | `trading/management`, `trading/execution` (approved exits) | `test_management.py`, AT-10; an approved exit names the entries it closes (bound into the packet hash), cancels their protective orders before sending a reduce-only sell, is never sent if a stop filled first, and restores protection if it ends unfilled (`test_approved_exit.py`) | S4, S12 | IMPLEMENTED |
| TRD-23 | Strategy-level authorisation: separate, versioned, bounded mandate; disabled by default | V6 §1811 | P2 | `trading/authority` | Mode refuses activation (AT-08); mandate execution not built (ADR-010) | S3 | DEFERRED |
| TRD-24 | Order state machine with broker-grounded transitions and full event history | V6 §1812, §9 | P0 | `trading/orders` | `test_order_state_machine.py` | S4 | IMPLEMENTED |
| TRD-25 | Uncertain state: mark, block, query by client ID, reconcile, resolve from evidence, escalate; never resubmit blindly | V6 §1812 | P0 | `trading/execution`, `reconcile` | AT-11, AT-20 (process + container) | S4 | IMPLEMENTED |
| TRD-26 | Duplicate/race defence: immutable IDs, idempotency keys, DB constraints, single serialized execution authority; eight listed races | V6 §1813 | P0 | `trading/execution` | AT-09; `test_concurrent_claims_never_duplicate`; local chaos run | S4 | IMPLEMENTED |
| TRD-27 | Broker adapter interface documenting operations, auth, freshness, order types, idempotency, rate limits, venue status, partial fills, fees, precision, reconciliation | V6 §1814 | P0 | `trading/brokers/base` | `test_broker_contract.py` | S4 | IMPLEMENTED |
| TRD-28 | No claimed integration without implementation and tests against official docs/sandbox | V6 §1814 | P0 | docs, `trading/brokers`, `trading/sandbox` | Paper adapter; Binance Spot Test Network adapter built from the official docs and tested against a doc-derived emulator (`test_binance_adapter.py`, `test_sandbox.py`). A test-network account needs a PASSED contract run against the real test network for the worker's key (ADR-026); the run PASSED on 2026-10-15 (8/8 steps) | S4, S12, S13 | IMPLEMENTED |
| TRD-29 | Credential rules: dedicated account, minimum scopes, no withdrawals, secret storage, rotation/revocation, network allowlist | V6 §1814 | P0 | doc 08, `core/config`, `deploy` | Design + config validation; test-network keys are worker-only Docker secrets, identified elsewhere only by a fingerprint (`test_secrets_are_least_privilege`); live keys BLOCKED | S9, S13 | BLOCKED (B-07) for live |
| TRD-30 | Portfolio-level veto evaluated on the post-trade portfolio immediately before submission | V6 §1815 | P0 | `trading/risk/portfolio` | `test_at07_risk_veto_after_approval` | S3–S4 | IMPLEMENTED |
| TRD-31 | Each limit defines source, currency, timezone/session, reset, fee treatment, unrealised treatment, unavailable-data action; unknown risk ≠ zero | V6 §1815 | P0 | `trading/risk/policy` | `test_default_policy_valid_and_semantics_documented`; `/settings/risk` | S3 | IMPLEMENTED |
| TRD-32 | Limit changes audited and never retroactive | V6 §1815 | P0 | `trading/risk/policy` | Approvals bind the policy version; `test_policy_versions_immutable` | S3, S5 | IMPLEMENTED |
| TRD-33 | Honest monitoring cadence; actual vs estimated; protective-order status; material alerts with prescribed content | V6 §1816 | P1 | `trading/monitor`, notify | `Alert` with six required answers; `test_every_alert_answers_the_six_questions`; portal Alerts inbox (`test_alert_inbox`) | S4 | IMPLEMENTED |
| TRD-34 | Kill switch: 8-step procedure, portal and Telegram, LLM-independent | V6 §1817 | P0 | `trading/killswitch` | AT-12, `test_emergency_stop_from_telegram` (Gate F needs a broker) | S4 | IMPLEMENTED |
| TRD-35 | Kill switch does not blindly cancel protective orders or liquidate; owner-defined emergency behaviour | V6 §1817 | P0 | `trading/killswitch` | `test_at12_kill_switch` | S4 | IMPLEMENTED |
| TRD-36 | Reconcile broker orders → fills → positions → cash → ledger → account state; broker authoritative; no guessed repair; stay protected until verified; explicit recovery event | V6 §1818 | P0 | `trading/reconcile` | AT-15; `test_resume_refused_until_a_reconciliation_after_the_stop_passes` | S4 | IMPLEMENTED |
| TRD-37 | Validate min/max quantity, lot, tick, precision, order type, TIF, venue rules with decimals | V6 §1821 | P0 | `trading/precision` | `test_normalise_*` (property) | S3 | IMPLEMENTED |
| TRD-38 | Pre-submission: session, identity, price band, spread/liquidity, rounding, TIF, halts/incidents | V6 §1821 | P0 | `trading/risk` | Risk checks R01–R17 (`test_each_check_blocks`) | S3–S4 | IMPLEMENTED |
| TRD-39 | Never round into materially different exposure; delayed data only if policy + operator accept | V6 §1821 | P0 | `trading/precision`, policy | `test_normalise_never_increases_exposure`, `test_delayed_data_not_acceptable_for_live_accounts` | S3 | IMPLEMENTED |
| TRD-40 | Partial fills are real exposure; recompute from broker values | V6 §1822 | P0 | `trading/orders`, ledger | AT-10 | S4 | IMPLEMENTED |
| TRD-41 | Cancel request ≠ cancellation; replace ≠ replaced; cancel/fill race → reconcile | V6 §1822 | P0 | `trading/orders` | `test_cancel_fill_race_is_resolved_from_the_venue` | S4 | IMPLEMENTED |
| TRD-42 | Protective order failure: mark degraded, block risk, alert, bounded deterministic retry, never widen | V6 §1822 | P0 | `trading/management` | `test_protective_rejection_degrades_and_retries_boundedly` | S4 | IMPLEMENTED |
| TRD-43 | Support venues without bracket/OCO; no false atomic claim | V6 §1822 | P1 | `trading/management` | `test_take_profit_cancels_stop_then_sells`; the packet states per venue whether the stop is attached (HFM: sent with the entry, DIR-04; others: separate orders) | S4, S17 | IMPLEMENTED |
| TRD-44 | Live release gates (§9 list) | §9 | P0 | `trading/golive` | Readiness report shows every gate; gates not passed (B-02, B-06/07) | S8 | PARTIAL |
| TRD-45 | Go-live gates A–I recorded with evidence | V6 §1830 | P0 | `golive_gates` | `cosmic golive record/status`, `/golive` | S8, S10 | IMPLEMENTED |
| TRD-46 | Live enabled only by a separate authenticated owner action after readiness report; never automatic | V6 §1830 Gate I | P0 | `trading/authority` | `test_live_modes_unreachable`, `test_golive_readiness_never_passes_without_broker` | S3, S8 | IMPLEMENTED |
| TRD-47 | Default sequence DISCOVER → … → LEARN; strategy sequence only when enabled | V6 §1834 | P0 | `trading`, `workforce` | `test_complete_mission_from_signal_to_execution` (detect → … → review) | S7 | IMPLEMENTED |
| TRD-48 | Absolute invariants (13 listed) | V6 §1834 | P0 | `trading` | doc 08 §12 maps each invariant to existing tests (checked mechanically) | S3–S7 | IMPLEMENTED |
| TRD-49 | Failure modes and default safe behaviour (V6 §1827 list) | V6 §1827 | P0 | `trading`, `worker` | doc 13 §3 failure-mode matrix with existing tests | S4–S10 | IMPLEMENTED |
| TRD-50 | Explicit emergency policy for open positions; fail-closed ≠ liquidate | V6 §1827 | P0 | risk policy `emergency` block | `EmergencyPolicy`; AT-12 keeps positions | S4 | IMPLEMENTED |
| TRD-51 | Risk/accounting vetoes for unknown positions/orders/cash, failed reconciliation, stale prices, missing data, limits, market access, venue outage, unauthorised instrument, liquidity, compliance, security incident, e-stop | §9 | P0 | `trading/risk` | AT-11, AT-14, AT-15, `test_r15_security_incident_blocks` | S3 | IMPLEMENTED |
| TRD-52 | Proposal, approval, decision, policy version, checks, request, response, fills, exits, fees, positions, P&L, reconciliation, review are audit artefacts | §9 | P0 | `audit`, `order_events` | `test_audit_trail_covers_the_whole_trade` | S4–S5 | IMPLEMENTED |
| TRD-53 | Without a configured broker: finish contract, simulator, tests, UI, docs; keep LIVE disabled; name the missing configuration | §9 | P0 | `trading/brokers/paper` | Paper adapter, simulator, UI, docs; LIVE disabled | S4 | IMPLEMENTED |

## ARC — Application architecture (§10)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| ARC-01 | Simplest architecture: modular monolith + worker processes | §10 | P1 | ADR-003 | Doc 04 | Gate A | DONE |
| ARC-02 | Boundaries: portal, Telegram, API, scheduler/worker, AI gateway, connectors, research/backtest, risk/approval, broker/execution, ledger/reconciliation, evidence, audit/observability, admin | §10 | P1 | package layout | `test_forbidden_imports`, `test_only_adapters_import_http_clients` | S1+ | IMPLEMENTED |
| ARC-03 | Domain logic separate from web, chat, SDKs, DB; no duplicated logic in bot and portal | §10 | P0 | services layer | Portal, API and Telegram call `TradingServices`/`approvals` (`test_full_telegram_approval_with_totp`, `test_full_paper_approval_flow_through_portal`) | S5 | IMPLEMENTED |
| ARC-04 | Explicit contracts, migrations, config validation, typed schemas | §10 | P0 | `db/migrations`, pydantic | `test_metadata_matches_migrated_schema`; pydantic `extra=forbid` models | S1 | IMPLEMENTED |
| ARC-05 | PostgreSQL; Redis only on measured need | §10 | P1 | ADR-005 | Compose has no Redis; Postgres queue | S1 | IMPLEMENTED |
| ARC-06 | Language/framework rationale in ADRs | §10 | P1 | ADR-004, ADR-006 | — | Gate A | DONE |
| ARC-07 | Domain entities (users … notifications) | §10 | P1 | doc 05 | doc 05; migrations 0001–0006 | Gate A | IMPLEMENTED |
| ARC-08 | Canonical IDs; UTC storage; source/local timezone metadata | §10 | P0 | `core/ids`, schema | `test_every_timestamp_is_timezone_aware`, `test_uuid7_*` | S1 | IMPLEMENTED |
| ARC-09 | Decimal/fixed precision for money; no binary float for ledger truth | §10 | P0 | `core/money`, `NUMERIC` | `test_no_floating_point_columns`, `test_money_columns_use_numeric_38_18` | S3 | IMPLEMENTED |
| ARC-10 | Unique constraints, FKs, idempotency keys, transaction boundaries; store minimal sensitive data | §10 | P0 | schema | `test_db_rejects_unbalanced_transaction_at_commit`, `test_submit_idempotent_by_client_order_id`, `test_enqueue_idempotent` | S1–S4 | IMPLEMENTED |

## UX — Operator portal (§11, V6 §1824)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| UX-01 | Home answers: what changed, what matters, verified vs inferred, uncertain/contradictory, active risks/incidents, work running/blocked/overdue, trades awaiting approval, human attention, health/freshness | §11 | P1 | portal `/` | `test_command_center_answers_the_questions` | S8 | IMPLEMENTED |
| UX-02 | Views: command center; sources/evidence; research/tasks; market/strategy; portfolio/risk/ledger; approval queue; orders/fills/reconciliation/audit; AI providers; operations/settings/access | §11 | P1 | portal | Authz matrix enumerates every view | S8 | IMPLEMENTED |
| UX-03 | Every button has real behaviour and states; no fake metrics, charts or simulated-as-live data | §11 | P0 | portal | `test_every_form_posts_to_a_real_route` | S8 | IMPLEMENTED |
| UX-04 | Accessibility, responsive layout, validation, readable tables, keyboard navigation, useful errors | §11 | P1 | templates/CSS | `test_every_page_is_structurally_accessible` (lang, one h1, labelled controls, unique ids, alt, named buttons, no positive tabindex, scoped headers; the checker is itself tested); no screen-reader audit | S8 | IMPLEMENTED |
| UX-05 | Trading control center: mode + live indicator, broker health, balances/positions/orders/fills/last reconciliation, proposal queue, approval detail, order lifecycle, exposure/P&L/drawdown/limits, mandates, kill switch/protection/recovery, mismatches/errors/provider status/freshness/audit | V6 §1824 | P0 | portal `/trading` | `/portfolio`, `/orders`, `/ops`; `test_full_paper_approval_flow_through_portal` | S8 | IMPLEMENTED |
| UX-06 | Simple language default; drill-down; blocked orders never hidden behind green; app health ≠ account health | V6 §1824 | P0 | portal | Command center separates system health from account health; packet plain language first | S8 | IMPLEMENTED |

## TG — Telegram (§11, V6 §1823)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| TG-01 | Verify operator by immutable Telegram user ID; optional extra authentication | §11 | P0 | `notify/telegram` | `test_private_only_and_unknown_users`, `test_full_telegram_approval_with_totp` | S5 | IMPLEMENTED |
| TG-02 | Never approve from group or untrusted messages | §11 | P0 | handlers | `test_private_only_and_unknown_users` | S5 | IMPLEMENTED |
| TG-03 | Approval actions expire, resist replay, are audited | §11 | P0 | handlers | `test_confirmation_expires`, `test_other_user_cannot_use_callbacks` | S5 | IMPLEMENTED |
| TG-04 | Portal works when bot credentials are unavailable | §11 | P0 | portal | Bot behind profile; portal flow verified without it | S5 | IMPLEMENTED |
| TG-05 | Proposal message: summary + REVIEW DETAILS, EVIDENCE, COUNTERARGUMENTS, RISK, PORTFOLIO IMPACT views | V6 §1823 | P1 | handlers | Handler tests (mocked updates) | S5 | IMPLEMENTED |
| TG-06 | APPROVE EXACT TRADE / REJECT / REQUEST CHANGES; exact terms on confirmation; second confirmation | V6 §1823 | P0 | handlers | `test_full_telegram_approval_with_totp`, `test_reject_and_request_changes` | S5 | IMPLEMENTED |
| TG-07 | No ambiguous controls; approvals never inferred from another channel | V6 §1823 | P0 | handlers | Free-text "ok"/"looks good" ignored (test_telegram) | S5 | IMPLEMENTED |
| TG-08 | No secrets/unneeded identifiers in chats; beginner + detail views; distinct status labels | V6 §1823 | P0 | handlers | `test_replies_never_contain_secrets` | S5 | IMPLEMENTED |
| TG-09 | Kill switch in Telegram | V6 §1817 | P0 | handlers | `test_emergency_stop_from_telegram` | S5 | IMPLEMENTED |
| TG-10 | Telegram Mini App as a primary interface | V6 header | P2 | — | ADR-021 | — | DEFERRED |
| TG-11 | Live bot operation | — | P1 | `bot` process | @cosmictradingBot running on staging (`getMe` ok); operator links Telegram via the portal | S5 | IMPLEMENTED |

## SEC — Security and privacy (§12, V6 §1820, §1825)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| SEC-01 | Threat model covering the 14 listed threat classes before public exposure | §12 | P0 | doc 09 | doc 09 (T1–T19, residual risks) | Gate A | IMPLEMENTED |
| SEC-02 | Strong auth, RBAC, secure cookies, CSRF, rate limits, input validation | §12 | P0 | `identity`, `web` | `test_web_auth.py`, `test_authz_matrix.py`, `test_api.py` | S1 | IMPLEMENTED |
| SEC-03 | No hard-coded credentials; `.env.example`; gitignore; secret scanning | §12 | P0 | repo | Secret scan in `make check` | S1 | IMPLEMENTED |
| SEC-04 | Dependency pinning and scanning | §12 | P0 | `uv.lock`, pip-audit | `make scan`: pip-audit (no known vulnerabilities, 2026-10-11), bandit clean | S1, S10 | IMPLEMENTED |
| SEC-05 | Security headers | §12 | P0 | `web/security` | `test_security_headers` | S1 | IMPLEMENTED |
| SEC-06 | Audit logging for privileged and financial actions | §12 | P0 | `audit` | `test_audit_trail_covers_the_whole_trade` | S1+ | IMPLEMENTED |
| SEC-07 | Encryption in transit; DB access control | §12 | P0 | tunnel TLS; DB roles | DB roles/grants tested (`test_app_role_has_no_ddl`); TLS terminates at Cloudflare (not configured, B-03); internal Docker traffic unencrypted on one VM | S1, S9 | PARTIAL |
| SEC-08 | Backups protected; log redaction | §12 | P0 | backup scripts; logging | age-encrypted backups; `test_redaction.py` | S1, S9 | IMPLEMENTED |
| SEC-09 | MFA for privileged administration | §12 | P0 | `identity/mfa` | `test_mfa_enrolment_step_up_and_require` | S1 | IMPLEMENTED |
| SEC-10 | Separate dev/test/paper/live credentials | §12 | P0 | `core/config` | `test_production_like_tiers_reject_dev_settings` | S1 | IMPLEMENTED |
| SEC-11 | Workers get allowlisted tools; research has no broker tool | §12 | P0 | `workforce/tools` | `test_toolbox_enforces_allowlist` | S7 | IMPLEMENTED |
| SEC-12 | Research output never executes without typed validation + authorisation + risk | §12 | P0 | `trading` | `test_at08_strategy_authority_locked` | S5–S7 | IMPLEMENTED |
| SEC-13 | Untrusted content cannot change permissions, approvals, limits, strategy authority, config, credentials | V6 §1820 | P0 | `ai/safety` | `test_invalid_output_and_injection_contained` | S6–S7 | IMPLEMENTED |
| SEC-14 | No shell/DB-write tools for research; LLM never builds broker payloads; rebuild from approved data | V6 §1820 | P0 | `trading/execution` | AI output schemas `extra=forbid`; proposals built by code from validated fields | S4 | IMPLEMENTED |
| SEC-15 | SSRF protection for research URL fetching | §12 | P0 | `sources/http` | `test_ssrf.py` | S2 | IMPLEMENTED |
| SEC-16 | Tamper-evident append-only execution audit with the listed contents | V6 §1825 | P0 | `audit` (hash chain + triggers) | `test_tamper_is_detected` | S1 | IMPLEMENTED |
| SEC-17 | No credentials in audit; stable IDs, timezone timestamps, correlation IDs; detect gaps/ordering | V6 §1825 | P0 | `audit` | `test_payload_is_redacted`, `test_deletion_gap_is_detected` | S1 | IMPLEMENTED |
| SEC-18 | Application code cannot rewrite, delete or backdate audit history | V6 §1825 | P0 | DB grants + triggers | `test_app_role_cannot_modify_audit`, `test_even_owner_triggers_block_update` | S1 | IMPLEMENTED |
| SEC-19 | Incident response | §12 | P1 | `incidents`, doc 15 | `test_dedup_escalation_and_resolution`; doc 15 | S4, S10 | IMPLEMENTED |
| SEC-20 | Webhook forgery, flooding, quota exhaustion | §12 | P0 | no inbound webhooks (long polling); rate limits | No inbound webhooks; `test_login_rate_limited`; AI quota | S1, S5 | IMPLEMENTED |

## DEP — Proxmox VM deployment (§13, V6 §1828)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| DEP-01 | Dedicated VM, not the hypervisor host; no destructive hypervisor changes | §13 | P0 | doc 12 | `create-vm.sh` dry run (refuses existing VMID, no host changes); VM not created (B-02) | S9 | PARTIAL |
| DEP-02 | Inspect VM inventory only where access exists; don't assume credentials | §13 | P0 | 01 audit | No Proxmox access exists | — | BLOCKED (B-02) |
| DEP-03 | Supported Linux distribution | §13 | P1 | Ubuntu 24.04 LTS (ADR-017) | Ubuntu 24.04 cloud image + `bootstrap.sh` dry run; not executed (B-02) | S9 | PARTIAL |
| DEP-04 | Realistic CPU/RAM/disk: minimum, recommended, observed | §13 | P0 | doc 16 | Observed: local run 2026-10-11 (doc 12 §7); VM measurement BLOCKED (B-02) | S9 | PARTIAL |
| DEP-05 | Separate storage for database, app data, backups | §13 | P1 | compose volumes, VM disks | Named DB volume, separate backup/config/secret dirs; second backup disk recommended, not provisioned | S9 | PARTIAL |
| DEP-06 | Non-root application user | §13 | P0 | Dockerfile | UID 10001 verified in containers; `test_deploy_artifacts` | S9 | IMPLEMENTED |
| DEP-07 | Docker Compose with pinned images (no `latest`) | §13 | P0 | `deploy/compose.prod.yaml` | `test_every_external_image_is_pinned_by_digest` | S9 | IMPLEMENTED |
| DEP-08 | Health checks, restart policies, limits, named volumes, clean shutdown | §13 | P0 | compose | Healthchecks, restart policies, limits, named volume, `stop_grace_period`; local run 2026-10-11 (doc 12 §7) | S9 | IMPLEMENTED |
| DEP-09 | VM autostart on host boot; services start on VM boot | §13 | P1 | `qm set --onboot`, systemd unit | systemd unit + `--onboot`; container restart verified; VM reboot BLOCKED (B-02) | S9 | PARTIAL |
| DEP-10 | DB, Redis, metrics, SSH, internal APIs never on public hostnames | §13 | P0 | compose networks, tunnel ingress | `test_production_publishes_no_ports`, `test_network_segmentation`; web has no egress (verified) | S9 | IMPLEMENTED |
| DEP-11 | Guest and Proxmox firewalls consistent; avoid lockout | §13 | P1 | `deploy/vm/bootstrap.sh` (ufw), `create-vm.sh` (`firewall=0` on the NIC: guest firewall is the single source) | Dedicated-VM ufw plan in `bootstrap.sh` (dry run); on the shared test server nothing is published and host firewall is not ours to change | S9 | PARTIAL |
| DEP-12 | Document IP, routing, DNS, outbound access, recovery | §13 | P1 | doc 12 | doc 12 | S9 | IMPLEMENTED |
| DEP-13 | Idempotent deploy/update scripts with dry-run/confirmation | §13 | P0 | `deploy/deploy.sh` | `deploy.sh` run for real on the test server (first deploy, redeploys with pre-deploy backups) | S9 | IMPLEMENTED |
| DEP-14 | No destructive operations without confirmation and rollback | §13 | P0 | scripts | Scripts dry-run by default, never delete volumes, back up before overwrite; restore into a new DB only | S9 | IMPLEMENTED |
| DEP-15 | Measure real resource use; reduced profile if needed | §13 | P0 | doc 16 | `docker stats` samples, local run 2026-10-11 (doc 12 §7); healthcheck CPU defect found and fixed | S9 | IMPLEMENTED |
| DEP-16 | 10 GB RAM / 200 GB SSD plan with hard budgets; background work yields to safety | V6 §1828 | P0 | compose limits, `worker` pressure checks | compose limits; `test_background_work_is_shed_under_disk_pressure`, `test_disk_thresholds` | S9 | IMPLEMENTED |
| DEP-17 | Bounded queues, backpressure, timeouts, breakers, log rotation, storage thresholds, incremental ingestion | V6 §1828 | P1 | `worker`, compose logging | queue priorities/leases, breakers, json-file rotation, disk thresholds, incremental ingestion | S2, S9 | IMPLEMENTED |
| DEP-18 | Actual deployment to the Proxmox VM | §13, §19 Ph10 | P0 | — | Needs host access | S9 | BLOCKED (B-02) |

## CF — Cloudflare Tunnel, DNS and domain (§14)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| CF-01 | Don't guess a domain or reuse other apps' domains | §14.1 | P0 | ADR-018 | No domain configured | — | DONE (none used) |
| CF-02 | No nameserver changes or record deletion without authorisation, inventory, rollback | §14.2 | P0 | doc 12 | doc 12 §5; `dns-inventory.sh` is GET-only; no DNS change made | S9 | IMPLEMENTED |
| CF-03 | Dedicated hostnames only as needed; private services stay private | §14.3 | P0 | ingress template | `test_tunnel_template_has_catch_all_404_and_hides_internal` | S9 | IMPLEMENTED |
| CF-04 | Origin bound to private network; no bypass of the tunnel | §14.4 | P0 | compose (no published ports) | No published ports; web on internal networks only (verified) | S9 | IMPLEMENTED |
| CF-05 | Tunnel token is a secret file; least-privilege tokens; never printed | §14.5 | P0 | `--token-file` | `--token-file`; secret only mounted into cloudflared (`test_secrets_are_least_privilege`) | S9 | IMPLEMENTED |
| CF-06 | Catch-all rule rejects unknown hostnames; explicit routes | §14.6 | P0 | `deploy/cloudflared/config.example.yml` | `cloudflared tunnel ingress validate` OK + `ingress rule` checks (cloudflared 2026.9.1) | S9 | IMPLEMENTED |
| CF-07 | Application auth even behind Cloudflare; Access for operator surfaces | §14.7 | P0 | `identity`; Access policy doc | App login + MFA, and Cloudflare Access (owner e-mail OTP + service token) on staging and ssh hostnames (2026-10-12) | S9 | IMPLEMENTED |
| CF-08 | Verify CNAME, tunnel health, TLS, auth, origin isolation, unapproved hostnames | §14.8 | P0 | verification script | `verify-public.sh staging.cosmictrading.bond` → ALL CHECKS PASSED (2026-10-12); production hostname pending | S9 | IMPLEMENTED |
| CF-09 | Document manual steps when API access is missing; don't pretend DNS is done | §14.9 | P0 | doc 12 | doc 12 §5, BLOCKERS B-03 | S9 | IMPLEMENTED |
| CF-10 | Separate DEV/STAGING/PRODUCTION; never point production at an untested build | §14.10 | P1 | doc 12 | Procedure in doc 12/15; no staging environment exists | S9 | PARTIAL |
| CF-11 | No public SSH hostname without client-side access + strong auth | §14.11 | P0 | doc 12 | No SSH route; break-glass via SSH local forward to 127.0.0.1 only | S9 | IMPLEMENTED |
| CF-12 | Minimal public routes; no debug/stack traces | §14.12 | P0 | `web/errors`, ingress | Generic error pages; catch-all 404; `/internal/*` 404 | S1, S9 | IMPLEMENTED |
| CF-13 | Test tunnel, DNS, app health, Access independently; "deployed" only when all verified | §14 | P0 | doc 12 | Tunnel, DNS, TLS, Access and app health verified separately for staging (docs/12 §8) | S9 | IMPLEMENTED |

## CFG — Configuration and environments (§15)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| CFG-01 | Environments LOCAL_DEVELOPMENT, TEST, PAPER_TRADING, STAGING, LIVE_DISABLED/LIVE_PENDING_APPROVAL, LIVE | §15 | P0 | `core/config` | `test_config.py` | S1 | IMPLEMENTED |
| CFG-02 | Validated config, safe defaults, early failure with non-secret messages | §15 | P0 | `core/config` | `test_error_messages_never_contain_values`, `test_invalid_secret_key_length` | S1 | IMPLEMENTED |
| CFG-03 | `.env.example`, configuration docs, startup validation | §15 | P0 | repo root, doc 15 | `.env.example`, `deploy/env/*.example` | S1 | IMPLEMENTED |
| CFG-04 | No keys in browser storage or frontend builds | §15 | P0 | portal | `test_pages_assets_and_logs_never_contain_credentials` | S8 | IMPLEMENTED |
| CFG-05 | Production cannot use dev secrets, test broker or seeded demo accounts by accident | §15 | P0 | `core/config` | `test_production_like_tiers_reject_dev_settings`, `test_fault_injection_refused_outside_dev` | S1 | IMPLEMENTED |
| CFG-06 | Feature flags for integrations; disabled shows NOT_CONFIGURED | §15 | P0 | `core/config`, portal | `integration_status()`; command center/AI pages show NOT_CONFIGURED | S1, S8 | IMPLEMENTED |

## TST — Testing and quality gates (§16, V6 §1830, §1831)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| TST-01 | Unit tests: domain, normalisation, indicators, money, permissions, limits, expiry, idempotency, transitions, source trust, routing | §16 | P0 | `tests/unit` | unit suite | all | IMPLEMENTED |
| TST-02 | Integration: migrations, transactions, handlers, worker lifecycle, adapters with fakes, connectors, Telegram mocks, broker contracts, audit persistence | §16 | P0 | `tests/integration` | integration suite | all | IMPLEMENTED |
| TST-03 | E2E: login, navigation, tasks, ingestion, evidence, AI exhaustion, proposal, approve/reject/expire, order simulation, reconciliation failure, kill switch, recovery | §16 | P0 | `tests/e2e` | e2e suite + local production-like run | S8 | IMPLEMENTED |
| TST-04 | Security: authz matrix, IDOR, CSRF/XSS/injection, secret exposure, replay, prompt injection, SSRF, rate limits, isolation, revocation | §16 | P0 | `tests/security` | security suite | S1–S10 | IMPLEMENTED |
| TST-05 | Trading simulations: accept/reject, partial, slippage, out-of-band price, timeout unknown, disconnect, duplicate webhook, cancel race, stale balance, limit breach, kill switch, restart | §16 | P0 | `tests/trading` | trading suite (paper venue faults) | S4 | IMPLEMENTED |
| TST-06 | Reliability: DB down, AI down, quota exhausted, stale source, worker crash, disk nearly full, tunnel down, VM restart, restore, migration rollback | §16 | P0 | `tests/reliability`, S9 scripts | DB/AI/quota/stale/worker-kill/disk covered; VM restart BLOCKED (B-02) | S9–S10 | PARTIAL |
| TST-07 | Release: formatter, lint, types, migrations, tests, scans, container build, smoke | §16 | P0 | `Makefile` | `make check`, `make scan`, image build, `scripts/local-verify.sh` | S10 | IMPLEMENTED |
| TST-08 | No credentials or real money for tests; deterministic fixtures and simulated brokers | §16 | P0 | tests | Socket guard in `tests/conftest.py`; fixtures; paper venue | S1 | IMPLEMENTED |
| TST-09 | Property-based tests where practical | V6 §1830 Gate B | P1 | hypothesis | Hypothesis: sizing, rounding, indicators | S3 | IMPLEMENTED |
| TST-10 | Go-live gates A–I evidence | V6 §1830 | P0 | readiness report | Gates recorded and reported; C/E/F/G/I BLOCKED (no broker, no VM) | S10 | PARTIAL |

### Required automated acceptance tests (V6 §1831)

| ID | Test | Pri | Test location (planned) | Slice | State |
|---|---|---|---|---|---|
| AT-01 | In RESEARCH_ONLY all live-order attempts are rejected | P0 | `test_at01_research_only_rejects_orders`, `test_at01_mode_change_after_approval_blocks` | S4 | IMPLEMENTED |
| AT-02 | In PAPER_TRADING simulated orders cannot reach a live endpoint | P0 | `test_at02_paper_cannot_reach_live` | S4 | IMPLEMENTED |
| AT-03 | In APPROVAL_REQUIRED nothing is submitted before authenticated approval | P0 | `test_at03_no_submission_without_approval` | S5 | IMPLEMENTED |
| AT-04 | Approval applies to one exact packet and cannot be replayed | P0 | `test_at04_approval_single_use_and_not_replayable`, `test_at04_wrong_operator_channel_or_hash` | S5 | IMPLEMENTED |
| AT-05 | Expired or materially changed packet cannot execute | P0 | `test_at05_expired_approval_cannot_execute`, `test_at05_challenge_and_intent_expiry`, `test_at05_materially_changed_conditions` | S5 | IMPLEMENTED |
| AT-06 | Approved trade executes only within approved quantity, price, instrument, account, order type | P0 | `test_at06_execution_matches_approved_terms_exactly` | S5 | IMPLEMENTED |
| AT-07 | Risk veto blocks an approved trade when hard limits fail | P0 | `test_at07_risk_veto_after_approval` | S5 | IMPLEMENTED |
| AT-08 | Strategy authority disabled by default; LLM/research worker cannot enable it | P0 | `test_at08_strategy_authority_locked` | S5 | IMPLEMENTED |
| AT-09 | Duplicate callback/retry/timeout creates no duplicate exposure | P0 | `test_at09_no_duplicate_exposure`, `test_at09_timeout_does_not_resubmit` | S5 | IMPLEMENTED |
| AT-10 | Partial fills update positions and permitted exit quantities | P0 | `test_at10_partial_fills_drive_exit_quantities` | S4 | IMPLEMENTED |
| AT-11 | Unknown order state blocks new exposure and triggers reconciliation | P0 | `test_at11_unknown_state_blocks_new_exposure` | S4 | IMPLEMENTED |
| AT-12 | Kill switch blocks new entries and preserves audit/order state | P0 | `test_at12_kill_switch`; local run | S4 | IMPLEMENTED |
| AT-13 | AI outage/quota exhaustion leaves hard risk and reconciliation working | P0 | `test_at13_ai_outage_does_not_affect_safety` | S6 | IMPLEMENTED |
| AT-14 | Missing/stale market data blocks actions needing current data | P0 | `test_at14_stale_data_blocks` | S3 | IMPLEMENTED |
| AT-15 | Account mismatch or accounting uncertainty blocks new exposure | P0 | `test_at15_account_mismatch_blocks` | S4 | IMPLEMENTED |
| AT-16 | Credentials absent from prompts, frontend assets, Telegram output, logs | P0 | `test_secret_exposure.py`, `test_replies_never_contain_secrets`, `test_keys_only_in_auth_header_never_in_usage_or_body` | S6–S8 | IMPLEMENTED |
| AT-17 | Broker-confirmed state distinguished from local request state | P0 | `test_at17_broker_confirmed_vs_local` | S4 | IMPLEMENTED |
| AT-18 | Owner can inspect exactly why an order was blocked or permitted | P0 | `test_at18_owner_can_see_why` | S5 | IMPLEMENTED |
| AT-19 | Restore from backup preserves approvals, strategy versions, orders, fills, audit, reconciliation | P0 | `deploy/backup/restore-drill.sh` — DRILL PASS (10 checks), negative cases fail; local run 2026-10-11 (doc 12 §7) | S9 | IMPLEMENTED |
| AT-20 | VM restart recovers safely without resubmitting an ambiguous order | P0 | `test_at20_*` (process); container SIGKILL ×4 + compose restart 17/17, 10/10; VM restart BLOCKED (B-02) | S4, S9 | PARTIAL |

## OBS — Observability, backup and recovery (§17)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| OBS-01 | Structured logs, correlation IDs, redaction | §17 | P0 | `core/logging` | `test_redaction.py`, correlation IDs | S1 | IMPLEMENTED |
| OBS-02 | Metrics: API, queue age, worker failures, freshness, DB, AI usage/errors/cost, broker, reconciliation, resources | §17 | P1 | `ops/metrics` | `test_metrics_require_token`; `/internal/metrics` | S8 | IMPLEMENTED |
| OBS-03 | Alerts actionable, rate-limited, owned, expiring | §17 | P1 | `incidents`, notify | `test_dedup_escalation_resolution_and_alerts` (alert on open and escalation only); `test_alert_requires_every_answer` | S4, S8 | IMPLEMENTED |
| OBS-04 | Liveness and readiness | §17 | P0 | `/healthz`, `/readyz` | `test_health_and_readiness`; container healthchecks | S1 | IMPLEMENTED |
| OBS-05 | Source/model/provider health page | §17 | P1 | portal | `/sources`, `/ai` pages | S8 | IMPLEMENTED |
| OBS-06 | Database and queue monitoring | §17 | P1 | `ops` | `/ops` (queue, heartbeats), metrics | S8 | IMPLEMENTED |
| OBS-07 | Order/position reconciliation alarms | §17 | P0 | `reconcile` | `test_reconciliation_detects_unexpected_venue_fill_order`, `test_at15_account_mismatch_blocks` | S4 | IMPLEMENTED |
| OBS-08 | Log rotation and disk-space alerts | §17 | P1 | compose logging; `ops/resources` | json-file 10 MB×5, journald cap; `test_disk_thresholds`, disk incident job | S9 | IMPLEMENTED |
| OBS-09 | Encrypted, database-aware backups plus config backup | §17 | P0 | `deploy/backup` | `cosmic-backup.sh` (pg_dump → age, no plaintext on disk); local run 2026-10-11 (doc 12 §7) | S9 | IMPLEMENTED |
| OBS-10 | Documented restore drills to an isolated target | §17 | P0 | `deploy/backup/restore-drill.sh` | Drill executed, results in doc 12 §7 / IMPLEMENTATION-STATUS S9 | S9 | IMPLEMENTED |
| OBS-11 | Retention policy and backup inventory | §17 | P1 | doc 14 | `cosmic backup prune` (unit-tested) + `.sha256` per set | S9 | IMPLEMENTED |
| OBS-12 | RPO/RTO | §17 | P1 | doc 14 | doc 14 §6 | S9 | IMPLEMENTED |
| OBS-13 | Tested migration and rollback instructions | §17 | P1 | Alembic downgrade test | `test_migrations` downgrade test; rollback in `deploy.sh` and doc 15 §6 | S1, S9 | IMPLEMENTED |
| OBS-14 | Incident runbook and postmortem template | §17 | P1 | doc 15 | doc 15 incidents + postmortem template | S10 | IMPLEMENTED |
| OBS-15 | No `.env`/tokens in unencrypted backups; preserve incident evidence | §17 | P0 | backup script | Secrets never archived; plaintext check step in local verification | S9 | IMPLEMENTED |

## COST — Cost and resource efficiency (§18)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| COST-01 | AI inference budget exactly $0 | §18 | P0 | `ai/policy` | `test_billing_signal_disables_provider`, `test_openrouter_requires_fresh_zero_price_and_no_fallbacks` | S6 | IMPLEMENTED |
| COST-02 | Report total operating cost separately (domain, hardware, power, internet, backups, data, broker fees, notifications) | §18 | P1 | doc 16 | doc 16 | Gate A | IMPLEMENTED |
| COST-03 | Cost controls: deterministic first, dedupe, cache with expiry, pause low-priority work, no swarms, smallest model, measure, optional subsystems off | §18 | P1 | `ai`, `worker` | AI cache/dedupe tests; `test_background_work_is_shed_under_disk_pressure`; measured in doc 12 §7 | S6, S9 | IMPLEMENTED |

## REPO — Repository quality (§20)

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| REPO-01 | Cohesive modules, directional dependencies, no circular imports, no giant files, no hidden globals | §20 | P1 | import-contract test | `test_architecture.py` | S1+ | IMPLEMENTED |
| REPO-02 | Types and schemas at trust boundaries | §20 | P0 | pydantic + mypy strict | mypy strict, 156 files | S1+ | IMPLEMENTED |
| REPO-03 | Migrations only; no manual schema edits | §20 | P0 | Alembic | `test_metadata_matches_migrated_schema`, `test_fresh_upgrade_downgrade_upgrade` | S1 | IMPLEMENTED |
| REPO-04 | Graceful shutdown, idempotent jobs, bounded retries | §20 | P1 | `worker` | SIGTERM handling, `test_expired_lease_is_recovered`, `test_fail_backoff_then_dead`, watchdog | S2+ | IMPLEMENTED |
| REPO-05 | Pinned dependencies; documented versions | §20 | P0 | `uv.lock` | `uv.lock`; image digests | S1 | IMPLEMENTED |
| REPO-06 | Reproducible commands; single test entry point | §20 | P0 | `Makefile` (`make check`) | `make check`, `make scan`, `scripts/local-verify.sh` | S1 | IMPLEMENTED |
| REPO-07 | Generated artefacts and local state stay out of Git | §20 | P0 | `.gitignore` | Review | S1 | DONE |
| REPO-08 | API endpoints, permissions, contracts, errors documented | §20 | P1 | doc 10 | doc 10: every listed endpoint built (S11); `test_api_read_models.py` | Gate A | IMPLEMENTED |
| REPO-09 | Useful errors without stack traces or secrets | §20 | P0 | `web/errors` | `test_errors_do_not_leak_internals` | S1 | IMPLEMENTED |
| REPO-10 | Real integrations only where credentials exist; otherwise testable adapters + setup instructions | §20 | P0 | adapters, doc 15 | Only keyless data + paper venue are live; AI/Telegram adapters tested with fakes | all | IMPLEMENTED |
| REPO-11 | No empty TODOs for high-priority requirements | §20 | P0 | grep in `make check` | `make lint` fails on TODO/FIXME/XXX/HACK | S10 | IMPLEMENTED |
| REPO-12 | No gratuitous rewrites | §20 | P1 | — | N/A (greenfield) | — | N/A |

## MIS — Mission-level capability map (V6 §1, North Star, §1835)

| ID | Requirement | Source | Pri | State |
|---|---|---|---|---|
| MIS-01 | Governed, evidence-driven, auditable financial intelligence institution: observe, find, verify, research, test, challenge, rank, coordinate, explain, remember | First slice: crypto spot research + paper trading | P1 | PARTIAL |
| MIS-02 | Serve beginners without hiding risk; forensic access for advanced users | Plain-language first views with detail on demand; Mini App deferred | P1 | PARTIAL |
| MIS-03 | Global Intelligence OS, Search Fabric, World Brain, knowledge graph, broad asset coverage (baseline Sections 0–1803) | V6 §0 | P3 | BLOCKED (B-01) / DEFERRED |
| MIS-04 | Prime directive: when authorization, data, account state or risk is uncertain, block new exposure, verify, escalate | Invariant and failure-mode tests (doc 08 §12, doc 13 §3) | P0 | IMPLEMENTED |

## DIR — Owner's master development directive (2026-10-15)

The owner's second directive (pasted 2026-10-15; Phases 1–12) adds operational requirements. Rows are added as each item is worked on.

| ID | Requirement | Source | Pri | Module | Verification | Slice | State |
|---|---|---|---|---|---|---|---|
| DIR-01 | Audit the actual state first; report confirmed, partial, broken, missing, security risks, next tasks | Ph. 1 | P0 | docs | `docs/reports/2026-10-15-audit.md` (restore drill PASS on the newest staging backup; `make check` baseline) | S17 | DONE |
| DIR-02 | Environment label (PAPER, TESTNET, DEMO, LIVE) on every proposal, Telegram message and account view | Ph. 6 | P0 | `trading/modes`, notify, portal | `test_every_proposal_names_its_environment`, `test_environments_never_read_safer_than_they_are`, HFM packet assertions in `test_buy_stop_and_exit_through_the_bridge` | S17 | PARTIAL (log lines and order rows carry the account, from which the label follows, not the label itself) |
| DIR-03 | Approval terms state the venue's real behaviour: stop type, session, funding (CFD swap) | Ph. 5–6 | P0 | `trading/venue_terms`, `trading/proposals` | `test_venue_terms_describe_cfds_and_broker_stops`, `test_buy_stop_and_exit_through_the_bridge` | S17 | IMPLEMENTED |
| DIR-05 | Alert on missed or failed backups; restoration checked regularly | Ph. 8 | P0 | `ops/backups`, `ops/backup_check`, worker | `test_backup_check.py` (late, incomplete, being written, drill due/failed, no directory), `test_late_backups_open_incidents_that_close_by_themselves`; `scripts/drill-staging.sh` | S17 | IMPLEMENTED |
| DIR-06 | Daily Telegram report (mode, health, closed trades with fees, exposure, drawdown and limit use, strategies, errors and pending actions, backups and reconciliation), per account, never mixing environments; concise health command | Ph. 8, 10 | P1 | `notify/report`, worker, bot | `test_the_report_names_each_account_and_never_adds_them_up`, `test_the_report_is_sent_once_a_day_after_its_hour`, `test_health_lines_are_the_status_answer`, `/status` test | S17 | PARTIAL (weekly and monthly reports not built) |
| DIR-07 | Log cleanup and retention | Ph. 8 | P1 | `worker/queue` | `test_finished_jobs_are_pruned_after_retention` | S17 | PARTIAL (job history only; quotes and fetch runs are kept as evidence) |
| DIR-04 | Use the venue's attached protective order where it has one; otherwise handle the risk window and fail safe | Ph. 3–4 | P0 | `trading/brokers/mt5_bridge`, `bridge/`, `trading/execution/entry` | HFM: `test_the_stop_loss_travels_with_the_entry`, `test_a_resting_entry_carries_its_stop_into_the_position`, `test_the_entry_is_protected_before_its_stop_order_exists`, `test_an_old_bridge_is_refused_an_entry_with_a_stop`; contract version 2 on the real demo account. Binance and paper keep separate protective orders (TRD-43). | S17 | IMPLEMENTED (emulator); real demo check below |
