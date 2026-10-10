# 06 — AI Provider and Agent Design

## 1. Policy

The only supported AI policy is `FREE_CLOUD_AI_ONLY` (AI-21). The monetary inference budget is **$0**. There is no paid route, no paid fallback, no local model and no Ollama. If eligibility of a route cannot be confirmed, the call is refused (fail closed). AI is never required for risk, approvals, order state, idempotency, reconciliation, secret handling, the kill switch or audit (AI-23); those packages cannot import `cosmic.ai`.

## 2. Provider review (2026-10-10)

| Provider | Endpoint (OpenAI-compatible) | Free basis | Verified facts | Default status |
|---|---|---|---|---|
| Groq | `https://api.groq.com/openai/v1` | Free plan (account without billing) | Rate-limit headers and `retry-after` documented | **Enabled after owner attestation** + model approval |
| Google Gemini API | `https://generativelanguage.googleapis.com/v1beta/openai` | Free tier (project without billing) | Official pages could not be fetched; third-party figures conflict | **Enabled after owner attestation** + model approval; quotas taken from the owner's AI Studio view |
| OpenRouter | `https://openrouter.ai/api/v1` | Models with `:free` suffix | 20 RPM; 50 RPD (<10 credits ever purchased) or 1,000 RPD; `/api/v1/models` exposes pricing | Enabled after model approval; **runtime pricing check** must return 0 for every price field |
| Cerebras | `https://api.cerebras.ai/v1` | Free Trial = $5 credits expiring after 30 days | Documented on inference-docs.cerebras.ai | **Not allowlisted** — a time-limited credit grant is not a standing free tier |

Privacy assumption for every provider: free-tier prompts and outputs may be retained, logged and reviewed by the provider and may be used to improve products. Therefore only data classified `PUBLIC` or `INTERNAL_NONSENSITIVE` is ever sent (§5).

## 3. Registry and approvals

- `config/ai_registry.toml` (in Git, reviewed): providers, base URLs, key environment variable names (never values), adapter type, default limits, documentation URLs, `requires_attestation`, `runtime_price_check`, allowed model-ID pattern (OpenRouter: must end with `:free`).
- `ai_provider_attestations` (DB): the owner attests, with step-up MFA, that the account is on a free plan without a payment method (or with a credit limit of 0), with the evidence URL; expires after 30 days.
- `ai_model_approvals` (DB): approved model IDs per provider with tier (`SMALL`, `LARGE`), context limit, evidence URL and review expiry (30 days). No model is pre-approved; model IDs change frequently and must be confirmed against the provider's live list (`cosmic ai models <provider>` lists them; the portal approval form requires the evidence URL).

A call is allowed only if **all** hold: provider in registry and enabled; key present; attestation valid (where required); model approved, unexpired, matching the pattern; runtime price check passed within 24 h (where required); circuit closed; local quota available; request data classification permitted.

## 4. Gateway behaviour

| Concern | Behaviour |
|---|---|
| Routing | Task type → tier (`classify`, `extract` → SMALL; `research_narrative`, `red_team` → LARGE). Candidates = approved models of that tier ordered by health and remaining quota. Fallback only among approved free models. Exact maths never goes to AI. |
| Timeouts / retries | 30 s timeout; at most 2 retries on connection errors/5xx with full-jitter backoff; on 429 honour `retry-after`/`x-ratelimit-reset-*` and mark the provider rate-limited until then (no immediate retry on the same provider). |
| Circuit breaker | Per provider: 3 consecutive failures → `OPEN` for 5 min → `HALF_OPEN` probe. |
| Billing signals | HTTP 402, an error mentioning credits/billing, or a reported `usage.cost > 0` → provider disabled immediately, `CRITICAL` incident, owner notified. |
| Quota | Local per-minute/per-day counters from registry/attestation values; requests beyond the budget are queued, not sent. Priority order: risk review > research narrative > background summarisation. |
| Usage ledger | `ai_usage` row per attempt (tokens, latency, status, reported cost). |
| Cache | Key = SHA-256 of (task type, schema version, normalised inputs). `expires_at` explicit; expired entries are never returned as current. |
| Output | JSON mode where supported; parsed into the task's Pydantic schema (`extra="forbid"`); one repair attempt on another approved model; otherwise the task fails as `AI_OUTPUT_INVALID`. Numeric claims are re-verified by code against evidence; unverifiable claims are stored as `INFERRED`. |
| Degraded mode | No eligible provider → tasks enter `WAITING_FOR_AI` with reason; status page shows `DEGRADED` or `NOT_CONFIGURED`. |

