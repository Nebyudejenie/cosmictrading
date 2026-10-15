# HFM demo account through MetaTrader 5 — facts read from the terminal (B-10, ADR-027)

Read on 2026-10-15 at 16:13–16:20 EAT. A read-only script ran next to the owner's MT5 terminal: MetaTrader 5 build 6264, official `MetaTrader5` Python package 5.0.6231, Python 3.11 on Windows 11. The account number is deliberately not recorded here. The API facts are in `mt5-python-api.md`.

## Setup facts

1. **Connection:**
   - `initialize(r"C:\Program Files\MetaTrader 5\terminal64.exe")` connects only once the terminal is **logged in**.
   - Before a successful login it failed with `(-10005, 'IPC timeout')`. That matches the MQL5 forum report for builds 6231/6249 (2026-10-10): it worked "after manually logging into a … demo account".
   - With a wrong server it failed with `(-6, 'Terminal: Authorization failed')`.
2. **Login:**
   - The demo account's server is `HFMarketsGlobal-Demo` (from HFM's account-opening email); `HFMarketsGlobal-Demo3` answered "Invalid account".
   - The password must be saved in the terminal, so that `initialize()` can reconnect without the bridge holding it.
3. **Account (`account_info`):**
   - `trade_mode` = `ACCOUNT_TRADE_MODE_DEMO`, currency USD, leverage 1:1000.
   - `margin_mode` = **HEDGING**: every buy is its own position ticket.
   - The demo balance was 100,000 USD.
   - `terminal_info`: `connected=True`, `trade_allowed=True`, `tradeapi_disabled=False`.
4. **Constants in package 5.0.6231:**
   - `ORDER_FILLING_FOK=0`, `ORDER_FILLING_IOC=1`, `ORDER_FILLING_RETURN=2`.
   - `SYMBOL_TRADE_MODE_DISABLED=0`, `LONGONLY=1`, `CLOSEONLY=3`, `FULL=4`.
   - `SYMBOL_FILLING_FOK`/`SYMBOL_FILLING_IOC` are **not exported**; `filling_mode` is read as a number.
5. **Running the probe from WSL:** it works through Windows interop, and through `explorer.exe <file>.bat` as a normal user process (same session 1, WinSta0\Default, not elevated, no job object).

6. **Broker server time:**
   - MT5 stamps ticks (and orders and deals) in the broker's server time.
   - First sync, 2026-10-15 13:50:39 UTC: the XAUUSD tick read `time_msc` 1792083039402, which is 16:50:39 if read as UTC. The server clock runs at **UTC+3**.
   - The portal's validation rejected those quotes as "in the future".
   - Since then the bridge reports the offset, learned from a fresh tick and rounded to 15-minute steps (ADR-028). The server converts every MT5 time to UTC and stores no prices until the offset is known.

## Symbols (`symbol_info`)

| Symbol | Description | Contract per lot | Min / step / max lots | Digits / tick | Execution | Filling flags | Swap long / short | Currencies (base / profit / margin) |
|---|---|---|---|---|---|---|---|---|
| `XAUUSD` | Gold Spot | 100 (oz) | 0.01 / 0.01 / 100 | 2 / 0.01 | market (exemode 2) | 1 (FOK) | −78.56 / 0.0 (swap_mode 1) | USD / USD / USD |
| `XAUUSD247` | Gold Spot | 100 | 0.01 / 0.01 / 100 | 2 / 0.01 | market | 1 | −78.56 / 0.0 | USD / USD / USD |
| `#BTCUSD` | Bitcoin vs US Dollar | 1 (BTC) | 0.01 / 0.01 / 200 | 3 / 0.001 | market | 1 | −8.0 / −4.0 (swap_mode 5) | USD / USD / USD |

All three have `trade_mode` 4 (FULL) and `trade_calc_mode` 2. Path for gold: `Metals & Energies\Spot\Gold & Silver`; for bitcoin: `#Crypto\Major`.

Ticks came back empty while the symbols were not in Market Watch. Per the documentation, `symbol_select(symbol, True)` must come first.

## Consequences for the design (ADR-028)

- **Smallest trades:** 0.01 lot is 1 oz of gold (about USD 4,100) or 0.01 BTC (about USD 810). The current per-order limit of USD 500 must be raised by the owner before any HFM trade can pass the risk checks.
- **Market execution, fill-or-kill:**
  - An immediate buy is a market deal with `ORDER_FILLING_FOK`, checked against the approved price band just before sending.
  - A buy below the market rests as a `BUY_LIMIT` pending order.
- **Hedging:** sells always name the position ticket. Protective stops are the position's own `sl`, so a stop can never open a short.
