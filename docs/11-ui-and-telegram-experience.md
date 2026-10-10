# 11 — UI and Telegram Experience

## 1. Principles

Decisions over decoration. Plain language first, details one click away. Every market value shows its timestamp and freshness label. Every simulated value shows `SIMULATED`. A blocked item is shown as blocked with its reason, never hidden behind a green aggregate. Application health and trading-account health are separate panels. Every control performs a real action and shows loading (form disabled on submit), success, failure and disabled states; there are no decorative controls or invented metrics.

Implementation: server-rendered HTML (Jinja2), one CSS file, no JavaScript framework, no CDN assets, strict CSP. Charts are server-generated inline SVG from stored bars (no client library). Semantic HTML (`<main>`, `<nav>`, `<table>` with `<th scope>`), labelled form fields, visible focus styles, skip link, colour never the only signal (labels carry text), responsive single-column layout below 760 px, works with keyboard only.

## 2. Views (priority order, §11 / V6 §1824)

| View | Content |
|---|---|
| **Command center** `/` | Mode banner (mode, "Live order submission: DISABLED — no live broker configured"); what changed (recent market events, source status changes); what matters now (proposals awaiting approval with expiry countdown text, open incidents); verified vs inferred (latest mission report claim counts by status); contradictions; risks/incidents; work running/blocked/overdue (task counts); system health (DB, worker heartbeat, AI status, source freshness) separate from account health (reconciliation status, unknown orders, protection state) |
| Sources `/sources` | Registry with label, last success, last error, freshness limit, terms status; fetch-run history; NOT_CONFIGURED sources listed (coverage gaps) |
| Market `/market` | Watchlist with last price, bid/ask, spread, label, age; instrument page with SVG chart (complete bars), indicators, regime, cross-source deviation, instrument rules with provenance |
| Research `/research`, `/tasks` | Missions, task board by status, artefacts, reviews, reports |
| Strategies `/strategies` | Versions, backtests, walk-forward results with assumptions and disclaimer |
| Portfolio & risk `/portfolio` | Paper balances/positions (SIMULATED), exposure, P&L (realised/unrealised, estimated vs actual), drawdown, limit proximity bars with numbers |
| Approvals `/approvals`, `/approvals/{id}` | Queue; packet in plain language → detail tabs (evidence, counterarguments, risk checks, portfolio impact, costs, management plan, technical); confirm screen with exact terms and MFA field |
| Orders `/orders`, `/reconciliation`, `/audit` | Lifecycle with broker-confirmed vs local states, fills, reconciliation runs and items, audit search |
| AI providers `/ai` | Per provider: status, attestation and review dates, approved models, last success, rate-limit state, breaker, last error — never keys |
| Operations `/ops` | Kill switch (prominent), protection-only, recovery checklist, mode change form, go-live readiness, workers, jobs, incidents, resources |
| Settings | Risk policy (view + change with MFA + reason), Telegram linking, MFA |

## 3. Telegram (bot process; V6 §1823)

- Private chats only; group/channel updates are ignored. The sender's immutable `from.id` must equal a linked user's `telegram_user_id`; otherwise the bot replies with nothing actionable.
- Linking: portal generates a one-time code (10 min) → `/link <code>` in a private chat.
- Commands: `/status`, `/approvals`, `/stop <reason>` (kill switch, with a confirm button), `/help`.
- Proposal message: plain summary — action, instrument, quantity, limit, account alias, key risk, expiry, status `PROPOSED` — with buttons `REVIEW DETAILS`, `EVIDENCE`, `COUNTERARGUMENTS`, `RISK`, `PORTFOLIO IMPACT`, `APPROVE EXACT TRADE`, `REJECT`, `REQUEST CHANGES`.
- `APPROVE EXACT TRADE` → confirmation message restating exact quantity, instrument, side, account alias, order type, price constraint and permitted exits, plus `CONFIRM APPROVAL` (callback carries an opaque challenge reference) → if TOTP-in-chat is enabled (default), the bot asks for the 6-digit code → server-side approval. Any free text such as "ok" is ignored for approval purposes.
- Status messages use explicit labels: `PROPOSED`, `APPROVED`, `SUBMITTED`, `ACKNOWLEDGED`, `PARTIALLY FILLED`, `FILLED`, `REJECTED`, `CANCELLED`, `UNKNOWN`, plus `SIMULATED` in paper mode.
- No secrets, keys, full account numbers or e-mail addresses are ever sent; account aliases only.
- If Telegram is down the portal remains a complete route; approvals are never inferred from another channel.

The Telegram Mini App is deferred (ADR-021): the portal covers the same functions with less attack surface while the first release is built.
