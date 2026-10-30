# 08 — Trading Approval and Execution

This document specifies the `COSMICTRADING_APPROVED_AUTONOMOUS_TRADING_ENGINE` (V6 §1804). Everything here is deterministic code in `cosmic.trading`, which may not import `cosmic.ai` or `cosmic.workforce`.

## 1. Authority

### 1.1 Modes (persisted in `authority_state`)

| Mode | New exposure | Paper orders | Live orders | Notes |
|---|---|---|---|---|
| `RESEARCH_ONLY` | no | no | no | Default on first start |
| `PAPER_TRADING` | paper only, per-trade approval | yes | no | The full approval workflow runs against `PaperBroker` |
| `APPROVAL_REQUIRED` | live, per-trade approval | yes | yes (after gates) | **Unreachable in this build**: no live adapter exists, and every lock of ADR-034 must be opened by the owner |
| `STRATEGY_AUTHORIZED` | within a mandate | — | — | Disabled; mandate execution not built (TRD-23) |
| `PROTECTION_ONLY` | no | protective only | protective only | Entered on reconciliation failure, config cap breach, or by operator |
| `EMERGENCY_STOP` | no | no new | no new | Kill switch; resume via recovery checklist |

`APPROVED_EXECUTION` is the authority recorded on an order created from a valid, consumed approval; it is not a global mode (ADR-010).

### 1.2 Transition rules

