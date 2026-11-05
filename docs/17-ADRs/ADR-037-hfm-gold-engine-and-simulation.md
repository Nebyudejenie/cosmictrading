# ADR-037: HFM gold only — HFM's own candles, a multi-timeframe reading, and an internal simulation with the owner's own money

- **Status:** accepted, 2026-11-04 (S61, S62)
- **Context:** the owner's directives of 2026-11-04 ("HFM gold only", then the architecture correction).
  - **The product:** one gold product on HFM, through MetaTrader 5.
  - **The data:** HFM's prices and HFM's own candles (M1, H1, D1), never Binance's as the execution reference.
  - **Mode A, an internal simulation:**
    - the owner's own starting balance, trading amount and risk;
    - fractional ounces from 0.01;
    - BUY and SELL, stop-loss and take-profit;
    - realistic costs, floating and realized P/L, every decision recorded;
    - "WAIT — Your selected trading amount is too small for this opportunity at your chosen risk level" instead of a raised risk or balance.
  - **Mode B, the HFM demo:** HFM's real limits, kept apart.
  - **The dashboard:** honest statuses.
  - **Binance:** out of the product.

## Decisions

### 1. HFM's own candles (`market/hfm_candles.py`)

- **Live:** every bridge report (each second) updates the forming 1-, 5- and 15-minute, hourly and daily candles of HFM's XAUUSD, from HFM's bid.
  - A tick older than 10 s (a closed market's frozen price) is not recorded.
  - A closed candle is never changed by a live report.
- **History rebuilt:** from the HFM quotes stored since 2026-10-15 (about one every 15 s), once a day where none exists. Highs and lows can miss a move between samples.
- **The broker's own (bridge version 4):** a read-only `RATES` command returns MT5's candles (`copy_rates_from_pos`); each hour the server asks for up to 1,000 daily, 2,000 hourly and 1,000–1,500 shorter candles. They replace the rebuilt ones of the same period.
  - Prices travel as exact text, never floats.
  - **Time conversion:** MT5's times are on HFM's server clock, New York + 7 hours, and are converted through New York time so that summer and winter both land right; today's offset alone would shift older summer candles by an hour.
  - **A different clock:** when HFM's reported offset differs from New York + 7 hours, the candles are refused, not stored.
  - **The bridge's journal:** a read-only answer (RATES) is sent once and never journaled.
- **A daily candle** is HFM's trading day, from 17:00 New York (00:00 on HFM's server), as MT5 draws it.
- **Migration 0020** lets `bridge_commands.kind` be `RATES`.

### 2. The reading (`gold/analysis.py`), a forward test, not a proven edge

- **Every timeframe:**
  - trend from EMA 8 and EMA 21 and where the price is;
  - momentum from RSI 14;
  - volatility from ATR 14;
  - support and resistance from the last 20 candles' low and high.
- **BUY:** H1 up, D1 not down, and on M1 the RSI crossing up through 50 with the price above its 21-EMA. **SELL:** the mirror.
- **WAIT:** otherwise, with the reason; also when the hourly volatility is under 0.05 % or over 1.5 % of the price, or the candles are too few or too old.
- **The daily filter** applies only from 21 HFM daily candles (bridge version 4 gives years at once).
- **Stop distance:** 1.5 × the hourly ATR.
- These rules were chosen, not fitted or tested on history. They are judged by the simulation's forward results; no edge is claimed.

### 3. The internal simulation (`gold/sim.py`, migration 0020): SIMULATED, never sent to HFM

- **The records:** its own tables.
  - An account with the owner's starting balance; only RUNNING or STOPPED change.
  - Settings, append-only (the latest counts).
  - Positions, which only close.
  - Decisions, append-only and unique per account and 1-minute candle: a restart never decides or opens twice.
  - A ledger, append-only and unique per reference; the balance is its sum.
- **The size:** the most a trade may lose is the risk per trade × the amount (never more than the equity left). Ounces = that ÷ the stop distance, rounded down to 0.01.
  - **Under 0.01:** WAIT, with the exact words above and "at R %, this opportunity trades from an amount of X". The risk is never raised.
  - **Other limits, each a WAIT with its reason:**
    - total open risk (% of the amount);
    - trades open at once;
    - the day's loss (realized and floating, HFM's trading day);
    - the drop from the best balance;
    - selling switched off;
    - free margin at HFM's reported leverage.
