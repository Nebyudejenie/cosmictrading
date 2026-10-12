# 10 — API and Integration Contracts

All endpoints are served by `web`. Authentication is the portal session cookie; unsafe methods require the CSRF token (`X-CSRF-Token` header for JSON, hidden field for forms) and a same-origin `Origin`. Errors return `{"error": {"code": "...", "message": "..."}}` with no stack traces. Times are ISO-8601 UTC; decimals are strings.

## 1. Health and internal

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | none | Liveness: process responds (`{"status":"ok"}`) |
| GET | `/readyz` | none | Readiness: DB reachable and migrations at head; 503 otherwise. Reveals no versions or hosts. |
| GET | `/internal/metrics` | bearer token from secret file | Prometheus text format; blocked at the tunnel |

## 2. JSON API (`/api/v1`)

| Method | Path | Capability | Notes | Built |
|---|---|---|---|---|
| GET | `/status` | VIEW | Mode, live-submission state, worker heartbeat age, AI status, source freshness summary, open incidents count | yes (S5) |
| GET | `/sources` | READ_MARKET_DATA | Registry with last success, freshness label | yes (S5) |
| GET | `/instruments` | READ_MARKET_DATA | Instrument master with provenance | yes (S5) |
| GET | `/market/{instrument}/bars?timeframe=1h&limit=200` | READ_MARKET_DATA | Completed bars only (oldest first, limit ≤ 720) + latest quote; labelled DELAYED | yes (S11) |
| GET | `/intents?status=` | READ_ACCOUNT | Proposals with status and expiry | yes (S5) |
| GET | `/intents/{id}` | READ_ACCOUNT | Intent, latest packet, risk check runs, approvals, orders | yes (S5) |
| POST | `/intents/{id}/challenge` | APPROVE_TRADE | Issue confirmation challenge for the latest packet → `{challenge, packet_hash, terms, expires_at}` | yes (S5) |
| POST | `/intents/{id}/decision` | APPROVE_TRADE (+ step-up MFA) | Body `{challenge, packet_hash, decision: APPROVE|REJECT|REQUEST_CHANGES, note?}` | yes (S5) |
| POST | `/approvals/{id}/revoke` | APPROVE_TRADE | Revoke an unconsumed approval | yes (S11) |
| GET | `/orders?status=` / `/orders/{id}` | READ_ACCOUNT | Order with events and fills; each value tagged `SIMULATED` or broker-confirmed | yes (S5) |
| GET | `/accounts/{alias}` | READ_ACCOUNT | Balances, positions, reconciliation status (internal vs broker separated) | yes (S5) |
| POST | `/accounts/{alias}/reconcile` | RECONCILE | Enqueue reconciliation | yes (S5) |
| POST | `/authority/emergency-stop` | ACTIVATE_EMERGENCY_STOP | Body `{reason}` | yes (S5) |
| POST | `/authority/mode` | more permissive: ADMINISTER_SYSTEM + fresh MFA; more restrictive: ADMINISTER_SYSTEM or ACTIVATE_EMERGENCY_STOP, no fresh MFA | Body `{mode, reason}`; unmet preconditions → 409; EMERGENCY_STOP only via `/authority/emergency-stop`, left only via the recovery checklist | yes (S11) |
| GET | `/golive/readiness` | VIEW | Gate list with status and evidence | yes (S11) |
| GET | `/ai/providers` | VIEW | Provider health (never keys) | yes (S11) |
| GET | `/missions`, `/missions/{id}` | VIEW | Mission list with task board; mission with tasks, reviews, claims, opportunity and report | yes (S11) |
| GET | `/audit?entity_type=&entity_id=` | ADMINISTER_SYSTEM or READ_ACCOUNT | Audit events (payloads already redacted) | yes (S11) |
| GET | `/search?q=` | VIEW (proposals only with READ_ACCOUNT) | Full-text search over claims (with status), mission reports and proposals; `websearch_to_tsquery` syntax | yes (S11) |

Status codes: 401 unauthenticated, 403 capability or MFA missing (`code: "step_up_required"`), 404 unknown/forbidden object, 409 state conflict (expired, consumed, superseded, mode precondition), 422 validation, 429 rate limit.

## 3. Portal routes

Server-rendered equivalents under `/` (`/login`, `/mfa`, `/`, `/sources`, `/market`, `/research`, `/tasks`, `/strategies`, `/portfolio`, `/approvals`, `/approvals/{id}`, `/orders`, `/reconciliation`, `/audit`, `/ai`, `/ops`, `/settings/risk`, `/settings/telegram`). Forms post to the same service functions as the JSON API.

## 4. Internal service contracts

| Contract | Location | Consumers |
|---|---|---|
| `SourceConnector` protocol (`fetch_quotes`, `fetch_bars`, `fetch_instruments`, `fetch_status`) | `sources/contract.py` | ingestion jobs |
| `Candidate` schema + `create_candidate()` | `trading/proposals` | workforce `risk_reviewer`, operator manual proposal form |
| `BrokerAdapter` protocol | `trading/brokers/base.py` | execution authority, reconciliation |
| `AIGateway.run(task)` | `ai/gateway.py` | workforce |
| `audit.record()` | `audit/service.py` | all modules |
| `notify.enqueue()` | `notify/outbox.py` | trading, ops, workforce |

## 5. External integrations

| Integration | Direction | Auth | Status |
|---|---|---|---|
| Coinbase Exchange public REST | outbound | none | Implemented in S2 (fixtures + live smoke) |
| Bitstamp public REST | outbound | none | Implemented in S2 |
| Coinbase status API | outbound | none | Implemented in S2 |
| Groq / Gemini / OpenRouter | outbound | API key (secret file) | Adapter implemented; live use BLOCKED (B-04) |
| Telegram Bot API | outbound long polling | bot token | Handlers implemented; live use BLOCKED (B-05) |
| Broker/exchange trading API | outbound | least-privilege key | NOT CONFIGURED (B-06/B-07) |
| Cloudflare Tunnel | outbound | tunnel token | Template only; BLOCKED (B-03) |