- More restrictive transitions (towards `EMERGENCY_STOP`) are always allowed for a user with the needed capability.
- More permissive transitions require `ADMINISTER_SYSTEM`, step-up MFA, a written reason and mode-specific preconditions. `APPROVAL_REQUIRED` and `STRATEGY_AUTHORIZED` additionally require: a registered live adapter, a venue named in `COSMIC_LIVE_VENUES`, every go-live gate `PASSED` with evidence, and the owner's active live activation (Gate I, ADR-034). Each refused transition returns the list of unmet preconditions.
- A live mode records the activation it rests on (`authority_state.live_activation_id`). When the last active activation ends (the owner's revoke, its expiry, the emergency stop or a failed key check), the system restricts the mode to `PAPER_TRADING`.
- `COSMIC_MAX_AUTHORITY_MODE` (environment) can only lower authority. Startup with a stored mode above the cap forces `PROTECTION_ONLY` and opens an incident. A configuration change therefore never raises authority silently (TRD-04).
- Every transition writes `authority_transitions` and an audit event in the same transaction.

### 1.3 Capability separation

See doc 05 §2.1. The research worker identity holds `RESEARCH`, `READ_MARKET_DATA`, `PROPOSE_TRADE`; only the execution authority identity holds `SUBMIT_ORDER`/`CANCEL_ORDER`/`MANAGE_APPROVED_POSITION`; only a human with step-up MFA holds `APPROVE_TRADE`.

## 2. Candidate trades and proposals

A candidate (`trading.proposals.Candidate`, Pydantic, `extra="forbid"`) is the only object research can hand to trading. Required fields mirror V6 §1805: account alias, canonical instrument key, side, effect (`OPEN`/`INCREASE`/`REDUCE`/`CLOSE`), quantity, order type, limit price, price band, max slippage, time in force, expiry, horizon, thesis, plain summary, evidence references (with source timestamps), counterarguments, red-team view, scenarios, invalidation conditions, exit plan (management plan), reasons for `WAIT`/`NO_TRADE`.

`create_candidate()` validates the schema, resolves the instrument by canonical key (ambiguous or unknown → `NON_EXECUTABLE`), applies precision rules (§5), runs the pre-approval risk checks (§3), and stores an immutable `trade_intent` plus packet version 1 when all checks pass. A failed check stores the intent as `NON_EXECUTABLE` with reasons and **no approval request is sent** (TRD-08, TRD-10).

### 2.1 Intent status

`PROPOSED` → (`APPROVED` | `REJECTED` | `CHANGES_REQUESTED` | `EXPIRED` | `SUPERSEDED` | `REVOKED`) → `EXECUTING` → `EXECUTED` / `EXECUTION_FAILED` / `BLOCKED`; plus `NON_EXECUTABLE` and `REQUIRES_REAPPROVAL`. Material terms are immutable; any change produces a new packet version.

## 3. Deterministic risk engine

`trading.risk.evaluate(context, policy) -> RiskDecision`. Pure function over an immutable `RiskContext` snapshot built inside the calling transaction. Each check returns `PASS`, `FAIL` or `UNKNOWN` with a plain-language message and technical detail. **For exposure-increasing operations `UNKNOWN` counts as `FAIL`.** Reduce-only protective operations evaluate a restricted subset (mode, account integrity, reduce-only quantity bound).

| # | Check (V6 §1806/§1815/§9) | Fails when |
|---|---|---|
| R01 | Mode permits operation | mode is `RESEARCH_ONLY`, `PROTECTION_ONLY` or `EMERGENCY_STOP` for new exposure; live order in non-live mode |
| R02 | Account identity/config | account missing, disabled, kind mismatch (paper vs live), alias mismatch with packet |
| R03 | Account integrity | `reconciliation_status ≠ OK`, last reconciliation older than policy max, any `UNKNOWN_ORDER_STATE` order on the account, cash/positions unknown |
| R04 | Instrument permissions | instrument not allowlisted, venue not allowlisted, currency not allowed, order type not allowed, short/leverage not allowed |
| R05 | Order notional | notional (qty × limit) > `max_order_notional` |
| R06 | Position/concentration | post-trade position notional > per-instrument cap; concentration > `max_concentration_pct` of equity |
| R07 | Gross/net exposure, buying power | post-trade gross > cap; cash insufficient for notional + fees (no leverage) |
| R08 | Loss limits | daily realised+unrealised loss ≥ limit; drawdown from high-water mark ≥ limit; loss streak ≥ limit |
| R09 | Price validity/freshness | no quote; quote older than `max_price_age_seconds`; data label not acceptable (`STALE`, `UNKNOWN`, `UNAVAILABLE`); delayed data not permitted by policy |
| R10 | Market conditions | spread > `max_spread_bps`; limit price outside band; reference price outside approved band; cross-source deviation > limit; venue status not operational; instrument halted/trading disabled |
| R11 | Costs and slippage | estimated fees + slippage exceed `max_cost_bps`; slippage tolerance above policy |
| R12 | Duplicates/conflicts | open order or active intent on same instrument/side; conflicting reduce/increase instructions |
| R13 | Event/blackout windows | inside a configured blackout window |
| R14 | Authorization validity | (pre-submission) approval not active, expired, bound to a different packet hash, policy version changed, operator lacks capability |
| R15 | Security/incident state | open incident of severity `CRITICAL` in categories `SECURITY` or `ACCOUNTING` |
| R16 | Precision/rounding | rounding to lot/tick changes quantity or notional beyond tolerance; below minimum size/notional |
| R17 | Reduce-only bound (sells, protection, exits only) | a reduce order, plus pending reduce orders, exceeds the held quantity (protective orders an approved exit replaces are not counted); anything but a SELL |
| R18 | Concurrent positions (added 2026-10-16) | instruments held (at least one lot), with a resting buy, or bought by this trade exceed `max_open_positions` (default 3) |
| R19 | Total open risk (added 2026-10-16) | the loss if every stop is hit exceeds `max_open_risk_pct` of equity (default 3 %). Stopped quantity counts down to its stop-limit; unstopped holdings, and resting entries or this trade without a stop, count at full value |
| R20 | Real-money activation (added 2026-10-30, ADR-034) | on a LIVE account: no active live activation of that account, or holdings at their mark plus resting entries plus this order above the activation's cap (UNKNOWN without a fresh mark). PAPER and SANDBOX accounts pass ("not real money") |

Every evaluation is stored in `risk_check_runs` (stage `PRE_APPROVAL` or `PRE_SUBMISSION`, policy version, results, context snapshot), making AT-18 ("why was this blocked/permitted") answerable from data.

### 3.1 Risk policy

Versioned JSON (`risk_policies`). Each limit declares measurement source, currency, timezone/session boundary (UTC day for crypto), reset behaviour, fee treatment, unrealised P&L treatment and the action when data is unavailable (always `BLOCK` for new exposure). Changes require `CHANGE_RISK_LIMITS` + step-up MFA + reason; the new version becomes active for **new** evaluations. Because approvals bind the policy version, a version change invalidates outstanding approvals (`REQUIRES_REAPPROVAL`) — a change can never make an already-blocked trade pass (TRD-32).

Default policy v1 (paper): limit orders only; no shorting; no leverage; allowlist `SPOT:BTC/USD@COINBASE`, `SPOT:ETH/USD@COINBASE`; `max_order_notional` 500 USD; per-instrument 1,000 USD; gross 2,000 USD; concentration 50 %; daily loss 100 USD; drawdown 10 %; loss streak 4; price age 120 s; spread 25 bps; cross-source deviation 50 bps; slippage 20 bps; approval TTL 15 min; intent TTL 60 min; reconciliation max age 15 min; `allow_delayed_data: true` **for paper only** (live requires explicit operator acceptance, TRD-39). These are conservative defaults for simulation, not recommendations for live capital.

## 4. Approval

### 4.1 Packet

`approval_packets.packet` is canonical JSON (sorted keys, Decimal as string, UTC ISO timestamps) of every material term: trade code, intent ID, account alias and kind, mode, instrument canonical key, venue, side, effect, quantity, order type, limit price, price band, max slippage, TIF, max notional, estimated fees/slippage/spread with their basis (`ASSUMPTION` where unconfirmed), current holdings and post-trade impact, max configured risk with assumptions, the loss warning text, thesis, evidence references with timestamps, counterarguments, scenarios, red-team view, entry conditions, management plan (each permitted action with parameters), prohibited actions, risk check results, policy version, packet creation time, approval expiry, intent expiry. `packet_hash = sha256(canonical_json)`.

A packet cannot be created if a mandatory field is missing or a referenced quote is stale (TRD-14).

### 4.2 Two-step confirmation

1. **Review** — the operator opens the packet (portal or Telegram). The server issues an `approval_challenge` (random 256-bit token, stored hashed, 5-minute expiry) bound to `(intent, packet, packet_hash, user, channel)`. The confirmation screen shows the exact quantity, instrument, side, account alias, order type, price constraint and permitted exits.
2. **Confirm** — the operator submits the challenge with the decision. Server-side checks: session valid; user has `APPROVE_TRADE`; step-up MFA within 10 minutes (portal) or TOTP entered in chat (Telegram, configurable, default on); challenge unused, unexpired, bound to this user/channel; packet is the latest version and its hash matches; intent status `PROPOSED`; intent unexpired; policy version unchanged. On success one `approvals` row is inserted with a fresh nonce and `expires_at = min(now + approval TTL, intent expiry)`.

Silence, timeouts, free-text replies, a button from an old message, or any other channel's message never create an approval (TRD-16, TG-07). Telegram callback data carries only an opaque challenge reference.

### 4.3 Rejection, changes, revocation, expiry

`REJECTED` and `CHANGES_REQUESTED` are terminal for the packet. The operator may revoke an unconsumed approval. A sweep job expires approvals and intents. Kill switch revokes all unconsumed approvals.

## 5. Precision

`trading.precision.normalise(instrument, qty, price)` rounds quantity **down** to `lot_size` and price to `tick_size` (down for buys, up for sells — conservative). If the rounded quantity differs from the requested quantity by more than `rounding_tolerance_bps` (default 10 bps), or falls below `min_qty`/`min_notional`, the candidate is rejected for re-proposal; execution never re-rounds an approved quantity (TRD-39).

## 6. Execution authority

One leader (`pg_try_advisory_lock(<execution key>)`) in the worker runs the execution loop. For each `APPROVED` intent:

1. **Tx A — consume and authorise.** `SELECT … FROM authority_state FOR SHARE`; lock the intent row; consume the approval atomically; build `RiskContext`; run `PRE_SUBMISSION` checks; verify the current reference price lies inside the approved band and is fresh; verify packet hash and policy version. On failure: approval → `SUPERSEDED`/`EXPIRED`, intent → `REQUIRES_REAPPROVAL` or `BLOCKED`, audit, notify, commit, stop. On success: insert order `CREATED → VALIDATED → AUTHORIZED` (three events) with `client_order_id` derived from the intent; commit.
2. **Tx B — submit marker.** Re-read `authority_state FOR SHARE`; if no longer permitted → order `REJECTED` (local, never sent); else order → `SUBMITTING`; commit.
3. **Transmit.** Rebuild the broker request from the order row only (never from AI output or client input). Call `adapter.submit_order()` with a timeout.
4. **Tx C — record response.** Transport returned → `SUBMITTED`, then `ACKNOWLEDGED` or `REJECTED` per broker response (raw response stored, sanitised). Timeout/connection error/5xx/ambiguous → `UNKNOWN_ORDER_STATE`, account `reconciliation_status = REQUIRED`, reconciliation job enqueued, incident opened.
5. **Monitor.** A job polls `get_order(client_order_id)` and `get_fills()` at the adapter's documented cadence; new fills are inserted idempotently and posted to the ledger in the same transaction; order status follows broker state.
6. **Approved management.** After a (partial) fill, the management plan is applied: protective stop and take-profit orders sized to the **broker-confirmed filled quantity**, marked `reduce_only`, each as its own order with events. Their combined open quantity never exceeds the position (overfill guard; no atomic OCO is claimed). When one exit fills, the sibling is cancelled (cancel request ≠ cancellation; confirmed by broker). Unfilled entry remainder is cancelled at `cancel_unfilled_after_seconds` if the plan says so.
7. **Notify** the operator of actual, broker-confirmed state only.
8. **Review** — when the position returns to zero or the intent reaches a terminal state, a post-trade review task is queued.

### 6.1 Startup recovery

Before the execution loop starts, a sweep moves every order in `AUTHORIZED` (not yet marked), `SUBMITTING`, or `SUBMITTED` without acknowledgement older than the timeout into `UNKNOWN_ORDER_STATE` (for `AUTHORIZED` the sweep first queries the broker; if the client order ID is unknown to the broker and the order was never marked `SUBMITTING`, it is safe to mark `REJECTED` locally). Affected accounts become `REQUIRED`; reconciliation runs before any new exposure is possible (AT-20).

### 6.2 Race defences (V6 §1813)

| Race | Defence |
|---|---|
| Two approvals for one trade | partial unique index (one `APPROVED` per packet); intent status check under row lock |
| Repeated Telegram presses | challenge single-use (`used_at` set with `WHERE used_at IS NULL`) |
| Retry after timeout | no automatic resubmission; `UNKNOWN_ORDER_STATE` → reconciliation by client order ID |
| Cancel/fill simultaneously | cancel moves to `CANCEL_PENDING`; final state only from broker; fills always recorded |
| Cancel request lost (venue unreachable or no clear answer) | while the order stays `CANCEL_PENDING`, the cancel is sent again: a minute apart for the first five sends, then every 5 minutes (`follow_up_cancels`). After the fifth send a `trading.cancel_unconfirmed:<order>` incident (CRITICAL for an entry, WARNING otherwise) tells the owner; it closes once the venue reports the order finished |
| Venue no longer has an order it acknowledged (for example a test-network reset) | after a minute of "no such order" the order is ended on reconciliation evidence: `CANCELLED` if a cancel was pending, `EXPIRED` otherwise. A `trading.order_missing_at_venue:<order>` incident (WARNING) is raised, a protective order is replaced under its plan, and fills are never invented |
| Venue answer read just before a concurrent change (a cancel, another sync) | the answer is dropped when the order's `status_version` changed meanwhile; the next sync reads the venue again |
| One failing step, order or position (a corrupt policy, one account's missing adapter) | each step of the execution cycle, and each order, exit or position within it, runs even when another failed; the first failure is raised at the end. A step failing in three leader cycles in a row raises `trading.cycle_step_failing:<step>` (CRITICAL), closed when the step succeeds again, because job health can miss it (a run that is not the leader returns normally) |
| An account whose adapter this process lacks (for example the worker without the test-network keys) | its positions are not managed here, and a cancel for it fails before anything is recorded |
| HFM: one stop-loss per position | one stop order per entry: when more of the entry fills, the new stop covers the whole holding and replaces the older ones in our books, sending nothing; an older stop's unconfirmed cancel is not sent again while another stop of the trade holds the stop-loss (ADR-028 addendum) |
| An order being sent while reconciliation runs (the stop reconciles at once) | an order created or sent less than a minute ago is "being sent now": the run does not pass and the account stays REQUIRED, but no disagreement alert is raised; older ones keep the CRITICAL path |
| An order settled while reconciliation runs | an order the venue listed as open but our record shows final is asked about again; only a venue that still reports it open, or cannot be asked again, is a mismatch |
| Partial fill while cancel pending | `CANCEL_PENDING → PARTIALLY_FILLED/CANCELLED` both allowed; exits sized from confirmed fills |
| Broker reconnect after unknown state | reconciliation imports broker-confirmed fills/status with source `RECONCILE` |
| Stale worker acting on old state | single leader lock; optimistic `status_version` on orders; authority row read under lock |
| Mode change mid-order | `FOR SHARE` on `authority_state` in Tx A and Tx B |

## 7. Broker adapter contract

`trading.brokers.base.BrokerAdapter` (Protocol):

| Method | Semantics |
|---|---|
| `capabilities()` | order types, TIFs, idempotent client IDs (bool), bracket/OCO support, partial fills, rate limits, data freshness, precision source, sandbox/live, venue status endpoint |
| `submit_order(req)` | returns `SubmitResult` (`ACCEPTED` with venue order ID / `REJECTED` with reason) or raises `BrokerAmbiguousError` / `BrokerUnavailableError` |
| `get_order(client_order_id)` | authoritative order state, cumulative filled quantity, average price; `NotFound` is explicit |
| `cancel_order(client_order_id)` | returns `CANCEL_REQUESTED`; never implies cancelled |
| `list_open_orders()`, `get_fills(since)`, `get_balances()`, `get_positions()` | reconciliation sources |
| `venue_status()` | operational / degraded / maintenance / unknown |

Registered adapters in this build: `paper` only. A `contract test suite` (`tests/trading/test_broker_contract.py`) is parametrised over adapters; a future live adapter must pass it against the venue sandbox before it can be registered (TRD-28).

### 7.1 PaperBroker (simulated venue)

- Books in `paper_*` tables, independent of the internal ledger.
- Matching on each `tick()` using the latest ingested quote: a buy limit fills when `ask ≤ limit` at `min(limit, ask × (1 + slippage_bps))`; sell symmetric. Fill quantity per tick is capped by `max_fill_fraction` (partial fills). Fees = `taker_fee_bps` (configuration, labelled `ASSUMPTION`, B-08). No quote or a stale quote → no fills ("cannot simulate", not "assume filled").
- Fault injection (`TEST`/`LOCAL` environments only; refused by config validation elsewhere): reject next order, accept-but-lose-response (timeout after the venue recorded the order), disconnect, delayed acknowledgement, duplicate fill reports.
- Every row, API response and UI value is labelled `SIMULATED`.

## 8. Reconciliation

Order: `BROKER_ORDERS → FILLS → POSITIONS → CASH/BALANCES → INTERNAL_LEDGER → ACCOUNT_STATE` (V6 §1818). For each step, items are `MATCH`, `MISMATCH`, `IMPORTED` (broker-confirmed fill or status adopted, with audit) or `RESOLVED`. The run `PASSED` only when no `MISMATCH` remains; then `accounts.reconciliation_status = OK`. Otherwise `FAILED`: account blocked for new exposure, global mode moved to `PROTECTION_ONLY` if the account is live (paper: account-level block only), incident + alert. Internal records are never overwritten with guessed values; only broker-confirmed facts are imported.

Triggers: schedule (every 5 minutes), after `UNKNOWN_ORDER_STATE`, after restart, before resuming from `EMERGENCY_STOP`/`PROTECTION_ONLY`, and on demand (`RECONCILE` capability).

## 9. Kill switch

`killswitch.activate(actor, reason)`:

1. In one transaction: `authority_state.mode = EMERGENCY_STOP` (remember `mode_before_stop`), end every live activation (ADR-034: recovery never resumes real money by itself), revoke all `ACTIVE` approvals, mark `APPROVED`/`PROPOSED` intents `BLOCKED`, open a recovery checklist, audit.
2. Apply the configured emergency policy: default `cancel_unfilled_entries: true` (unfilled entry orders would add exposure), `cancel_protective_orders: false`, `liquidate_positions: false`.
   - An entry already on its way (`SUBMITTING`, `SUBMITTED` or `UNKNOWN_ORDER_STATE`) cannot be cancelled before the venue acknowledges it. While the stop is active, every execution cycle cancels any entry open at a venue (`cancel_entries_under_stop`), right after the steps that record venue answers and place protective orders. Such an entry is cancelled in the cycle that sees it acknowledged; whatever filled before that stays a position, protected by its plan. This step runs even when an earlier step of the cycle failed. The stop's alert says how many entries were in flight, or, under a policy that keeps unfilled entries, that they stay open.
3. Enqueue reconciliation for every account.
4. Notify on every configured channel; record delivery failures.

Resume (`killswitch.resume`) requires: step-up MFA, all checklist items complete (reconciliation `PASSED` after the stop for every account, no `UNKNOWN_ORDER_STATE`, incidents acknowledged, operator confirmation of positions reviewed, reason), and a target mode not more permissive than `mode_before_stop`. The UI states plainly that stopping new entries does not close existing positions.

## 10. Protection failures (V6 §1822)

If an approved protective order is rejected, cancelled unexpectedly or cannot be verified: position `protection_state = DEGRADED`/`UNKNOWN`; new risk on that instrument is blocked; urgent notification; at most `protective_retry_max` (default 2) deterministic re-submissions of the **identical** order parameters with fresh client IDs, only after the broker confirms no live protective order exists; never a wider stop. Then escalate to the owner.

## 11. Go-live readiness

`trading.golive.readiness()` evaluates the §9 live gates and V6 §1830 gates A–I from recorded evidence and returns each with `PASSED` / `NOT_MET` / `BLOCKED`, the reason and what passes it. In this build gates C and F cannot pass while no live adapter exists (B-07). Gate G needs its paper period and the paper fills compared with Binance's real order book (S41, `trading/real_book.py`): at least 20, typically neither more than 5 bps kinder nor more than 25 bps harsher than the real book. Gate I is the owner's own live activation (`trading/live.py`, ADR-034): never recorded by a command, it passes only while an activation is active and A–H pass, and stays BLOCKED without a live adapter. A, B, D, E and H pass on recorded evidence. `authority.live_preconditions` requires every gate PASSED as `readiness()` evaluates it (S34), so `APPROVAL_REQUIRED` is unreachable by construction.

### 11.1 The real-money locks (ADR-034)

Real money needs seven locks open, each opened only by the owner: `LIVE_ADAPTERS` (a reviewed code constant, empty), `COSMIC_MAX_AUTHORITY_MODE` and `COSMIC_LIVE_VENUES` (the server's env file), gates A–H, a passing key check under 24 hours old (`live_key_checks`: spot trading and reading only, IP-restricted, every other permission off), the owner's live activation of one LIVE account (a cap of at most 1,000, at most 90 days, a reason, a fresh code, portal only), and each trade approved with a code. The `/golive` page lists each lock with its state now.

The database holds the last line whatever the application does (migration 0016):

- `orders_live_guard`: an order on a LIVE account needs that account's activation, active at the later of the order's time and the database's clock, with the order's value within the cap. Only a reduce-only SELL exit or stop goes without one. A real-money order's account, purpose, side, size and price never change.
- `trading_mandates_guard`: a mandate on a LIVE account must be ASK, under an active activation, with its money within the cap and its expiry no later than the activation's. `approvals_mandate_guard` (no MANDATE approval on a LIVE account) is unchanged.
- `live_activations` only for a LIVE account, by an active owner, for at most 90 days; immutable, with revocations in their own append-only table.
- A LIVE account only with the adapter `binance_spot_live`.

## 12. Invariants → tests

Test names as they exist in `tests/` (verified 2026-10-11).

| V6 §1834 invariant | Test |
|---|---|
| A proposal is not an approval | `test_at03_no_submission_without_approval` |
| Approval scoped, authenticated, time-bound, single-use | `test_at04_approval_single_use_and_not_replayable`, `test_at04_wrong_operator_channel_or_hash`, `test_at05_expired_approval_cannot_execute`, `test_at05_challenge_and_intent_expiry`, `test_other_user_cannot_use_callbacks` |
| Approved trade does not override a current veto | `test_at07_risk_veto_after_approval`, `test_at01_mode_change_after_approval_blocks` |
| Successful API call is not a confirmed fill | `test_at17_broker_confirmed_vs_local` |
| Missing response is not failure | `test_at09_timeout_does_not_resubmit`, `test_at11_unknown_state_blocks_new_exposure` |
| Unknown order state reconciled before new exposure | `test_at11_unknown_state_blocks_new_exposure`, `test_at20_restart_recovers_without_resubmitting` |
| Cancel request is not a cancellation | `test_cancel_fill_race_is_resolved_from_the_venue` |
| Strategy cannot exceed mandate | `test_at08_strategy_authority_locked` (mandates disabled) |
| AI cannot modify its own authority or limits | `test_at08_strategy_authority_locked`, `test_forbidden_imports`, `test_research_worker_cannot_approve_submit_or_change_authority` |
| Emergency stop ≠ positions closed | `test_at12_kill_switch` (positions and protective orders preserved) |
| Protective order ≠ guaranteed max loss | `test_approval_packet_contains_every_material_term` (warning text) |
| Free AI ≠ free execution/data | `test_approval_packet_contains_every_material_term` (cost fields and note) |
| Backtest ≠ future profitability | `test_costs_reduce_returns_and_are_disclosed`, `test_backtest_result_carries_the_past_performance_disclaimer` |
| Unknown financial truth/authorization/data → no new exposure | `test_at14_stale_data_blocks`, `test_at15_account_mismatch_blocks`, `test_each_check_blocks`, `test_broken_check_fails_closed`, `test_tampered_policy_fails_closed` |
| Real money only under the owner's activation, each lock on its own (ADR-034) | `test_an_activation_is_refused_by_each_lock_on_its_own`, `test_the_database_refuses_a_real_money_buy_without_an_activation`, `test_a_real_money_mandate_must_ask_within_the_activation`, `test_r20_needs_an_activation_and_keeps_real_money_within_its_cap`, `test_the_emergency_stop_ends_every_activation`, `test_a_failed_key_check_ends_real_money_and_raises_an_incident` |