- **Execution:**
  - **Fills:** a BUY opens at HFM's ask and closes at its bid, a SELL the other way; the spread is paid as at HFM.
  - **Checks:** stops and targets every second; a position closes at the price that crossed them, so a jump costs more than planned.
  - **Swap:** HFM's own terms from its report, per night held at 17:00 New York, triple on HFM's triple day.
  - **Commission:** none (HFM charged none on the owner's demo deals).
- **Exits:**
  - the stop-loss;
  - the take-profit (a multiple of the stop distance, or none);
  - the rule (the hourly trend turned against the position);
  - the owner's stop, which closes everything at HFM's price.
- **Running it:** one simulated account at a time. The worker checks every second and decides once per 1-minute candle.
- **Every open position is watched, whatever its account's state:** a stopped account's trades close at HFM's price, now or at the next price when there is none (review). The swap of a night held is booked before any close.

### 4. Mode B, the HFM demo

- The HFM demo page shows HFM's own account: balance, equity, its positions with tickets (marked manual or automatic) and its deals.
- It answers, by HFM's rules (1 ounce minimum), whether HFM would take a trade from the owner's amount at their risk, with the same WAIT wording.
- It offers the one-press execution test (S57).
- Automatic HFM demo orders from this reading come in the next slice. The S60 page's own simulation mode is replaced by the internal simulation.

### 5. Binance paused (`mandates.PAUSED_VENUES`)

- No auto-trader acts on a market on BINANCE or BINANCE_TESTNET, or on one whose signal is their prices. That includes both paper rehearsals and the earlier HFM auto-trader, which read Binance's PAXG.
- Every record is kept. Emptying the constant (a reviewed change) resumes them.
- R10's cross-check of HFM's price against an independent market stays: it is a safety check, not the execution reference.
- The Binance pages are under "More", marked paused.

### 6. The dashboard

- The HFM panel shows eight statuses: SYSTEM HEALTHY, HFM CONNECTED, MARKET DATA RECEIVING, AI ANALYSIS WORKING, SIMULATION RUNNING, DEMO EXECUTION READY, RECONCILIATION MATCHED, RISK PROTECTION VERIFIED.
  - Each is "yes" only on evidence: for example, an analysis within a day for the AI, and a stop-loss at HFM on every open position for protection.
  - The headline is ALL GOOD only when every essential one is.
- Each HFM position shows its ticket, who opened it, its side, size, entry, stop-loss and take-profit.
- A manual position without a stop-loss is named with its ticket until it is protected or closed.

## Consequences

- **Small amounts trade in the simulation;** HFM's demo shows its real limit honestly. Nothing raises the owner's balance or risk.
- **No Binance price in HFM decisions.** Until bridge version 4 is installed on the VM (an owner step), the daily filter waits for 21 rebuilt daily candles.
- **Not yet built:** the AI's second opinion (the AI shows "not switched on"), and automatic HFM demo orders. Real money stays impossible: no live adapter, and the bridge trades demo accounts only.

## Addendum, S63 (2026-11-04): one panel, safe HFM exits, approvals in the portal

- **One panel, My Gold Auto-Trader:**
  - **the owner chooses** the amount, the mode, automation and permission;
  - **the system calculates** every other limit from the amount (`sim.defaults`);
  - **START** comes after a review page with every figure;
  - **settings** are saved in place (append-only), so a change never switches the trader off and on;
  - **Automation OFF** is a pause, PAUSED: no new trade, and open trades stay protected. Taking risk away (OFF, stop, reject) never needs the code; adding it (START, ON, approve, settings) does.
- **Statuses from evidence:** READY, RUNNING, WAITING, NEEDS ATTENTION, PAUSED, STOPPED (`board.Board.status`).
- **HFM exits:** on a venue whose stop lives on the position, the exit closes the position with the stop on. The stop is never cancelled first, so an unfilled exit leaves the position protected.
- **Approvals:** trades are approved in the portal only (`telegram_approvals` off). The Telegram path remains in code behind that setting, with its tests, and the approval service refuses it while the setting is off.
- **The old HFM auto-trader stays paused:** its mandate's signal is not moved to HFM's candles. HFM demo automation will come from this reading, started from the panel by the owner.
- **Market timing:** the sessions are business hours in each centre's own time zone, and the busy window is London and New York together. It is a habit of the market, not a promise; no news calendar is claimed.

## Addendum, S64 (2026-11-04): HFM Demo mode, through the existing order path

- **No new engine, no new authority.** HFM Demo mode is the gold reading under a mandate (rule `gold_reading`) on the HFM demo account. Every trade is:
  1. a proposal through every risk check (`create_candidate`);
  2. approved by the mandate (Automatic) or asked in the portal (Ask me first);
  3. sent by the bridge with its stop-loss;
  4. reconciled against HFM's own records.
- **The rule `gold_reading`:**
  - It may be granted only on HFM's gold on the HFM demo account (`hfm_mt5_demo`), on hourly mandates. It is not offered on the older setup pages.
  - The research auto-trader skips it: it is not in the strategy catalog. Pages that replay a rule say it has no replay.
- **Size:**
  - HFM's whole ounces (0.01 lot = 1 ounce), so that the loss at the stop, with the risk policy's fee counted both ways (as the risk check counts it), stays within the mandate's loss a trade.
  - When 1 ounce would lose more, it waits and says from which amount it would fit. The amount and risk are never raised. The owner is told once a day, not every minute.
- **Direction:**
  - **BUY only.** A SELL reading is recorded and not sent: the order path holds long positions only, and a genuine short needs its own checks.
  - **Exits:** the rule closes on an hourly downturn, with the stop still on until HFM has closed the position.
- **Once per 1-minute candle and mandate** (`gold_hfm_decisions`, migration 0021, append-only), so a restart never proposes twice.
- **Settings and OFF:**
  - A settings change replaces the mandate in one transaction. The owner hears one "Settings changed", not "OFF" then "ON" (`mandates.end(replaced=True)`).
  - **Automation OFF** ends the mandate; open positions keep HFM's stop-loss and take-profit.
- **A mandate lasts at most 90 days** (its existing limit), with reminders before it ends; this is not open-ended.

## Addendum, S65 (2026-11-05): HFM's measured costs, and one-step START

- **The fee for HFM is measured, not assumed.**
  - HFM's own deal records (commission + fee on each deal, against its value) give the fee per side, once at least 5 deals exist.
  - The owner's HFM Demo START, an authenticated press with their code, records it as a new risk-policy version with the evidence in the reason, so every check (risk checks, sizing, the mandate) uses the same figure.
  - A fee the owner set for HFM is never overwritten.
- **HFM Demo START never starts what cannot trade.** When the amount cannot carry HFM's smallest trade (1 ounce) at the owner's risk, nothing starts. The page says from which amount it would, and offers the simulation with the same amount.
- **START replaces any earlier auto-trader on the HFM demo,** with one "ON … replacing the earlier one" alert.
