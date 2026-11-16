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

## Addendum (S87, 2026-11-12): the fast lane, and an order within one second of its prediction

- **The owner's rule (2026-11-12):** when the robot predicts, its trading request leaves for HFM at once, within one second, and the data it predicts from is delivered at once.
- **Measured before the change (production, read-only, 2026-11-12):**
  - the bridge's report arrived every 2.2 to 3.3 s; HFM's price was 0.15 to 0.65 s old on arrival;
  - the robot decided on average 3.4 s after a 1-minute candle closed (179 decisions; the fastest 1.3 s);
  - an approved trade then waited for the trading cycle (every 5 s) and for the bridge's next report (2 to 3 s).
  So a prediction's order left 3 to 9 seconds after it.
- **Decision: two lanes.** The report stays as it is (the whole account, the source of truth for positions, deals and reconciliation). Beside it the bridge (version 8) keeps one small request open on a connection that stays up:
  - **prices up:** HFM's newest tick a few times a second, kept apart from the report (`bridge_fast`, migration 0032) and put into HFM's forming candles, so a 1-minute candle is complete a fraction of a second after the minute turns. The robot reads the lane's price when it is newer than the report's. Only beside a fresh report of the demo account: the report says whose prices these are.
  - **orders down:** the server holds the request for up to 250 ms and answers the moment a SUBMIT or CANCEL waits. Nothing else travels on it.
  - **one way for an order:** while the lane is up (heard within the last second) the report leaves orders to it; when it is silent the report carries them again, as before bridge 8.
- **Decision: one step from the prediction to the order.** The worker watches for a newly complete 1-minute candle ten times a second (`gold.jobs.watch_candles`). The robot's decision and its order are then one step (`hfm_now` → `execution.execute_approved`): every risk check as before, no waiting for the trading cycle. An intent is still executed under its own row lock and only while APPROVED, so the cycle and this step never both take it.
- **Decision: late is never sent.** The robot's intent records when it was decided and that it must leave within 1,000 ms (`decided_at`, `send_within_ms`).
  - The adapter refuses it once that has passed, and the bridge command expires at that moment.
  - The command carries what is left (`ttl_ms`), less the way to the bridge (half the round trip the bridge measures). The bridge counts it from the order's arrival on its own clock and refuses a late one, unsent. Two computers' clocks never decide it.
  - The robot opens nothing while the rule cannot be kept: bridge before 8, the lane not heard from for 1.5 s, or a round trip over 400 ms. This is checked before the owner's approval in advance is used.
