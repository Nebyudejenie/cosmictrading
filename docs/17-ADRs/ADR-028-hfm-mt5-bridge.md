# ADR-028: The HFM link — a MetaTrader 5 bridge, CFDs held like fully paid spot
- **Status:** Accepted · **Date:** 2026-10-15 · Implements ADR-027 decision 1–2
- **Context:**
  - HFM is reachable only through MetaTrader 5. MetaQuotes' Python package runs on Windows next to a logged-in terminal (`docs/integrations/mt5-python-api.md`, `hfm-mt5-demo.md`).
  - The system runs on Linux behind Cloudflare.
  - HFM's demo account is a hedging CFD account with market execution and fill-or-kill. Gold trades in lots of 100 oz and BTC in lots of 1 BTC, both with 0.01-lot steps.
  - The owner's rule: demo only, buy only, no leverage.
- **Decision:**
  1. **The bridge** (`bridge/cosmic_mt5_bridge.py`) is one small Python program on the Windows computer.
     - It attaches to the running terminal with `initialize(path)`, using the terminal's saved login. It never holds the HFM password.
     - Every second it makes one outbound HTTPS call to the portal, `POST /bridge/v1/sync`. It sends a heartbeat (terminal and account status, no login number), the latest ticks, the symbol specifications and the results of finished commands. The answer carries any new commands.
     - The call passes Cloudflare Access with a service token and is authorised by the bridge's own token, compared in constant time. No inbound port is opened on the computer.
  2. **Demo only, enforced twice.** The bridge refuses every trading command unless `account_info().trade_mode == ACCOUNT_TRADE_MODE_DEMO`. The server opens HFM accounts only as SANDBOX, with adapter `hfm_mt5_demo`.
  3. **Commands through the database.** The worker's `HfmBridgeAdapter` writes a command (`bridge_commands`) and waits for its result.
     - A read that times out is "venue unavailable".
     - A send or cancel that times out is "outcome unknown", and the order is reconciled, never resent.
     - Commands carry a deadline, and the bridge does not start an expired one.
     - The bridge keeps a local journal of finished commands. A command delivered twice is answered from the journal and not executed again.
  4. **Idempotent orders.**
     - Every MT5 request carries `magic` = the first 63 bits of SHA-256 of our client order ID, and the ID in `comment`.
     - Before sending, and after any uncertain result, the bridge looks the order up by `magic` in open orders, history orders and deals.
  5. **Buy only, on the position.**
     - **Entry:** a `BUY` market deal (FOK) when the approved limit is at or above the ask, after checking the tick is inside the approved band. Otherwise a `BUY_LIMIT` pending order at the limit.
     - **Protective stop:** the position's own `sl`. It is never a separate order, so it can never open a short. It is removed by setting `sl` to 0.
       - **Sent with the entry** (bridge version 2, 2026-10-16): the approved stop price travels in the buy request itself (`sl` on the `BUY` deal or the `BUY_LIMIT` order). HFM holds the stop on its server from the moment of the fill, even when the bridge, the PC or the worker is offline. Before this, the stop followed the fill as a separate `TRADE_ACTION_SLTP` request, and an outage straight after a fill (the bridge was away about 4 h on 2026-10-15) would have left the position without a stop.
       - The protection step then records the stop as an order as before; the bridge's `TRADE_ACTION_SLTP` answers "no changes".
       - The adapter refuses an entry with a stop when the bridge reports a version before 2, and nothing is sent.
       - If the stop closes the position before the protection step has sent its stop order, the bridge reports that order as filled from the stop-out deal, so the books stay exact.
       - Until the protection step sends it (seconds after the fill), the entry's own stop is not listed as an open order, so reconciliation does not call it unknown.
     - **Exit and take-profit:** a `SELL` market deal with `position=<ticket>`, at most the position's volume, sent only if the bid is at or above the approved limit; otherwise "not filled" (IOC).
  6. **CFDs presented as fully paid spot.** The adapter turns the hedging CFD account into the system's spot model:
     - **USD cash** = broker balance − Σ open (volume × contract size × open price).
     - **Holdings** = Σ open volume × contract size per symbol, in the base unit (oz of gold, BTC).
     - **Fills** come from deals. A deal's commission, fee and swap are the fill's fee in USD; swaps are charged when the position closes, as MT5 reports them.

     A buy therefore needs the full notional in cash, so effective leverage is at most 1 whatever HFM's 1:1000 allows. Equity matches the broker's, and the standard reconciliation (USD, XAU, BTC) works unchanged.
  7. **Instruments and data.**
     - New venue `HFM` (kind BROKER) and asset class `CFD`.
     - Instruments `CFD:XAU/USD@HFM` (symbol `XAUUSD`, quantity in oz) and `CFD:BTC/USD@HFM` (`#BTCUSD`, quantity in BTC).
     - Their tick, lot and minimum come from the bridge's `symbol_info`. Quotes and venue status come from source `hfm_mt5_bridge` (the bridge's heartbeat and ticks, labelled DELAYED).
     - No heartbeat for 60 s means the venue is UNAVAILABLE and nothing new is sent.
  8. **Registration gate:** as for Binance (ADR-026), an HFM account opens only after a PASSED contract run for that bridge (a tiny `BUY_LIMIT` far below the market, query, cancel, balances). Since contract version 2 the order carries a stop-loss below it, and the check confirms that HFM holds the stop on the order.
