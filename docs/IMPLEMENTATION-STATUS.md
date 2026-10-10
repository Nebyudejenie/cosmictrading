# Implementation Status (living log)

Each entry records what was actually done and verified. Test counts come from real runs; anything not run is marked NOT_TESTED.

## Deployment state

| Layer | State |
|---|---|
| Local development | VERIFIED (S1: dev Postgres, migrations, web smoke test) |
| Local production-like stack | NOT VERIFIED (S9 pending) |
| Proxmox VM | BLOCKED (B-02) |
| Cloudflare Tunnel | NOT CONFIGURED (B-03) |
| DNS | BLOCKED (B-03) — no changes made |
| AI providers | NOT CONFIGURED (B-04) |
| Telegram bot | NOT CONFIGURED (B-05) |
| Broker / live trading | NOT CONFIGURED (B-06, B-07); live trading unreachable |

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
