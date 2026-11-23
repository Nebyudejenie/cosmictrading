# HFM's gold contract: verified against unverified, and the calculator's evidence (2026-11-23, S115)

Every figure below was read from production by a read-only probe on 2026-11-23, between 05:11 and 05:13 UTC (`~/.cache/cosmic-checks/broker_values_probe.py` and `swap_check_probe.py`, outside the repository). It comes from terminals 1 and 2, which are both on HFM demo accounts (bridge 12).

**None of it is verified for the owner's real account.** Terminal 3 is not installed yet. Once it reports, the Manual Trading page shows the same table for the real account, from its own data.

## 1. The values and their evidence (HFM demo)

| Figure | Demo value | Status | Evidence |
|---|---|---|---|
| Contract size | 100 oz a lot | **VERIFIED** (demo) | HFM's booked profit on all 29 closing deals of terminal 1 equals (exit − entry) × lots × 100; the largest difference is 2E-16 |
| Value of a price move | 100.00 USD a lot for 1.00 of price | **VERIFIED** (demo) | tick 0.01 worth 1.00 USD a lot, consistent with 100 oz; confirmed by the same booked profits |
| Price step (tick size) | 0.01 | REPORTED | MT5 symbol info; every booked price is on it |
| Lot sizes | 0.01 to 100, steps of 0.01 | REPORTED | MT5; HFM filled all 30 demo orders on this grid |
| Account currency | USD | REPORTED | MT5 account info (both terminals) |
| Leverage | 1:1000 | REPORTED | MT5 account info (both demos); margin mode HEDGING |
| Margin | lots × 100 × price ÷ 1000 | **NOT VERIFIED** | only an estimate: no stored figure from HFM exists (0 risk checks hold one), and bridge 12 does not read HFM's margin calculator. Bridge 13 reads `order_calc_margin` for a BUY and a SELL of 1 lot, and the calculation mode |
| Spread | median 0.34 | MEASURED (demo) | 101,276 of HFM's quotes over 30 days: 90 % at or below 0.36, 99 % at or below 0.45, widest 0.65 (13:30 UTC on 6 and 10 November, US data releases). Quotes are stored every few seconds, so brief spikes between them are not seen |
| Commission | 0.00 | MEASURED (demo only) | 0 on all 58 deals of terminal 1. **This says nothing about the real account:** HFM's account types are charged differently |
| Swap, BUY | −73.56 points = −73.56 USD a lot a night | **CONTRADICTED** (demo) | HFM booked **0.00** on three BUY positions of 0.01 lot held over its rollover (Wednesday→Thursday, the triple night; Monday→Tuesday; Tuesday→Wednesday), where the terms give −2.21, −0.74 and −0.74. The calculator still counts the stated cost, the cautious figure |
| Swap, SELL | 0.00 | REPORTED | MT5 terms; no SELL held overnight |
| Triple-swap night | Wednesday | REPORTED | MT5 `swap_rollover3days` = 3; the one position held over a Wednesday booked no swap at all |
| Nearest stop-loss (stops level) | 0: no minimum | REPORTED | MT5 `trade_stops_level` |
| Freeze level | — | **NOT REPORTED** | bridge 12 does not read it; bridge 13 does |
| Stop-out and margin call | — | **NOT REPORTED** | bridge 12 does not read them; bridge 13 does (`margin_so_so`, `margin_so_call`, their mode) |
| Execution | market, fill-or-kill | REPORTED | MT5 `trade_exemode` 2, `filling_mode` 1 |

**What differs between the demo and the real account:**
- The account type sets commission, spread and swap. The demo is a standard demo; the real account's type is not known yet.
- Leverage on a real account is set by HFM's terms for its equity and may be lower than 1:1000.
- The stop-out level, freeze level and swap-free status are per account.
- The calculator uses the real account's own reported contract once it is connected, never the demo's.

## 2. What S115 adds so that nothing is assumed

- **Bridge 13** (terminal 3 first) reports, per symbol:
  - the freeze level;
  - the margin calculation mode;
  - the initial and maintenance margin;
  - the spread now in points;
  - HFM's own margin for 1 lot, BUY and SELL, at the price it was read at (MT5 `order_calc_margin`);
  - HFM's own profit for 1 lot on a 1.00 rise (`order_calc_profit`).

  Per account, it reports the stop-out and margin-call levels and their mode. A terminal without the calculators reports the rest. Nothing is sent.