- **Addendum (2026-10-16): alerts when the bridge is silent.**
  - The night of 15–16 October showed one cause raising three alerts: two stale sources and a CRITICAL "our records and the venue disagree". Nothing had been compared.
  - Now a bridge is offline when its heartbeat source (`VENUE_STATUS`) is stale. That gives one incident, `data.bridge_offline:<bridge>`, which covers its price source.
  - A reconciliation that could not reach the venue raises `accounting.venue_unreachable:<alias>`: WARNING when flat, CRITICAL with positions or active orders. Real mismatches keep `accounting.reconciliation_failed`.
- **Consequences:**
  - The owner's computer must be on, with MT5 logged in; when it is not, HFM trading pauses safely. Production moves the bridge to a Windows VM on Proxmox.
  - Per-order and per-position limits must allow 1 oz of gold (about USD 4,100) before an HFM trade can pass the risk checks.
  - Market execution means fills can slip beyond the limit within the band; the fill price is recorded and checked against the band.
- **Addendum (2026-10-17, one stop-loss per position):**
  - MT5 keeps one stop-loss per position. Every STOP_LOSS order of an entry sets the same stop on the entry's positions, and cancelling any one of them clears it for all of them. The adapter declares this as `stops_per_position`.
  - So a cancel that was not confirmed is never sent again for an older stop while another stop of the same trade is active and not being cancelled itself (`_newer_stop_holds_it`). Sending it would remove the protection the newer stop holds. Found by review with the MT5 emulator; covered by `test_an_old_stops_cancel_never_clears_the_stop_a_newer_one_holds`.
  - With the bridge silent, a cancel fails at once (`BrokerUnavailableError`) instead of waiting for an answer; the follow-up sends it again later.
  - **Known limit:** cancelling one of two stop orders of the same entry by hand (portal Cancel) clears the stop-loss of the whole entry until the protection step places a replacement on the next cycle, and the other stop may then be reported as ended at the venue. Two stop orders for one entry only exist after a partial fill.
  - **One stop order per entry (2026-10-18).** A defect found by review: after a partial fill an entry had two stop orders, each sized to its part, while every stop-loss deal goes to the newest. A stop-out's second deal was then refused as an overfill, and the books kept a closed position. Now, on a venue with `stops_per_position`, a new stop covers the whole holding and replaces the older stop orders in our books (CANCELLED, "superseded"). Nothing is sent for the old stops, because MT5's stop-loss stays in place.
    - The older stops end in the same transaction as the new stop's risk checks, inside a savepoint, so R17 counts the new stop alone. If a check fails, the older stops stay as they were.
    - While an older stop is still on its way, the new one waits.
    - Covered by `test_a_stop_out_after_a_partial_fill_leaves_the_books_flat`: the books end flat, with no overfill and reconciliation passing.

## Addendum (2026-11-03): independent reference prices first

- **Found by the pre-rehearsal audit:** for Binance's PAXG/USDT, the only second price the cross-venue check (R10) could find was the Binance Spot Test Network's book. That book copies Binance's own prices (0.0 bps apart on 2026-11-03), so it could never catch a bad Binance price.
- **Decision:**
  - A simulated venue's book is no longer an independent price (`market/data._pair_views`).
  - PAXG/USDT's references are XAU/USD (HFM's gold through the bridge) and PAXG/USD. PAXG/USD on Coinbase and Bitstamp counts only where they are polled; this server retired them (ADR-027). They were about 18 bps below Binance on 2026-11-03, the USDT/USD rate.
  - Only when no independent market is fresh does the check fall back to the simulated venue's book, as before. That happens at HFM's weekend and daily break, or with a silent bridge. The fallback can then tell a missing price but not a wrong one.
  - An instrument on a simulated venue is still compared with the real markets.
  - HFM gold is compared with Binance's PAXG/USDT as before, and with the test network only as that same last resort.
