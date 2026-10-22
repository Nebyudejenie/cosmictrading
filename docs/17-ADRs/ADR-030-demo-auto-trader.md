# ADR-030: The demo auto-trader — a bounded mandate instead of a click per trade, demo money only
- **Status:** Accepted · **Date:** 2026-10-20 · Implements TRD-23 for simulated accounts only; ADR-010 still holds for real money
- **Context:**
  - Until now every trade needed the owner's approval of its exact terms (ADR-011). On 2026-10-20 the owner asked for a simulated trader that trades by itself on the demo balance, risking at most $30 per trade.
  - The owner's choices:
    - the HFM demo account;
    - "$30" means the largest loss at the stop;
    - the strategy library decides, and of its rules only Turtle 20/10;
    - 1-hour bars.
  - Measured on 2026-10-20:
    - **HFM's smallest trades:** 0.01 BTC (about $816) and 1 oz of gold (about $4,130).
    - **On daily bars**, the library's stop of 3×ATR(20) puts about $62 (BTC) and $144 (gold) at risk on the smallest trade, so nothing fits $30 there. Daily rules also signal only every few weeks.
    - **On 1-hour bars** the smallest trade risks about $12 (BTC) and about $28 (gold).
    - **No library rule was switched on.**
- **Decision:**
  1. **A mandate, granted once.**
     - **Granting:** the owner grants a demo mandate on Operations with a fresh authenticator code (APPROVE_TRADE). It names one account, the rule, the instruments, the most a trade may lose at its stop, the number of trades a day, the open positions, the loss in a day after which no new trades start, and an expiry of at most 90 days. The defaults are $30, 5, 2, $60 and 30 days.
     - **Ending:** the owner can stop it at any time without a code, since stopping only removes authority. It also ends on expiry. The emergency stop overrides it, because the mode refuses new exposure.
  2. **Simulated money only, enforced by the database** (migration 0011).
     - A trigger refuses a mandate for any account that is not PAPER or SANDBOX.
     - A mandate's terms never change, and an ended mandate stays ended.
     - An approval on the new channel `MANDATE` must name its mandate. A trigger refuses it unless that mandate is active, unexpired and covers the trade's account.
     - The application checks all of this too, but no code path can give real money to the auto-trader.
  3. **The research layer proposes; the trading core decides.**
     - **Proposing:** `research.auto_trader`, every 5 minutes, computes the rule on the completed 1-hour bars of each instrument's signal series. HFM CFDs have no stored bars, so HFM BTC follows Binance BTC/USDT and HFM gold follows Binance PAXG/USDT.
       - A move from flat to long proposes a buy; a move to flat proposes selling exactly what the rule bought.
       - Each bar is handled once.
       - A series whose newest completed bar is over two hours old is never used.
     - **Deciding:** `cosmic.trading.mandates.approve` checks that:
       - the proposal is the auto-trader's own, under this mandate, on its account and instruments;
       - the risk policy is unchanged;
       - the mode allows new exposure;
       - the packet's own loss estimate at the stop is within the limit;
       - today's trades, the open positions and today's realised loss are within the mandate.
     - **Then** it records an approval bound to the exact packet, like the owner's, with the granting owner as operator. It is audited and reported on Telegram. A refused entry is set BLOCKED with the reason and reported.
     - **Exits** are approved while the mandate is active, because they only reduce exposure.
     - From the approval on, orders go through the same execution, protection, sync and reconciliation as any approved trade, with every risk check.
  4. **Sized to the loss, never resized past it.**
     - The quantity is whole lots of the venue, with quantity × (entry − stop-limit), plus the fee on both sides, at most the limit. This is the same estimate the approval packet shows and the mandate checks.
     - The stop is 3×ATR(20) of the hourly bars below the entry. On HFM the venue holds the stop with the position and fills it at market, so the stop-limit equals the stop.
     - If even the smallest lot would lose more than the limit, the trade is skipped and reported. The stop is never tightened to make it fit.
     - The policy's order limit still caps the size.
  5. **Labelled honestly.** Every proposal and message says AUTO, the mandate, the rule, "1-hour bars" and "a forward test, not a proven edge": the rule was evaluated on daily bars. Every account is named with its environment (DIR-02).
- **Consequences:**
  - **Coverage:** with today's volatility the HFM BTC trade fits $30 (about 0.02 BTC at about $23 at risk). HFM gold fits only while its hourly ATR stays below about $9.50, and is otherwise skipped with the reason.
  - **Gate G:** demo trades now accumulate without a click each, building the paper and demo record that gate G needs.
  - **When it stops:** after a revocation or expiry, open positions keep their broker stop and are the owner's to manage from Portfolio. A refused exit stays in the approval queue.
  - **The bridge:** the auto-trader depends on the HFM bridge like any HFM trade. With the PC asleep, prices go STALE and nothing is proposed.
  - **Rules:** only rules listed in `AUTO_RULES` may be mandated. Adding one is a code change with its own review.
- **Not decided here:** any mandate for real money. That stays behind ADR-010, the go-live gates and a separate owner activation.

## Addendum (2026-10-22): from money, ask first, and the auto-trader page

- **Owner's request:** "just put money in", "the system notifies me when it is time to trade", "it handles the risk based on the amount", and a dashboard of every trade's result in plain words.
- **Decision:**
  1. **Money and care level** (migration `0012_mandate_money`): the owner gives an amount and chooses Careful (0.5 %), Normal (1 %) or Bold (2 %). `terms_from_money` derives every limit from them:
     - the most a trade may lose at its stop is that share of the money;
     - the daily stop is three such losses;
     - at most 5 trades a day and 2 open positions.
     The money may not exceed the account's equity. Open positions, held fully paid, may never be worth more than the money: the auto-trader sizes within what is free and skips with the reason when nothing is. The terms are immutable like the rest of the mandate, and new settings replace the old mandate in one transaction (the old one ends REVOKED, "replaced by new settings").
  2. **Two modes.** AUTO approves within the mandate, as before. ASK runs the same checks, then asks the owner: the normal proposal card ("⏰ TIME TO TRADE"), approved with the authenticator code as any proposal. The mandate approves nothing itself in ASK mode.
  3. **Heads-up.** When a market comes within half an hourly ATR of the rule's breakout level, the owner is told the next hourly close may trade. At most once per market every six hours.
  4. **The auto-trader page** (`/trader`, first in the menu) and Telegram `/trader` show:
     - the mandate, with the money, the limits and today's use;
     - total, today, wins and losses, win rate, best and worst;
     - open trades, with their result now and the loss if the stop is hit;
     - trades waiting for the owner;
     - each market's distance to its breakout, and whether a trade fits the limits;
     - a result curve and a bar per trade;
     - the history, with how each trade ended.
     The page refreshes itself every minute. `/trader/setup` shows the plan for an amount and care level (the money each market needs at each level, from today's volatility) before anything is granted.
  5. **Closed-trade messages** say "✅ WIN +12.40 USD" or "❌ LOSS …", with the market's plain name.
- **Unchanged:** demo or paper money only, enforced by the database. Every trade passes every risk check and carries its stop. The emergency stop overrides everything. No promise of profit is shown, and results are labelled SIMULATED and a forward test.
