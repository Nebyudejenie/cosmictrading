# Robot A, the HFM account and the backtest — 2026-11-18

The owner's master specification of 2026-11-18 asks for these deliverables (section 17). The figures
come from production read-only, from the connected terminal's own report and from the gold lab's
recorded runs. Nothing here is a promise of profit. SIMULATED, DEMO and LIVE results stay apart, and
live trading stays off.

## 1. The connected HFM account (MT5 report, 2026-11-18 09:34 UTC)

| | |
|---|---|
| Account | DEMO, USD, balance 100.00, leverage 1:1000, hedging margin mode |
| Terminal | build 6274, connected, algorithmic trading allowed |
| Symbol | `XAUUSD` (no suffix), profit and margin in USD |
| Contract size | 100 oz per lot |
| Volume | minimum 0.01 lot (**1 oz**), step 0.01 lot (1 oz), maximum 100 lots |
| Price | 2 digits; tick size 0.01; tick value 1.00 a lot (0.01 USD per ounce per tick) |
| Stops level | 0: a stop-loss or take-profit may sit at any distance |
| Freeze level | not reported by bridge 12 |
| Execution | market execution; filling fill-or-kill |
| Swap | per lot a night: long −70.99, short 0.00; triple on Wednesday |
| Spread | 0.34 typical in the last 7 days (0.34 to 0.51 at the 1-minute robot's predictions) |
| Commission | none on the owner's demo deals (measured 2026-11-04 and kept in the risk policy) |

**What follows for 1 to 2 USD a trade:** the smallest gold trade is 1 oz, so a trade fits 2 USD only
when its stop is at most about 1.66 USD from the entry (2.00 less the 0.34 spread). The 1-minute robot
(Robot B) uses such a stop. The TEST rule (Robot A) uses about 12 to 16 USD at today's volatility.

**Cent accounts:**

- **Demo:** myHF offered the owner Demo Premium, Demo Contest and Demo Zero on 2026-11-18, with no
  Cent demo.
- **Live:** third-party reviews describe HFM live Cent accounts with no minimum deposit, but none
  confirms gold's contract size on Cent. Only myHF's "Open Live Account" screen for the owner's entity
  can confirm it.

## 2. Robot A, the TEST robot (S102)

| | |
|---|---|
| Mode | **SIMULATED**: 100 USD of simulated money on HFM's live bid and ask, nothing sent to HFM |
| Rule | the lab's best candidate (`trend_buy`, unchanged): BUY when the 4-hour and 1-hour trends are both up and the 15-minute RSI(14) has just crossed up through 50 with the close above EMA21; decided at each 15-minute close, acted on at HFM's price of that moment |
| Exits | stop-loss 2 × ATR14(15 min) below the fill, take-profit twice as far, closed after 24 hours |
| Risk | 1 USD a trade (2 at most), 4 USD a day including the open trade, HFM's trading day (17:00 New York), one trade at a time, a pause after three losses in a row, a stop at 20 % below the best balance |
| Before each trade | the probability of winning and losing in markets as volatile as now; expected reward, loss and value; 1:2; confidence; reasons for and against; what the connected demo would need |
| Decision | by the owner's bands: 30 to 50 % asks the owner (approval in the portal with their code; Telegram tells) |

**Why simulation:** on every HFM demo the owner can open, its stop would put about 13 to 16 USD at
risk on the 1-ounce minimum. Sections 3 and 6 of the specification refuse that trade. Each decision
still records what the demo would have needed, for example "refused on the connected HFM demo: its
smallest trade (1 oz) would risk 13.70 USD, more than your 2.00".

## 3. Robot A's backtest (lab Run 8, trial 24, BUY side; report of 2026-11-18)

**Data:** HFM's own XAUUSD 1-minute bid candles, 2022-10-31 to 2026-11-09, 1,416,366 candles. The
15-minute, 1-hour and 4-hour candles were built from them, and a rule saw completed candles only.

**Costs and fills:**

- Spread 0.35 (stress test at 0.50).
- Buys fill at the ask at the next minute's open.
- Stop and target are on the bid. When both fall inside one candle, the stop counts first. A gap
  through a level fills at the candle's open.
- Swap: longs pay 0.6882 USD per oz a night at 21:00 UTC, triple on Wednesday.
- No commission.
- No entry when the spread is more than 5 % of the stop.

**Split:** train 2022-11 to 2024; test 2025-01-01 to 2026-11-09, used once.

| | All | Train | Test |
|---|---|---|---|
| Trades | 372 | 88 | 284 |
| Target first (win) | 36.6 % | 37.5 % | 36.3 % |
| Stop first (loss) | 54.6 % | 48.9 % | 56.3 % |
| Closed at 24 hours | 8.9 % | | |
| Average win / loss | +1.81 R / −0.99 R | | |
| Expectancy | **+0.177 R** (t 2.41) | +0.236 R | +0.159 R |
| Profit factor (USD at 1 oz) | 1.11 | 1.30 | 1.08 |
| Net | +66.0 R | +20.7 R | +45.2 R |
| Maximum drawdown | 22.6 R | 9.6 R | 22.6 R |
| Recovery factor | 2.92 | | |
| Losses in a row, worst | 9 | 5 | 9 |
| Sharpe (weekday R, annualised) | 1.2 | | |
| Trades a weekday | 0.36 | 0.17 | 0.59 |
| Best / worst week | +9.0 R / −4.0 R | | |

**By year:**

| Year | Trades | Expectancy | Net |
|---|---|---|---|
| 2023 | 19 | +0.327 R | +6.2 R |
| 2024 | 65 | +0.222 R | +14.4 R |
| 2025 | 182 | +0.310 R | +56.4 R |
| **2026** | 102 | **−0.110 R** (win 29.4 %) | **−11.2 R** |

2026 also holds the worst drawdown (22.6 R) and the 9-loss run.

**By the market's volatility** (thirds of the trades by stop distance; this describes what happened
and is not a filter):