- **Rejected:** dropping the test network altogether. With Coinbase and Bitstamp retired, the Binance rehearsal would have had no second price on any day, and would never have entered (found by review, before release).
- **Known limit, before any real money:** the check does not require the checked venue's own price among those it compares. For simulated accounts, R09 refuses a stale own quote. A live account would need its venue's live price compared.

## Addendum (S60, 2026-11-04): your amount is a risk budget; gold is held on margin

- **The owner's correction (2026-11-04):** "I decide the amount. The system calculates the position size. … Never require me to increase my balance because the developer hardcoded an unsuitable position size."
- **What made every signal skip:**
  - The auto-trader's money had to cover the whole value of its open trades (point 6: fully paid), so 1 oz of gold (about 4,100) never fit in $100 or $1,000.
  - Also, `terms_from_money` refused any amount whose risk per trade was under 1.00.
  - HFM's 1 oz minimum (0.01 lot × 100) is HFM's own rule, read from the bridge; it is not hard-coded.
- **Decision:**
  - For a CFD auto-trader (every market a `CFD:`), the money is a risk budget. Its open trades may be worth up to `mandates.CFD_EXPOSURE` = 10 × the money; HFM asks 1/1,000 of the value as margin.
  - Each trade is still sized by the loss at its stop: at most the risk per trade, chosen from 0.1 to 5 %.
  - A spot coin stays fully paid (exposure 1).
  - The smallest risk per trade is 0.01.
- **Unchanged:**
  - **The accounting:** the adapter still presents positions as fully paid (cash = balance − Σ open cost), so the account's own cash check (R07) and the reconciliation are unchanged. The account (HFM's demo, or the simulation's virtual 10,000) must hold the trade's value. The owner's amount is what the auto-trader may risk and use.
  - **The replay:** it applies the same multiple (`expect.Limits.exposure`), so the replay and the live sizing stay equal.
