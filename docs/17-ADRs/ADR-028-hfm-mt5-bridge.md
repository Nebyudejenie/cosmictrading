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
     - **Protective stop:** the position's own `sl`, set with `TRADE_ACTION_SLTP` and removed by setting `sl` to 0. It is never a separate order, so it can never open a short.
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
  8. **Registration gate:** as for Binance (ADR-026), an HFM account opens only after a PASSED contract run for that bridge (a tiny `BUY_LIMIT` far below the market, query, cancel, balances).
- **Consequences:**
  - The owner's computer must be on, with MT5 logged in; when it is not, HFM trading pauses safely. Production moves the bridge to a Windows VM on Proxmox.
  - Per-order and per-position limits must allow 1 oz of gold (about USD 4,100) before an HFM trade can pass the risk checks.
  - Market execution means fills can slip beyond the limit within the band; the fill price is recorded and checked against the band.