- **The calculator** uses HFM's own margin figure when it has been read, and says which source it used: "HFM's own margin calculator" or "estimated from the leverage 1:N (HFM's own figure not read)". It refuses a trade whose loss at the stop-loss would bring the margin level to HFM's stop-out, and warns:
  - when the spread is more than twice its 30-day median (for a SELL, that its stop is hit by the ask);
  - when there is no current price (the spread and the market-order check are then not done);
  - when HFM's freeze level is reached.
- **The Manual Trading page** shows the table above for the demo and, beside it, for the real account once terminal 3 reports, with each figure's status and evidence.

## 3. Calculator test evidence

Every case is an executable test on HFM's reported contract, in `tests/unit/test_manual_examples.py`, `tests/unit/test_manual_calc.py` and `tests/unit/test_manual_evidence.py`. Each figure was checked by hand.

**Worked examples**

| Equity | Trade | Risk | Lots | Loss at the stop | Profit at the target | Margin at 1:1000 |
|---|---|---|---|---|---|---|
| 100 | BUY, stop 1.00 | 1 % | 0.01 | 1.00 (1.00 %) | +2.00 | 3.54 |
| 500 | BUY, stop 5.00 | 1 % | 0.01 | 5.00 (1.00 %) | +10.00 | 3.54 |
| 1,000 | BUY, stop 10.00 | 1 % | 0.01 | 10.00 (1.00 %) | +20.00 | 3.54 |
| 2,500 | SELL, stop 7.25 | 1.5 % | 0.05 (0.0517 rounded down) | 36.25 (1.45 %) | +72.50 | 17.69 |
| 3,000 | SELL, stop 4.00 | 1 % | 0.07 (0.075 rounded down) | 28.00 (0.93 %) | +56.00 | 24.77 |
| 5,000 | SELL, stop 15.00, 1:500 | 2 % | 0.06 (0.0666 rounded down) | 90.00 (1.80 %) | +180.00 | 42.46 |
| 10,000 | BUY, stop 25.00 | 2 % | 0.08 | 200.00 (2.00 %) | +400.00 | 28.31 |
| 20,000 | BUY, stop 12.40, 1:1.5 | 1 % | 0.16 | 198.40 (0.99 %) | +297.60 | 56.62 |
| 50,000 | SELL, stop 7.77 | 0.5 % | 0.32 (0.3217 rounded down) | 248.64 (0.50 %) | +497.28 | 113.22 |

**Refusals and warnings**

| Case | Result |
|---|---|
| A BUY's stop at or above its entry; a SELL's at or below | refused: "a BUY's stop-loss must be below its entry" (and the SELL mirror) |
| A BUY's target below its entry; a SELL's above | refused |
| A price of 0 or less; a side other than BUY or SELL | refused |
| 100 equity, BUY, stop 10.00 (or 20.00) | NO TRADE: the smallest lot loses 10.00 (20.00), it fits from about 1,000 (2,000), "Never raise the risk to make it fit" |
| 250 equity, SELL, stop 3.00 | NO TRADE: 3.00 is 1.20 %, it fits from about 300 |
| A typed 0.015 lot, 150 lots, or 0.05 lot at 1,000 equity (5 %) | refused: not on HFM's grid / above HFM's largest / above the 2 % limit |
| 1,000 equity, stop 0.10, 1:100 | refused: about 3,538.59 of margin needed, 1,000 free |
| HFM's own margin of 3,538.59 a lot (1:100), 0.10 lot, 300 free | refused: 353.86 needed, 300.00 free |
| A loss to the stop that leaves the margin level at 48 % with a 50 % stop-out | refused: HFM would close positions itself first |
| Spread 1.20 against a usual 0.34 | warned: "3.5 times its usual 0.34"; for a SELL, "its stop-loss is hit by the ask" |
| No current price (stale or missing) | warned; the size is from the typed entry, no spread is counted, and the page's risk checks refuse the trade |
| 12.00 a lot of commission at 1,000 equity, stop 10.00 | NO TRADE: 10.12 is above the 10.00 allowed; at 2,000, 0.01 lot risks 10.12 |

**A property test** (300 random cases) covers equity from 50 to 1,000,000, risk from 0.1 to 2 %, stops from 0.10 to 60.00, BUY and SELL. In every case:
- the lot size is on HFM's grid;
- its loss at the stop is never above the money allowed;
- one more step would exceed it, or HFM's largest trade is reached;
- a refusal happens only when HFM's smallest trade already risks too much.
