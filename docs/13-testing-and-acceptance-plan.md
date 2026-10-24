# 13 — Testing and Acceptance Plan

## 1. Entry points

| Command | Runs |
|---|---|
| `make check` | `ruff format --check`, `ruff check`, `mypy --strict src`, secret scan, full `pytest` (requires the dev Postgres from `make db-up`) |
| `make test-unit` | tests that need no database |
| `make test` | full pytest |
| `make scan` | `pip-audit`, `bandit -r src` |
| `make verify-prod-local` | builds the image and runs the local production-like verification (S9) |

Database tests run against real PostgreSQL 17 (`compose.dev.yaml`, `127.0.0.1:55442`). A session fixture creates a fresh database, runs Alembic migrations to head as `cosmic_owner`, and connects the application as `cosmic_app`, exactly like production. Between tests, tables are truncated by the superuser with triggers disabled for that session only. If the database is unavailable, DB tests **fail** (they are not skipped), so a green run always includes them.

Unit tests block outbound network access (socket guard fixture). External APIs are exercised only through recorded fixtures (`tests/fixtures/`, captured 2026-10-10 from the real public endpoints) and `httpx.MockTransport`. A separate, opt-in `make smoke-live-data` performs a real keyless fetch and is reported separately.

## 2. Test layers (mapped to §16)

| Layer | Directory | Examples |
|---|---|---|
| Unit | `tests/unit` | money/precision, uuid7, config validation, freshness labels, indicators (reference values), regime, sizing, risk checks (pass/fail/unknown each), order transitions, approval binding/hash, AI policy/routing/breaker/cache, redaction, capability matrix, architecture import contracts |
| Property | `tests/unit` (hypothesis) | rounding never increases quantity; sizing never exceeds caps; ledger transactions balance; risk monotonicity (tighter limits never turn FAIL into PASS); state machine never leaves terminal states |
| Integration | `tests/integration` | migrations up/down + drift, audit chain + tamper, role grants, ingestion idempotency with fixtures, job queue concurrency (SKIP LOCKED), ledger deferred constraint, reconciliation steps, AI gateway with fake transport |
| Trading simulation | `tests/trading` | acceptance AT-01…AT-20, broker contract suite on PaperBroker, partial fills, cancel/fill race, timeout → unknown → reconcile, kill switch mid-queue, restart recovery |
| Security | `tests/security` | authz matrix (every route × role), IDOR, CSRF, XSS escaping, SQL injection strings, secret exposure (logs, HTML, prompts, Telegram text), approval replay, Telegram wrong user/group, prompt injection, SSRF, rate limit/lockout, session revocation |
| E2E | `tests/e2e` | ASGI client + worker functions with a controlled clock: login + MFA → mission (fake AI) → proposal → approve → paper execution → fills → reconciliation → kill switch → recovery |
| Reliability | `tests/reliability` + S9 scripts | DB unavailable at startup, AI all down, quota exhausted, stale source, worker crash, disk threshold, migration rollback, container restart, backup/restore drill |

## 3. Failure-mode matrix (V6 §1827)

Test names as they exist in `tests/` (verified 2026-10-11); "local run" = `scripts/local-verify.sh` (doc 12 §7).

| Failure | Expected | Evidence |
|---|---|---|
| LLM unavailable / quota exhausted / malformed / contradictory output | AI tasks wait or fail as invalid; no proposal; safety unaffected | `test_at13_ai_outage_does_not_affect_safety`, `test_not_configured_waits_without_calls`, `test_local_quota_budget`, `test_invalid_output_and_injection_contained`, `test_no_ai_means_no_ai_researched_proposal` |
| Internet down / delayed data / invalid prices / schema break / source disagreement | fetch run fails, label STALE/UNAVAILABLE, rows rejected, R09/R10 block | `test_source_outage_recorded_and_incident`, `test_invalid_records_rejected_with_reasons`, `test_coinbase_schema_errors`, `test_cross_source_deviation`, `test_market_pages_report_stale_data`, `test_stale_data_blocks_exposure_at14` |
| Broker outage / rate limit / maintenance / auth expiry / ambiguous response | no submission; ambiguous → UNKNOWN, reconciled, never resubmitted | `test_fault_modes`, `test_at09_timeout_does_not_resubmit`, `test_at11_unknown_state_blocks_new_exposure` (auth expiry/rate limit: no live broker, B-06) |
| DB/queue unavailable; low disk; restart during submission; hung worker | readiness fails; background paused; startup sweep; watchdog restart | `test_health_and_readiness`, `test_expired_lease_is_recovered`, `test_background_work_is_shed_under_disk_pressure`, `test_disk_thresholds`, `test_at20_*`, `test_watchdog_exits_only_when_the_loop_stalls`; local run: SIGKILL ×4, compose restart |
| Duplicate callbacks / expired / replayed approval / wrong owner / stale dashboard | rejected (409); audited | `test_at04_*`, `test_at05_*`, `test_other_user_cannot_use_callbacks`, `test_confirmation_expires`; local run: re-used approval → 409 |
| Partial fill / unexpected fill / rejection / cancel-fill race / missing protective order | ledger correct; protection DEGRADED; alert | `test_at10_partial_fills_drive_exit_quantities`, `test_reconciliation_detects_unexpected_venue_fill_order`, `test_cancel_fill_race_is_resolved_from_the_venue`, `test_protective_rejection_degrades_and_retries_boundedly`, `test_gap_through_stop_limit_leaves_position_unprotected_honestly` |
| Balance/position mismatch / unknown order / unexplained P&L | reconciliation FAILED; block new exposure | `test_at15_account_mismatch_blocks`, `test_at11_unknown_state_blocks_new_exposure` |
| Risk service unavailable / limit unknown / stale account / kill switch mid-workflow | UNKNOWN ⇒ FAIL | `test_broken_check_fails_closed`, `test_tampered_policy_fails_closed`, `test_at12_kill_switch`, `test_at01_mode_change_after_approval_blocks`, `test_resume_refused_until_a_reconciliation_after_the_stop_passes` |
| Worker unavailable / provider change / mandate expiry / config corruption | heartbeat age shown; restart; config validation fails early | `test_heartbeat_touches_the_healthcheck_file`, `test_watchdog_exits_only_when_the_loop_stalls`, `test_config.py` (11 tests), `test_openrouter_requires_fresh_zero_price_and_no_fallbacks` (mandates: not built, disabled) |

## 4. Go-live gates (V6 §1830) — current expectation

| Gate | Can pass in this build? (updated 2026-10-24, S34) |
|---|---|
| A — repository/environment assessment | Yes, on recorded evidence (VM verified 2026-10-19; B-02 resolved) |
| B — unit/property tests | Yes, on recorded evidence |
| C — integration/E2E with broker sandbox | No — BLOCKED until a live adapter exists (B-07); `/golive` lists the sandbox contract runs |
| D — adversarial safety tests | Yes, on recorded evidence |
| E — recovery/chaos | Yes, on recorded evidence (VM restart test passed 2026-10-20) |
| F — kill switch/permissions | No — BLOCKED until a live adapter exists (B-07) |
| G — paper/shadow period | No — BLOCKED: no live venue to compare simulated fills against (B-07) |
| H — operational readiness | Yes, on recorded evidence |
| I — explicit final activation | No — the owner's own activation; never recorded by a command, and not in this build |

## 5. Evidence rules

Only test runs actually executed are reported, with the command, date, counts and failures. A failing or skipped test is reported as such. Mocks and simulations are named as mocks and simulations.