- **Why 10:** with stops at 3 × ATR of hourly bars (about 0.5–1 % of gold's price), a 2 % risk holds about 2–4 × the money. 10 is a guard against a very quiet market's tight stop, not a size. A gap past the stop loses more than the risk per trade, so the guard bounds that too.
- **Internal simulation:** `hfm-sim`, a PAPER account on HFM Cent terms (0.01 oz steps, HFM's prices, measured swap), labelled SIMULATED. It is for amounts too small for HFM's 1 oz. Nothing in it is sent to HFM.

## Addendum (S74, 2026-11-10): a second bridge, on its own HFM demo account

- **The owner's choice (2026-11-10):** run a daily gold robot next to the hourly one, on a **second HFM demo account** with its own MT5 terminal and bridge, rather than in the simulation or instead of the hourly robot.
- **Why not one account:**
  - Positions are one row per account and instrument, so one robot's short would be booked against the other's long: `fills.py` refuses "a short while a long is held".
  - The database also allows one active mandate per account.
- **Decision:**
  - Bridges are named in `mt5_bridge.BRIDGES`: `hfm-demo` (adapter `hfm_mt5_demo`, primary) and `hfm-demo-2` (adapter `hfm_mt5_demo_2`).
  - Each bridge has its own token: secrets `hfm_bridge_token` and `hfm_bridge2_token`, web only. A token syncs only as its own bridge (403 otherwise).
  - **Market data:** only the primary bridge feeds HFM's prices, candles and terminal status. The second bridge's report is kept for its own account's orders, deals and balance, so one market never has two feeds.
  - **Each bridge trades only its own account:** commands go to a bridge only while its MT5 account is the one its adapter's account was opened on (fingerprint, per adapter).
  - **One MT5 account never serves two bridges:** opening an account, the contract check and the trade check all refuse an MT5 account another open HFM account was opened on (`sandbox.mt5_account_in_use`).
  - Migration 0026 allows SANDBOX accounts on `hfm_mt5_demo_2`, still one active account per adapter.
- **Alerts:**
  - The second bridge has no price sources, so it raises no "Bridge … not reporting" heartbeat incident.
  - Its silence shows as its account's "venue unreachable, account not reconciled": WARNING while it holds nothing, CRITICAL with a position or an active order.
- **The risk policy is per account:** the account must hold a gold trade's value (R06, R07) and keep the loss at every stop within 3 % of its equity (R19). The second demo therefore needs about 10,000 or more, even though the robot's own money is 2,000.

## Addendum (S83, 2026-11-11): Broker Connections: accounts managed in the portal, bridge 7

- **The owner's directive (2026-11-11):** connect, test, save, switch and assign HFM MT5 accounts from Admin → Broker Connections, without editing the server; DEMO and REAL kept apart; a REAL connection never enables trading.
- **Terminals ("slots"):** `mt5_bridge.SLOTS`.
  - `hfm-demo`: terminal 1, demo, HFM's prices.
  - `hfm-demo-2`: terminal 2, demo.
  - `hfm-real`: terminal 3, **read-only**. It is not in `BRIDGES` and has no adapter, so no order can be routed to it. Its bridge runs with `"read_only": true` and refuses every trading command whatever the server sends. Its own token: `hfm_bridge3_token`.
- **Accounts** (`trading/connections.py`, migration 0030): one record per MT5 account (login and server), bound to one terminal.
  - The owner's DEMO/REAL choice is only a claim. HFM's `account_info().trade_mode`, read through the bridge, decides. A mismatch is refused, and the bridge signs back in to the account it had.
  - **REAL:** held only with its **investor (read-only) password**, on the read-only terminal. HFM refuses orders on such a sign-in (`trade_allowed` false). The execution switch cannot be turned on, enforced by a database constraint. Live trading keeps its own authorization (ADR-034), untouched.
  - **DEMO:** usable only once HFM has verified it and the owner saved it. New entries also need its own execution switch on.
- **Passwords:** encrypted at once (`core/sealing.py`; AES-GCM with an HKDF key from `secret_key`; bound to the record), never returned to a page or logged.
  - A password is decrypted only when a `LOGIN` command is handed to the bridge, in that one sync answer. The command row stores a reference, never the password. The decrypted value is registered with the log scrubber.
  - When MT5 is already signed in to the account, a test only asks who is signed in (`VERIFY`); the password is not sent at all.
  - Behind a bridge older than 7, a demo account MT5 is already signed in to is verified from the bridge's own report (fingerprint and account mode), with no command at all. Every other test needs bridge 7, and the portal says so. A terminal that never reported is refused at once.
  - The bridge uses it once for `mt5.login` and never journals or logs it; an error text containing it is masked.
- **Bridge 7:**
  - `LOGIN`: sign in. Refused when the account signed in now holds positions or orders, unless the owner chose to leave them at HFM (`allow_open`). After a refusal it signs back in to the previous account with MT5's saved password. After sign-in it checks the type HFM reports (`expected_mode`).
  - `VERIFY`: who is signed in, without signing in.
  - `expected_account`: the fingerprint on every SUBMIT and CANCEL; refused on any other account, nothing sent.
  - `attached_tp`: the take-profit sent with the entry.
  - Reported per account: `trade_allowed`, the margin level, and every position and order open on the account (any symbol). Reported per symbol: the tick value and margin currency.
- **Switching never touches positions:** MT5 does not close or move positions when it changes account. A sign-in, a robot move or a disconnect is refused while positions or orders are open there, unless the owner explicitly leaves them at HFM with their stop-loss.
- **Identity before every order:** `connections.entry_problem` runs in the adapter just before an entry is sent. On a terminal Broker Connections manages, the entry needs:
  - the account connected, verified DEMO, with its execution switch on;
  - MT5 signed in to exactly it, reporting within the last minute and linked to HFM;
  - and the HFM account opened on that terminal to be this account.
  The bridge checks the account again (`expected_account`). START needs the Demo Robot assigned to such an account and shows it above the button. A terminal not yet managed keeps the adapter's own checks.
- **Take-profit at HFM:** an entry carries its take-profit (bridge 7, capability `attached_take_profit`).
  - While HFM holds it (`take_profit_state` HELD), the protection step never sends its own.
  - Once HFM has closed the position at it (deal reason 5, TAKEN), nothing is sent, and the reconciliation books HFM's deal as the trade's exit (like a close by hand, S71; audit `trade.closed_at_take_profit`).
  - An older bridge drops it, and the robot keeps watching the target as before.
- **Health** (`broker.health`, every 30 s): per connected account, one of:
  - OK;
  - DEGRADED: MT5 has lost HFM, Algo Trading is off, or a REAL account is signed in with its trading password;
  - DOWN: no report for a minute, signed out, or another account or type signed in.
  An incident is open while it is not OK; CRITICAL when a robot runs there and it is DOWN.
  - **Restart recovery:** a terminal that comes back signed out is signed in again with the saved password, at most every 2 minutes.
  - One refusal by HFM stops automatic sign-in and alerts the owner, so the account is never locked by retries.
- **Not changed:** demo-only trading (server and bridge), the Cent fail-closed currency rule (S55), one active mandate per account, and the live locks.
- **Correction (S85, 2026-11-11): the owner never picks a terminal.** Their first account landed on a terminal that is not installed. `connections.choose_slot` now places a new account (the terminal signed in to it, else a free installed one); a Test moves a misplaced account to the free terminal that is signed in to it; and one press assigns the Demo Robot with its DEMO execution on.
