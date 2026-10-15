# MetaTrader 5 from Python — verified facts for the HFM bridge (B-10, ADR-027)

Reviewed 2026-10-15 from MetaQuotes' official documentation (mql5.com) and PyPI. Facts are quoted or closely paraphrased from the cited pages. Anything not listed here is **not documented here and must not be assumed**. HFM's own website blocks automated reading, so HFM-specific values (symbol names, contract sizes, hours, swaps, commission) are read at run time from the terminal (`symbol_info`, `account_info`), never hard-coded.

Base URL for the Python pages: `https://www.mql5.com/en/docs/python_metatrader5/`.

## 1. Platform and connection

1. The package obtains data "via interprocessor communication directly from the MetaTrader 5 terminal". Install with `pip install MetaTrader5`. (index page)
2. PyPI ships `MetaTrader5` 5.0.6231 as `win_amd64` wheels only, for Python ≥3.6 <4 (`https://pypi.org/pypi/MetaTrader5/json`). It runs on Windows, next to the terminal.
3. `initialize()` establishes a connection with the terminal. There are three call options:
   - without parameters, the terminal is found automatically;
   - with `path` to the terminal EXE;
   - with path plus `login`, `password`, `server`, `timeout`, `portable`. (mt5initialize_py)
4. In `initialize`:
   - if `login` is not given, "the last trading account is used";
   - if no password is given, "the password for a specified trading account saved in the terminal database is applied automatically" (likewise the server);
   - the default timeout is 60 000 ms. (mt5initialize_py)

   So the bridge can attach to the running, logged-in terminal **without holding the HFM password**.
5. `login(login, password=…, server=…, timeout=…)` returns True on success, otherwise False. If the connection is not established within the timeout, "the call is forcibly terminated and the exception is generated". (mt5login_py)
6. `last_error()` gives information on the last error. Functions return `None` on error. (most pages)
7. `terminal_info()` returns the terminal status. Its fields include `connected`, `trade_allowed`, `tradeapi_disabled`, `ping_last`, `build`, `company`. (mt5terminalinfo_py)

## 2. Account

8. `account_info()` returns a named tuple, or None on error. (mt5accountinfo_py) Its fields include:
   - `login`, `trade_mode`, `leverage`, `margin_so_mode`, `trade_allowed`, `trade_expert`, `margin_mode`, `fifo_close`;
   - `balance`, `credit`, `profit`, `equity`, `margin`, `margin_free`, `margin_level`;
   - `server`, `currency`, `company`.
9. `ENUM_ACCOUNT_TRADE_MODE`: `ACCOUNT_TRADE_MODE_DEMO` (demo account), `ACCOUNT_TRADE_MODE_CONTEST`, `ACCOUNT_TRADE_MODE_REAL` (real account). (`/en/docs/constants/environment_state/accountinformation`) **The bridge must refuse to trade unless `trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO`.**
10. `ENUM_ACCOUNT_MARGIN_MODE`:
    - `ACCOUNT_MARGIN_MODE_RETAIL_NETTING`: OTC, "only one position can exist for one symbol";
    - `ACCOUNT_MARGIN_MODE_EXCHANGE`;
    - `ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`.

    (same page)

## 3. Symbols and prices

11. `symbol_info(symbol)` returns a named tuple, or None. (mt5symbolinfo_py) Fields include:
    - `visible`, `digits`, `spread`, `trade_calc_mode`, `trade_mode`, `trade_stops_level`, `trade_freeze_level`, `trade_exemode`;
    - `filling_mode`, `order_mode`, `swap_mode`;
    - `bid`, `ask`, `point`, `trade_tick_value`, `trade_tick_size`, `trade_contract_size`;
    - `volume_min`, `volume_max`, `volume_step`, `volume_limit`;
    - `swap_long`, `swap_short`, `margin_initial`;
    - `currency_base`, `currency_profit`, `currency_margin`, `description`, `name`.
12. If a symbol is not visible in Market Watch, `symbol_select(symbol, True)` adds it. (order_send example, mt5symbolselect_py)
13. `symbol_info_tick(symbol)` returns the last tick, or None. Fields: `time`, `bid`, `ask`, `last`, `volume`, `time_msc`, `flags`, `volume_real`. (mt5symbolinfotick_py)

## 4. Sending and checking orders