- **Decision: one thread calls MT5.** The bridge runs three threads (`serve`): the main one reads the terminal and sends the orders; the report and the fast lane each only wait on the network and pass what arrives through queues. The MetaTrader5 package is used from the thread that connected, as before; nothing relies on it being safe across threads. An order waits for the MT5 call in progress (at most half a snapshot), never for a request in flight.
- **Decision: an explicit order between results and reports.** With two lanes a command's result and the report that shows what it did no longer travel together. Each finished command is numbered (the bridge's run and a count); each report says how many were finished when it read the account, and reads the account as of one moment. `mt5_bridge.reflects` reads a command's outcome only from a report that has reached its number. A number never reached is given up after 60 s.
- **Decision: the checks read the price the trade was decided on.** The risk checks read HFM's price from the stored quotes, which the reports keep every 15 s. The 1-minute trade's price band is 0.60 wide, so a price 15 s old refused a real order (production, 2026-11-12 13:04 UTC). The decision's own tick is stored as HFM's quote at the decision (`hfm_candles.record_quote`). Storing every tick was rejected: several per second, each a recorded fetch, for a price only a decision needs.
- **Decision: an order's answer waits for the report that shows it.** Before the fast lane a result arrived with the report read after it, and code that reads the report straight after an order relies on that (the trade check closes what it bought by what the report holds; with the result ahead of the report it left a position open in one run of three, found by the full check). The bridge sends its report at once after an order on the fast lane, and the adapter gives a mutating command's answer only when that report is stored (`HfmBridgeAdapter._shown`, at most 10 s). The order itself has left long before; only its answer waits.
- **Measured per order:** from the prediction to the hand-out (server clock), the way to the bridge, the bridge's wait before MT5 was asked, and HFM's answer time (`speed_*` on the order's ACKNOWLEDGED event; shown on the dashboard).
- **Cost of the step:** 150 statements and 17 connections from the decision to MT5 (it was about 185; the 1-minute rule no longer loads the hourly reading, and a timeframe's candles are one statement). A budget in the test keeps it from growing unnoticed.
- **Not changed:** every risk check, the demo-only rule on the server and in the bridge, the account named on every order, the journal (a command delivered twice runs once), sizes, stops and targets.
- **Compatibility:** bridge 8 with an older server behaves like bridge 7 (the lane starts only when offered; the new fields are sent only then). Bridge 7 with this server works as before, and the 1-minute rule opens nothing and says why.

## Addendum (S90, 2026-11-13): several lanes, after the first measurements on the real path

- **Measured in production with bridge 8 (2026-11-13, read-only):** the robot's decision step 80 to 200 ms; a 1-minute candle decided 0.65 to 1.5 s after it closed (median 0.83 s; 3.4 to 4 s before S87); the fast lane's request every 0.68 s; the bridge's snapshot under 1 ms.
- **The limit that remains is the way itself.** The bridge reaches the server through Cloudflare, by design (the server publishes no port on the home network). That round trip was about 0.06 s on 2026-11-12 and 0.36 to 0.43 s on 2026-11-13, measured from the bridge computer and from a workstation; the server's own answer takes 0.06 s. With one long-held request, an order waits between half and one and a half round trips: 0.2 to 0.6 s that day.
- **Decision (the owner's choice of 2026-11-13, "both, lanes first"):**
  - **Now, bridge 9:** three order lanes, each held for up to a second on its own connection and started a third of a second apart, and a price lane every quarter of a second that is never held and also takes a waiting order. An order then nearly always finds a request waiting at the server; what remains is one way of the round trip. No server change: the server already hands an order to whichever request asks first, once.
  - **Next, to be proposed to the owner before anything is opened:** a direct link between the bridge computer and the server on the home network, so that an order no longer travels through the internet at all.
- **Rejected:** a longer hold on a single lane (prices would arrive only once a hold); raising the robot's limit on the round trip (the one-second rule would then be met only on average).

## Addendum (S91, 2026-11-14): a direct link on the home network, with the internet path as its fallback

- **Why:** the way through the internet is the remaining limit, and it changes from day to day: a round trip of 0.06 s on 2026-11-12, 0.36 to 0.43 s on 2026-11-13, 0.02 s on 2026-11-14. The bridge computer and the server stand on the same home network, 2 to 4 ms apart (measured 2026-11-13). The owner approved this design on 2026-11-13, before anything was built.
- **What is not changed:** the server still listens on the home network with sshd only (`verify.sh` checks it at every release), the firewall is as it was, and the portal is still published through Cloudflare only.
- **The link:**
  - **Bridge 10** keeps one SSH connection to the server and forwards a port on its own loopback to the relay's port on the server's loopback. The same two requests travel through it (`/bridge/v1/sync`, `/bridge/v1/fast`), as plain HTTP inside SSH, with the bridge's token as on the internet path.
  - **The server's key is pinned** on the bridge computer (strict host key checking against one stored key): the bridge's token is never sent to a server showing another key.
  - **The sign-in on the server** (`deploy/vm/bridge-link.sh`) is a system account without a shell or a password. sshd gives it one right: to forward to the relay's port on the loopback. It refuses a command, a terminal, a file copy, any other forward (local, remote or dynamic) and the agent. Its one key is accepted from the bridge computer's address only, and sits in a file only root can change.
  - **The relay** (`deploy/compose.bridge-link.yaml`) is published on the VM's loopback only and passes bytes to the web process. It has its own fixed address on the internal network; it is not the proxy the web process trusts.
  - **The web process** serves that address the bridge's two addresses, by POST, and 404 for everything else, the sign-in page and the health check included (`BridgeOnlyMiddleware`, `COSMIC_BRIDGE_ONLY_PEERS`). So whoever held the link's key could reach nothing the bridge's token does not already open.
- **Fallback at every request:** a lane (and the report) uses the link while its tunnel is up and no request on it failed in the last 5 seconds; otherwise, or the moment a request on it fails, the same request goes over the internet path. A request may so arrive twice: results and reports are taken twice without harm, and an order handed to an answer that never arrived is sent by nobody (it ends unsent at its time limit, as on one path). The tunnel is started again when it ends, ever more rarely while it never comes up.
- **The one-second rule on two paths:** each request carries the round trip measured on the path it takes, and the server counts an order's remaining time with that (`_claim(way_ms=)`), never with the other path's. An order's record names its path (`speed_lane`: `direct`, `fast` or `report`) and counts the way there on it. The dashboard says which path is in use.
- **Set up by the release, never by hand:** the admin hands in the bridge computer's public key and its address as two files in the checkout's `var/` (not in git). `deploy.sh` checks them before anything changes, keeps them in production's config, starts the relay with the stack, and sets up the sign-in after the release itself is up. sshd checks its whole configuration before it is reloaded (never restarted), what it applies to the admin's sign-in must be unchanged, and what it applies to the link's sign-in must be the rules above; otherwise the change is taken back. A failure there never fails the release: the bridge carries on over the internet path and `verify.sh` reports the link as not in order.
- **Verified at every release (`verify.sh`):** the relay runs and is published on the loopback only; through it the sign-in page and the health check answer 404 and the bridge's address asks for its token; sshd applies every rule to the link's sign-in. With the link off: that nothing of it is left.
- **Tested against a real sshd:** `scripts/bridge-link-drill.sh` runs the script in throwaway containers with the VM's own SSH hardening: 65 steps, among them every refusal above, a rejected change taken back, a moved address, removal, the bridge's own tunnel code (a request on the tunnel, the internet path when the tunnel is killed, the tunnel back by itself), and `deploy.sh` itself as root with a stand-in for docker (a release without the link, a bad hand-in stopping the release before anything starts, the link switched on, kept, ignored for staging, and removed).
- **No waiting to fill a packet:** a request and its answer are a few small pieces each, and a hop that waits to fill a packet holds one back for tens of milliseconds now and then. Measured through a real sshd (2026-11-14, one machine): 2.7 ms a request as a rule and up to 90 ms with the relay as it was; 1.6 ms and at most 2.2 ms with `nodelay` on both sides of the relay and each request written by the bridge in one piece (`_Whole`).
- **Undo:** an empty file `var/bridge-link.remove` at the next release removes the relay, the sign-in and its key; or `"direct"` is taken out of the bridge's `config.json`. Either way the bridge uses the internet path, as bridge 9 does.
- **Rejected:**
  - **A port on the home network for the portal or for the bridge's two addresses** (with TLS): the first application port on the LAN, a certificate to issue and renew, and a second place where the portal can be reached.
  - **A VPN between the two machines:** a new network interface and a new service on both, for one connection.
  - **The relay at cloudflared's address** (as the break-glass relay): the web process would trust its headers, and the whole portal would be reachable through the link.
- **Risks kept in view:** the link's key lies on the bridge computer beside the bridge's token, so it adds no new place to protect; a stolen key opens the bridge's two addresses from that one address only, and they still ask for the token. A local program on the bridge computer could listen on a loopback port and pose as the tunnel only if the bridge were pointed at it, which needs the same access as reading the token.
