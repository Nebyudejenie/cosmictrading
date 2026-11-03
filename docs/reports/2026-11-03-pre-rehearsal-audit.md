# Pre-rehearsal audit — 2026-11-03

The owner asked for an evidence-based technical and trading-safety audit before the two simulated $100 gold rehearsals start. The two rehearsals are Binance gold (PAXG) and HFM Cent terms. The audit ran on release `3b1718e`.

- **What counts as PASS:** behaviour exercised by a test that was run. A configuration value alone is not a PASS.
- **What was not used:** no real-money account, order or credential. Live trading stayed DISABLED throughout.
- **What was left out of this file:** security weaknesses still open are reported to the owner directly, not here.
- **How:**
  - 134 probe tests (counting each parametrised case), 128 of them written by three independent reviewers, against the released code;
  - production checked through the release's own checks, container status and logs, and a restore drill of today's backup. Production's database was not read.

## Checklist

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1.1 | $100 start, $80 floor | PASS | A started rehearsal holds 100.00 in both the paper venue and the ledger. Its terms: 2.00 a trade, total stop 20.00, daily stop 6.00. `test_losing_trades_walk_down_to_the_floor_and_it_stops_for_good` |
| 1.2a | The −$20 limit counts fees, spread, slippage and open trades | PASS | The size counts both fees, down to the stop-limit price; the entry limit sits at the ask + 0.1 %; every open trade counts as a full loss. 13 real stop-outs each lost 0.99 of their plan; it stopped for good at 81.44 |
| 1.2b | …and HFM's overnight swap and price gaps | FAIL (open, owner decision) | A 12-week HFM Cent trade lost 2.27 against 1.63 planned (`test_hfm_swap_on_a_long_hold_passes_the_planned_loss`). A gap below the stop-limit leaves the trade open (`test_a_gap_through_the_stop_is_not_silent_but_can_pass_the_plan`) |
| 1.3 | Positions closed safely at the stop | PASS for stops; FAIL in a gap (open) | A stop fills at or above its limit. The stop for good waits until nothing is open and never sells by itself. In a gap the owner gets an incident and an exit to approve |
| 1.4 | Losses cannot silently pass the limits | FIXED | The closing notice now says "It lost X more than the Y planned at its stop". A gap raises an incident and asks for the exit |
| 2.1 | No duplicate trades from the daily engine | PASS | The same bar handled many times gives one order. Four concurrent runs give one entry. An hourly exit retry sells once, and runs straddling the hour sell once |
| 2.2 | Idempotency, order identifiers, transactions | PASS | Once-only signal keys and unique constraints; duplicate fill reports are recorded once; an approval is consumed once |
| 2.3 | Restarts cannot duplicate orders | PASS | A crash at each step never sent a second order or sold twice: before approval, inside the authorising transaction, after authorising, after the venue accepted, while recording a fill, while an exit waits |
| 3.1 | Both rehearsals simulated only | PASS | PAPER accounts on the paper venue with no network access. HFM Cent is never sent through the bridge |
| 3.2 | No path to a live order | PASS | Every route, CLI command, job and Telegram path that could reach real money refuses. Forged database rows never reach a venue. No live adapter is built or served |
| 3.3 | Credentials, environment, account identifiers | PASS (code) | The Binance adapter accepts only the test network (11 address variants refused). The HFM adapter accepts only demo terminals. Settings never reveal secret values. The production environment file was not read |
| 3.4 | Live disabled at every level | PASS | `test_each_lock_alone_refuses_the_live_mode`; production check "live trading disabled (no live adapter)" PASS on both stacks |
| 4.1 | Worker crashes and restarts | PASS, FIXED | The crash probes above; containers restart unless stopped, with 0 restarts; the watchdog restarts a stalled loop. Fixed: the loop could stall behind four slow jobs, and a long job could run twice. Background jobs now take at most two slots, a hung handler is stopped after an hour, and a worker stuck on every slot is restarted |
| 4.2 | Recovery after a VM reboot | NOT VERIFIED today | Last reboot test 2026-10-20 (15/15 and 11/11). Today "stack starts on boot" PASS. A reboot needs the owner |
| 4.3 | Stale prices and missing data | PASS | A stale quote waits; stale or too few bars, or a still-forming bar, never trade; paper fills nothing on a stale quote. Observation: a missing day inside the series is not detected |
| 4.4 | Failed broker requests | PASS | Venue unavailable, rejected, timeouts before and after acceptance, lost responses, duplicate fill reports: no inconsistent state |
| 4.5 | Interrupted trades reconciled | PASS | An unknown open order blocks new entries; a missed fill is imported; a fill of an unknown order fails reconciliation; a send in progress is not a mismatch |
| 5.1 | Decides at about 03:00 Addis Ababa | PASS | The daily bar is complete at 00:00 UTC; bars are fetched each hour and the auto-trader runs every 5 minutes, so it decides about 00:00–00:10 UTC. Never before the bar is complete, and once |
| 5.2 | Timezone handling | PASS | UTC throughout. Gold's weekend is computed in New York time across daylight-saving changes (21 cases). A Saturday signal trades on Binance and waits for Monday on HFM |
| 5.3 | Correct gold instruments | PASS | `SPOT:PAXG/USDT@BINANCE` on Binance's prices. `CFD:XAU/USD@HFM_CENT` on HFM's XAUUSD from the bridge. Both decide on PAXG daily bars |
| 5.4 | Sizing, stop-loss, take-profit, entry | PASS | The arithmetic was checked on both venues: stop 3×ATR(20) of daily bars, stop-limit 0.5 % below. There is no take-profit by design: exits are the rule's exit and the stop |
| 5.5 | No trade on stale or invalid data | PASS, FIXED | Fixed: the Binance cross-check compared Binance only with the test network's copy of its own prices. It now compares with HFM's gold first, and uses the copy only when no independent price is fresh, such as HFM's weekend (ADR-028 addendum) |
| 6.1 | Every decision and trade recorded | PASS | End to end: signal, skip and reason, intent, approval, orders, fills, ledger, review, audit |
| 6.2 | P&L, equity, drawdown, win rate, costs | PASS | The open trade's unrealised result, equity and drawdown; fees and swap included |
| 6.3 | Pages match the records | PASS (tests); NOT VERIFIED on production | No rehearsal runs yet, and production's database is not readable to the auditor |
| 6.4 | Telegram reports accurately | PASS, FIXED | Texts match the tables. Fixed: an exit notice read "at most None" |
| 7.1 | What renewal does | PASS | At 90 days, if chosen, a new run on the same account from its money then, on the same rule; an open trade is taken over with its worst case kept apart |
| 7.2 | It cannot reset a failed run or bypass $80 | PASS | After renewal from 94.11 the total stop was 14.11, so the floor stayed at 80. A stopped run never renews or restarts. The database refuses a lower floor |
| 7.3 | A new isolated $100? | No, by design | It continues the same account; one rehearsal account per venue |
| 7.4 | Limits and audit | PASS | Each run continues at most once (unique index); `mandate.granted`, `mandate.expired` and `mandate.renewed` are audited; "Stop renewing" works |
| 8.1 | Production configuration | PASS | `3b1718e`; 26/26 server checks; containers healthy |
| 8.2 | Backups and restore | PASS | Nightly backups; restore drill of today's set PASS 10/10; a tampered copy and a wrong key were refused |
| 8.3 | Authentication, authorisation, secrets, API | PASS, FIXED | Authorisation matrix, CSRF and origin on every POST, a fresh code for every money action, CSP, session expiry, API limit, no secret in pages, logs, Telegram or AI. Fixed: codes in forms and on Telegram are now limited |
| 8.4 | Deployment and worker logs | PASS | No warnings or errors since the release. Older logs went with the containers at the release |
| 8.5 | The 26 server checks | PASS | Release output of 2026-11-03 |