14. `order_send(request)`: "Send a request to perform a trading operation from the terminal to the trade server." It returns the result as `MqlTradeResult`; error info comes from `last_error()`. (mt5ordersend_py)
15. **Request fields** (`MqlTradeRequest`): `action`, `magic`, `order`, `symbol`, `volume` (lots), `price`, `stoplimit`, `sl`, `tp`, `deviation` (points), `type`, `type_filling`, `type_time`, `expiration`, `comment`, `position`, `position_by`. (mt5ordersend_py)
    - `magic`: "Expert Advisor ID … Each Expert Advisor can set its own unique ID when sending a trade request". (`/en/docs/constants/structures/mqltraderequest`)
    - `comment`: "Order comment". No length is stated on these pages, so a comment is not relied on to identify an order.
    - `position`: "Position ticket. Fill it when changing and closing a position for its clear identification." (mt5ordersend_py)
16. **Closing a position** (documented example): `action=TRADE_ACTION_DEAL`, `type=ORDER_TYPE_SELL` for a long, `position=<ticket>`, `price=bid`, `deviation`, `type_filling`. (mt5ordersend_py) In the hedging system the position ticket must be specified; in netting it can be, "though a position is identified by the symbol name". (mqltraderequest)
17. **Changing stop-loss/take-profit** of an open position: `TRADE_ACTION_SLTP` with `action`, `symbol`, `sl`, `tp`, `position`. (mqltraderequest)
18. **Result fields:** `retcode`, `deal`, `order`, `volume`, `price`, `bid`, `ask`, `comment`, `request_id`, `retcode_external`, `request`. The documented example's successful open has `retcode=10009` and `comment=Request executed`. (mt5ordersend_py)
19. `OrderSend` returning true "is not a sign of successful execution of a trade operation". The `retcode` (and `retcode_external`) of the result must be checked. Each accepted order is stored on the trade server awaiting processing. (`/en/docs/trading/ordersend`)
20. `order_check(request)` checks funds sufficiency. Its result fields: `retcode`, `balance`, `equity`, `profit`, `margin`, `margin_free`, `margin_level`, `comment`, `request`. (mt5ordercheck_py)
21. **Trade server return codes** (`/en/docs/constants/errorswarnings/enum_trade_return_codes`):

    | Code | Name | Meaning |
    |---|---|---|
    | 10004 | REQUOTE | Requote |
    | 10006 | REJECT | Request rejected |
    | 10007 | CANCEL | Request canceled by trader |
    | 10008 | PLACED | Order placed |
    | 10009 | DONE | Request completed |
    | 10010 | DONE_PARTIAL | Only part of the request was completed |
    | 10011 | ERROR | Request processing error |
    | 10012 | TIMEOUT | Request canceled by timeout |
    | 10013 | INVALID | Invalid request |
    | 10014 | INVALID_VOLUME | Invalid volume in the request |
    | 10015 | INVALID_PRICE | Invalid price in the request |
    | 10016 | INVALID_STOPS | Invalid stops in the request |
    | 10017 | TRADE_DISABLED | Trade is disabled |
    | 10018 | MARKET_CLOSED | Market is closed |
    | 10019 | NO_MONEY | Not enough money |
    | 10020 | PRICE_CHANGED | Prices changed |
    | 10021 | PRICE_OFF | No quotes to process the request |
    | 10022 | INVALID_EXPIRATION | Invalid order expiration date |
    | 10023 | ORDER_CHANGED | Order state changed |
    | 10024 | TOO_MANY_REQUESTS | Too frequent requests |
    | 10025 | NO_CHANGES | No changes in request |
    | 10026 | SERVER_DISABLES_AT | Autotrading disabled by server |
    | 10027 | CLIENT_DISABLES_AT | Autotrading disabled by client terminal |
    | 10028 | LOCKED | Request locked for processing |
    | 10029 | FROZEN | Order or position frozen |
    | 10030 | INVALID_FILL | Invalid order filling type |
    | 10031 | CONNECTION | No connection with the trade server |
    | 10032 | ONLY_REAL | Only for live accounts |
    | 10033 | LIMIT_ORDERS | Pending orders limit reached |
    | 10034 | LIMIT_VOLUME | Volume limit for the symbol reached |
    | 10035 | INVALID_ORDER | Incorrect or prohibited order type |
    | 10036 | POSITION_CLOSED | Position already closed |
    | 10038 | INVALID_CLOSE_VOLUME | Close volume exceeds the position volume |
    | 10039 | CLOSE_ORDER_EXIST | A close order already exists for the position |
    | 10040 | LIMIT_POSITIONS | Open positions limit reached |
    | 10041 | REJECT_CANCEL | Pending activation rejected, order canceled |
    | 10042 | LONG_ONLY | Only long positions allowed |
    | 10043 | SHORT_ONLY | Only short positions allowed |
    | 10044 | CLOSE_ONLY | Only closing allowed |
    | 10045 | FIFO_CLOSE | FIFO rule |
    | 10046 | HEDGE_PROHIBITED | Opposite positions prohibited |

