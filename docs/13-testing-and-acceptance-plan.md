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

| Failure | Expected | Test |
|---|---|---|
| LLM unavailable / quota exhausted / malformed / contradictory output | AI tasks wait or fail as invalid; no proposal; safety unaffected | `test_at13_ai_outage_safety`, `test_ai_malformed_output` |
| Internet down / delayed data / invalid prices / schema break / source disagreement | fetch run fails, label STALE/UNAVAILABLE, rows rejected, R09/R10 block | connector + risk tests |
| Broker outage / rate limit / maintenance / auth expiry / ambiguous response | `BrokerUnavailableError` → no submission; ambiguous → UNKNOWN | trading sim tests |
| DB/queue unavailable; low RAM / CPU / full disk; restart during submission | readiness fails; background paused; startup sweep | reliability tests, AT-20 |
| Duplicate callbacks / expired / replayed approval / wrong owner / stale dashboard | rejected with 409; audited | approval + Telegram tests |
| Partial fill / unexpected fill / rejection / cancel-fill race / missing protective order | ledger correct; protection DEGRADED; alert | trading sim tests |
| Balance/position mismatch / unknown order / unexplained P&L | reconciliation FAILED; block new exposure | AT-15 |
| Risk service unavailable / limit unknown / stale account / kill switch mid-workflow | UNKNOWN ⇒ FAIL; FOR SHARE serialisation | risk + kill switch tests |
| Worker unavailable / provider change / mandate expiry / config corruption | heartbeat incident; config validation fails early | ops + config tests |

## 4. Go-live gates (V6 §1830) — current expectation

| Gate | Can pass in this build? |
|---|---|
| A — repository/environment assessment | Partially (VM inventory BLOCKED) |
| B — unit/property tests | Yes |
| C — integration/E2E with broker sandbox | No — no broker (B-06) |
| D — adversarial safety tests | Yes (paper) |
| E — recovery/chaos | Partially (VM restart BLOCKED) |
| F — kill switch/permissions | Yes for paper; broker key scopes BLOCKED |
| G — paper/shadow period | Paper period can run once deployed; live comparison BLOCKED |
| H — operational readiness | Partially (monitoring/backups local; VM BLOCKED) |
| I — explicit final activation | No — by design, requires owner action and all gates |

## 5. Evidence rules

Only test runs actually executed are reported, with the command, date, counts and failures. A failing or skipped test is reported as such. Mocks and simulations are named as mocks and simulations.
