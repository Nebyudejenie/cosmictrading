# ADR-032: The Gold page — gold trading made visible, explained and benchmarked
- **Status:** Accepted · **Date:** 2026-10-22 · Builds on ADR-030 (the auto-trader) and ADR-031 (the rule competition)
- **Context:**
  - **Owner's request (2026-10-22):** "focus on gold trading … research why I lost and how I win … make it easy and visible … automated trading".
  - **Constraints:** real money stays off, and no profit can be promised.
  - **What the evidence said:**
    - No gold rule qualified in the strategy library or the rule competition.
    - The auto-trader's hourly rule lost money on gold over the last year.
    - Gold fell about 25 % from its 2026 high.
- **Decision:**
  1. **One page, `/gold`** (second in the menu, READ_ACCOUNT). It shows:
     - **Gold now:** price, change over a day, 30 days and a year, fall from the high, trend (50/200-day averages, `market/regime.py`), today's daily range against its usual, and the trading session now (UTC: Asia, London, New York, late).
     - **The last year:** a chart with the 50- and 200-day averages.
     - **Can you trade it:** what one ounce ties up and risks at the auto-trader's stop, and the least money for each care level.
     - **The radar:** the last 48 hours and where the auto-trader would buy.
     - **Why it lost:** the auto-trader's rule replayed on the last year of hourly gold twice, with costs and without (`research/library/insight.py`). The split covers what costs took, stop versus rule exits, the session a trade was opened in, the daily trend at the time, and holding times. It ends in plain sentences and compares with simply holding.
     - **How to win:** the levers the numbers support — costs, only rules that earned it, sizing to the money, beating the benchmark, and the stops.
     - **The competition:** gold's rows.
     - **The long run:** buying a fixed amount at the first price of each month and holding, over 1, 3 and 5 years. This is the benchmark a trading rule must beat.
     - **Your trades:** your own closed gold trades.
  2. **Telegram `/gold`:** the same in a few lines.
  3. **Honesty:**
     - Research results are HISTORICAL SIMULATIONs, in R or %. Costs are labelled (the policy's fee ASSUMPTION, the venue's measured spread, slippage ASSUMPTION), and CFD swap is named as not modelled.
     - Patterns (a session, a trend) are observations, not rules: a rule built on them must win the rule competition before the auto-trader may use it.
     - The benchmark says "not advice".
     - Real futures and real-money gold are not connected: live trading needs a broker adapter, gate G and the owner's activation (ADR-010).
- **First findings** (local check on public Binance PAXG hourly prices, the last year): before costs the auto-trader's rule made about +21 R over 123 trades. Costs decide the result:

  | Costs | Result after costs |
  |---|---|
  | The general 0.6 % fee assumption | −81 R |
  | No fee, 0.1 % spread | −23 R |
  | No fee, 0.04 % spread | about −1 R |
  | No fee, 0.02 % spread | +19 R |

  The page therefore leads with the costs, and asks the owner to confirm HFM's real commission (B-08).
- **Not decided here:** modelling CFD swap (it needs the bridge to report HFM's swap rates), and any change to the auto-trader's rule (ADR-031 decides).

## Addendum (2026-10-22): the overnight swap is counted

- **Why:** a CFD held past the broker's daily rollover pays (or earns) a swap. On the Gold page it was the last cost not counted.
- **Decision:**
  1. **The bridge (version 3)** also reports each symbol's swap mode, triple-swap day and point size. It already reported the long and short swap.
  2. **`venue_terms.swap_per_night`** converts MT5's report into a share of the price per night for a buyer and a seller. It handles points per lot, money per lot (in the account currency, USD) and yearly rates. It refuses to guess when the mode is unknown (an older bridge) or not money (the reopening modes).
  3. **The replay (`expect.py`)** charges it for each rollover a trade is held over, at about 21:00 UTC. A market closed at weekends (gold) pays nothing on Saturday and Sunday and three nights on the broker's triple day. Bitcoin pays every night.
  4. **Where it counts:** the past-year test, the Gold research (with its share of the costs, as "of which overnight swap") and the rule competition (doubled in its double-cost check).
  5. **Shown** in every cost line, or as "not counted: the bridge does not report it yet".