## 5. Prompt safety

- **Structure:** a fixed system policy + task instructions + a `DATA` section in which every untrusted item is wrapped in `<untrusted_data id="…">…</untrusted_data>` with an explicit statement that its content is data, never instructions.
- **No tools:** the gateway exposes no function-calling to models. Models return data; deterministic code decides.
- **Outbound scanner:** before transmission the full request body is scanned for configured secret values (all `*_KEY`, `*_TOKEN`, `*_SECRET`, DB DSN), common key patterns (`sk-…`, `gsk_…`, `AIza…`, Telegram bot tokens, PEM blocks), account identifiers, e-mail addresses and session tokens. A hit blocks the call and opens an incident. Tested with every configured secret (AT-16).
- **Classification:** each request declares `PUBLIC` (market data, public documents) or `INTERNAL_NONSENSITIVE` (aggregate research state). `CONFIDENTIAL` (account balances, positions, identities) is never permitted.

## 6. Workforce

Roles are declarative (`workforce/roles.py`); each declares mission, out-of-scope, inputs, output schema, allowed tools, data classification, capabilities, SLA, retry policy, AI budget, evidence requirements, quality gates, reviewer, metrics, failure modes and retirement condition.

| Role | Kind | Allowed tools | AI use |
|---|---|---|---|
| `orchestrator` | deterministic | create/assign tasks, read mission state | none |
| `source_data_analyst` | deterministic | read bars/quotes/fetch runs, freshness, cross-source check | none |
| `quant_analyst` | deterministic | indicators, regime, backtest, sizing helper | none |
| `market_researcher` | AI-assisted | read evidence packet (references), AI gateway (`research_narrative`, `red_team`) | LARGE, ≤ 2 calls/task |
| `risk_reviewer` | deterministic | `trading.proposals.create_candidate` (propose only), read-only risk evaluation | none |
| `task_reviewer` | deterministic (+ optional AI critique, off by default) | acceptance-criteria checks | none by default |
| `ops_security_monitor` | deterministic | health, quotas, incidents, audit-chain verification | none |

No role has `SUBMIT_ORDER`, broker access, shell access or database write access beyond its task outputs. Task engine limits: delegation depth ≤ 3, attempts ≤ 3, per-task AI budget, per-mission AI budget, task expiry, idempotency keys on mission and task creation.

## 7. Research mission (first end-to-end flow)

1. Detection — `market_events` (deterministic detector) → mission (idempotent per event).
2. `source_data_analyst` — freshness + cross-source evidence packet.
3. `quant_analyst` — indicators, regime, strategy signal, recent backtest, proposed sizing.
4. `market_researcher` — AI narrative (thesis, counterarguments, scenarios, red team) over evidence IDs; claims verified.
5. `risk_reviewer` — `create_candidate()` → `PROPOSED` (approval request) or `NON_EXECUTABLE`.
6. `task_reviewer` — acceptance checks for each task; failures reopen with reason.
7. Report artefact (what changed / matters / verified / inferred / contradicts / unknown / timestamps / freshness / why) → mission `COMPLETED` → archived.

If step 4 cannot run (no eligible AI), the mission waits in `WAITING_FOR_AI` until its expiry, then ends as `EXPIRED` with no proposal (AI-22).