## 5. Orders, positions, history

22. `orders_get()` returns active (pending) orders, filterable by `symbol`, `group` or `ticket`. (mt5ordersget_py) Fields shown include `ticket`, `time_setup_msc`, `type`, `type_time`, `type_filling`, `state`, `magic`, `volume_current`, `price_open`, `sl`, `tp`, `price_current`, `symbol`, `comment`, `external_id`.
23. `positions_get()` returns open positions, filterable by `symbol`, `group` (comma-separated masks with `*`) or `ticket`. (mt5positionsget_py) Fields shown include `ticket`, `time`, `type`, `magic`, `identifier`, `reason`, `volume`, `price_open`, `sl`, `tp`, `price_current`, `swap`, `profit`, `symbol`, `comment`.
24. `history_orders_get(date_from, date_to, group=…)`, `(ticket=…)` or `(position=…)` (mt5historyordersget_py). Fields include `ticket`, `time_setup_msc`, `time_done_msc`, `type`, `type_filling`, `magic`, `position_id`, `volume_initial`, `price_open`, `price_current`, `symbol`, `comment`, `external_id`.
25. `history_deals_get(date_from, date_to, group=…)`, `(ticket=<order ticket>)` (all deals with that order in `DEAL_ORDER`) or `(position=<position ticket>)` (mt5historydealsget_py). Fields: `ticket`, `order`, `time`, `time_msc`, `type`, `entry`, `magic`, `position_id`, `reason`, `volume`, `price`, `commission`, `swap`, `profit`, `fee`, `symbol`, `comment`, `external_id`.
26. `ORDER_POSITION_ID` is the position identifier set to an order when it is executed. `POSITION_IDENTIFIER` finds the order that opened a position. `DEAL_POSITION_ID` is assigned to all deals of a position. `DEAL_MAGIC` is the deal magic number (see `ORDER_MAGIC`). (`/en/docs/constants/tradingconstants/orderproperties`, `…/positionproperties`, `…/dealproperties`)
27. `ENUM_DEAL_ENTRY`: `DEAL_ENTRY_IN`, `DEAL_ENTRY_OUT`, `DEAL_ENTRY_INOUT` (reverse), `DEAL_ENTRY_OUT_BY`. `ENUM_DEAL_TYPE` includes `DEAL_TYPE_BUY`, `DEAL_TYPE_SELL`, `DEAL_TYPE_BALANCE`, `DEAL_TYPE_CREDIT`, `DEAL_TYPE_CHARGE`, `DEAL_TYPE_CORRECTION`, `DEAL_TYPE_BONUS` and commission types. (dealproperties)

## 6. Design consequences (ours, from the facts above)

- **Demo only:** the bridge checks `account_info().trade_mode == ACCOUNT_TRADE_MODE_DEMO` before every trading request and refuses otherwise (fact 9).
- **Idempotency:** every request carries `magic` = a 63-bit number derived from our client order ID, plus the ID in `comment` for people. Before sending, and after any uncertain outcome (no result, `10012`, `10031`, `10011`), the bridge looks the order up by `magic` in `orders_get` / `history_orders_get` / `history_deals_get`, so it never sends twice (facts 15, 22–26).
- **Executed vs accepted:** only `retcode` decides (fact 19). `10009`/`10010` are fills, `10008` is placed, the validation codes are rejections, and anything else is unknown until looked up.
- **Buy only:**
  - Sells are sent only with `position=<ticket>` of an existing long, and never for more than its volume (facts 16, 21: `10038`).
  - Protective stops are the position's `sl` set via `TRADE_ACTION_SLTP` (fact 17), so a separate stop order can never open a short.
- **CFD accounting:** positions are in lots × `trade_contract_size`. P&L, `swap`, `commission` and `fee` come from the deals (fact 25). Margin and equity come from `account_info` (fact 8).

## Not documented here (do not assume)

- The maximum length of `comment`, and whether brokers keep it unchanged.
- Whether `order_send` is safe to call from several threads, or with several terminals at once.
- HFM's symbol names, contract sizes, trading hours, swap and commission values: read from the terminal at run time.
