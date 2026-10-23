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

## Addendum 2 (2026-10-22): what to expect, and the market radar

- **Owner's request:** "continue with your idea and amaze me".
- **Decision:**
  1. **What to expect.** Before the owner commits money, `/trader/setup` replays the auto-trader's own rule on the last 12 months of its signal series' hourly bars, with the owner's plan. `/trader` shows a short version for the settings in force. The replay (`research/library/expect.py`) follows the live path:
     - the rule's own state, with decisions at the close filled at the next open;
     - the 3×ATR(20) stop, with a gap through it filled at the worse open;
     - whole-lot sizing as `auto.size_for_max_loss` (a test checks they agree);
     - the per-trade limit, trades a day, open positions, the daily stop and the money.
     It reports the result and its share of the money, trades a week, the win rate, the typical win and loss, the worst losing streak, the deepest drop, the time in a trade, each half, each month and each market, and the signals it would have skipped and why.
  2. **Costs.** The fee is the risk policy's assumption for the venue (ASSUMPTION). The spread is the median of the venue's own quotes over the last day, or 0.1 % (ASSUMPTION) when none are stored. Slippage is 0.05 % a fill (ASSUMPTION). Overnight financing (swap) on CFDs, delays and requotes are not modelled, and the page says so.
  3. **History.** A worker job (`research.auto_history`, every 6 hours) backfills the signal series to 390 days of hourly bars once, in pages of 1,000. Hourly bars older than 400 days are refused on ingestion. Until 30 days are stored, the page says how many days there are.
  4. **The market radar.** Each market card on `/trader` draws the last 48 hours of the price the rule watches, as SVG on the server:
     - out of a trade: where it buys (the 20-hour high, step by step);
     - in a trade: where it sells (the 10-hour low), the stop, and the purchase.
     Every line is named in a legend and at its end. Colours are three categorical slots, checked for colour-blind readers in both themes, and each hour has a tooltip.
- **Honesty:**
  - Results are labelled SIMULATED and HISTORICAL SIMULATION, and the page says "not a promise".
  - A loss is shown as plainly as a gain.
  - The period is named exactly ("the last 198 days" until a year is stored).
  - Nothing was tuned: it is the rule the auto-trader runs.
- **Unchanged:** this is read-only. It grants nothing, sends nothing, and never changes a mandate.

## Addendum 3 (2026-10-22): the total loss stop

- **Why:** the past-year test (addendum 2) showed the rule losing money on hourly bars after costs, and a mandate had only a daily stop: a long losing run could take most of the money over a month. The owner agreed to an overall stop on 2026-10-22.
- **Decision** (migration `0013_mandate_total_stop`):
  1. **The limit.** A mandate carries `total_loss_limit`, the most the auto-trader may lose under it in total. From money it is ten losing trades' worth: 5 % of the money at Careful, 10 % at Normal, 20 % at Bold. Mandates granted before have no stored value and get ten trades' worth. It is immutable like the other terms.
  2. **Before each entry.** The entry is refused unless a full loss on it and on every open position, added to what the auto-trader has lost so far under the mandate, stays within the limit. Profits give room; manual positions on the account count as open.
  3. **Switching itself off.** At the start of each cycle, a mandate whose losses leave no room for one more full losing trade, with nothing open, ends `STOPPED`, with the amount in the reason. The owner is told on Telegram. Open trades are never cut by it: their exits stay approved while they are open.
  4. **Starting again** is a new grant, with a fresh code, so a new total begins only by the owner's decision.
  5. **The same rule in the test.** The past-year replay applies the limit: it skips an entry that could pass it, and it ends where the auto-trader would have switched itself off.
- **Shown:** the limit and what has been lost so far on `/trader`, on the plan, on Operations and in Telegram `/trader`. After a stop, `/trader` says STOPPED ITSELF and why.
- **The database** accepts the new status `STOPPED`. The downgrade refuses while a mandate uses the limit or is STOPPED.

## Addendum 4 (2026-10-23): size to the evidence — half size after a deep drop

- **Why:** the plan's step 5 (docs/03 §5). After a long losing run, the evidence for the rule is weaker, so the next trades should risk less, before the total loss stop is reached.
- **Decision:**
  1. **The rule.** The auto-trader's closed trades under the mandate are replayed in order, with their results after fees. Once the running result falls five losing trades' worth (`HALVE_AFTER`) below its best point, every entry is sized to half the per-trade limit. It returns to full size once the drop is under two and a half trades' worth (`RESTORE_BELOW`). In between it keeps the size it has, so it does not flip with each trade.
  2. **Derived, not stored.** The size follows from the closed trades, so there is no new term and no migration. A new mandate starts at full size.
  3. **Only smaller.** Half size changes only the sizing. Every limit still applies at the full per-trade amount: the mandate's check, the daily stop, and the total stop's worst case.
  4. **Told and shown.**
     - Telegram says it once at each switch.
     - `/trader` shows "Trade size FULL / HALF" with the drop.
     - Telegram `/trader` adds a line while it is at half.
     - The proposal says "half size after a deep drop".
  5. **The same in the test.** "What to expect" replays the same rule.