| Market | Win | Expectancy |
|---|---|---|
| Calm (stop under 9.46) | 44.4 % | +0.369 R |
| Normal (9.46 to 15.26) | 34.7 % | +0.166 R |
| **Volatile (15.26 or more, like gold in November 2026)** | **30.6 %** | **−0.002 R** |

**Spread 0.50:** 235 trades, 32.3 % won, +0.066 R (t 0.73), −56 USD at 1 oz.

**Random benchmark** (the whole family, BUY and SELL, 200 seeds): random entries in the same trend
filter beat it 11.5 % of the time.

**Verdict, by the rule fixed before the run:** no evidence of an edge. The t-value is below 2.2,
random timing is not beaten at p ≤ 0.0167, and 2026 is negative. Its BUY side is only the closest
thing to an edge in 24 trials, which is why it runs as a SIMULATED forward test that asks the owner,
not as an automatic robot.

**At the owner's 1 USD a trade:**

- 4 years would have made about +66 USD on 100.
- The worst drop was 22.6 USD, all in 2026. Starting from 100 USD, a drop like that passes Robot A's
  20 % stop (20 USD) and stops new trades.

## 4. Robot B, the 1-minute robot (unchanged)

Its calibrated probability of a 1:2 trade at a 2 USD stop:

- **Lab test:** predicted 26.8 % and happened 26.9 % over 579,710 signals on 2026 data.
- **Live forward test:** 9,286 predictions resolved on HFM's own prices from 2026-11-12 to 11-18.
  Predicted 26.8 %, happened 26.9 % (BUY 25.4 %, SELL 28.5 %).

Break-even is 33.3 %, and HFM's spread is 18 % of its stop. By the owner's bands it opens nothing by
itself, which is correct.

## 5. Risk-management evidence (automated tests, all passing)

- **Robot A:** `tests/trading/test_gold_trend_sim.py`. A 1 USD size with costs; the evaluation and
  the demo's verdict; once per candle; it asks the owner; on approval, the rule's own levels; the
  24-hour exit; the day's count; independence from the other simulation; START, approval and OFF
  audited.
- **The demo account:**
  - `tests/trading/test_hfm_balance.py`: the owner's accepted balance, and the drawdown's best
    restarting with it (S99, S100).
  - `tests/trading/test_hundred_limits.py`: R08's limits raised only by the owner (S100).
  - `tests/unit/test_risk_engine.py`: margin on demo only, real money fully paid.
- **Robot B:** `tests/trading/test_gold_m1.py`. Stop-loss and take-profit inside the order and held
  by HFM, a CRITICAL alarm and a pause when HFM does not hold the take-profit, the 1-second rule, the
  bands, and an approval good for one trade.

## 6. Known limitations

1. **The 10-trades-a-day target is not reachable with evidence.** Robot A finds about 0.36 signals a
   day. Robot B's trades lose on average at a 2 USD stop. Neither is forced (section 7).
2. **Robot A makes no real MT5 orders** until an account's smallest gold trade fits 2 USD at its
   stop, which needs a Cent-type account.
3. **Robot A's probability and expectancy are in-sample descriptions** by volatility. 2026 is
   negative and today's volatile market shows no edge.
4. **Section 8's high-performance mode** (above 80 % over 50 trades) cannot trigger for a 1:2 rule
   whose wins are 30 to 45 %. The rolling 20- and 50-trade record is still to build (S104).
5. **Not covered:** economic news and event risk. The robots trade through news.

## 7. Remaining work

- S104: the rolling 20- and 50-trade record (win rate, profit factor, expectancy, drawdown) for
  section 8, display only. It never raises risk.
- A pre-registered lab trial (25) of the TEST rule limited to calm and normal markets. This is a new,
  counted trial, not an adjustment of trial 24.
- A Cent-type account for the owner's 100 USD, demo or live, so that real MT5 orders can carry 1 to
  2 USD of risk.