## Open, for the owner to decide

Each item below changes how trades are made, so none was changed without the owner's approval. On 2026-11-03 the owner chose the recommended answer to items 1–3; they are built in S54 (ADR-035 addendum).

1. **A gap through a stop-limit:**
   - The trade stays open until the owner approves the exit or the rule sells.
   - A real Binance stop-limit behaves the same. A real HFM account fills its stop at market; the HFM Cent rehearsal uses a stop-limit.
   - Options: sell a gapped simulated trade at market by itself; HFM Cent uses market stops as HFM does.
2. **HFM's swap is not in the plan.** A long-held last trade can end a little below 80, most likely after a renewal. Option: keep a cost reserve in the stop for good.
3. **A daily entry refused for a passing data reason loses that day's signal.** The reasons are a reconciliation too old, or the cross-check unavailable. Option: try again within the day, as is already done while a price is stale.
4. **The risk policy's defaults freeze a $100 rehearsal** after four losses in a row or a 10 drop. Tick the advised limits in "Get them ready".
5. **Before any real money:** the cross-check does not yet require the checked venue's own price among those it compares. Simulated accounts are covered by R09, which refuses a stale own quote.

## Review of the fixes

An independent review of the fix commit found that its first version dropped the test network altogether. With Coinbase and Bitstamp retired on this server (ADR-027), the Binance rehearsal would then have had no second price, and would never have entered. The fix was corrected before release: independent prices first, the test network's copy only as the last resort.

The review also found three worker gaps in the first version, each corrected:
- the last-slot rule could starve price fetching and the auto-trader;
- a hung handler could hold its slot for good;
- a job over an hour could still run twice.

It found one wording error, about which code limits are shared.
