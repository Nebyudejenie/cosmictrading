"""In-process stand-in for the official ``MetaTrader5`` package and a hedging MT5 account.

Constants carry the values read from MetaTrader5 5.0.6231 on 2026-10-15 (docs/integrations/
hfm-mt5-demo.md); behaviour follows docs/integrations/mt5-python-api.md:
- every buy deal opens its own position (hedging); a sell names ``position`` and closes it;
- market deals fill at once at the ask/bid; pending BUY_LIMIT orders fill when the ask reaches
  them; a position's ``sl`` closes it at the bid when touched (deal reason SL);
- a buy may carry ``sl`` (the official ``order_send`` example sends one with a market deal): it
  must lie below the bid (market) or the order price (pending), else INVALID_STOPS, and the new
  position holds it from the fill;
- commission is charged per deal and swap at close, both into the balance, as MT5 reports them.
It is a test double, not a claim about undocumented broker behaviour (for example, which magic a
stop-out deal carries: here the position's).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any

ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_EXCHANGE = 0, 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP = 1, 5, 6
TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE, TRADE_ACTION_CLOSE_BY = 7, 8, 10
ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
ORDER_TIME_GTC = 0
ORDER_STATE_PLACED, ORDER_STATE_CANCELED, ORDER_STATE_FILLED = 1, 2, 4
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
DEAL_REASON_CLIENT, DEAL_REASON_EXPERT, DEAL_REASON_SL = 0, 3, 4
DEAL_REASON_TP = 5  # the position's take-profit closed it at the broker
TRADE_RETCODE_TRADE_DISABLED = 10017  # trading is not allowed on this sign-in (investor password)
DEAL_REASON_MOBILE = 1  # a deal made from the phone app
TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL = 10008, 10009, 10010
TRADE_RETCODE_TIMEOUT, TRADE_RETCODE_INVALID_PRICE, TRADE_RETCODE_INVALID_STOPS = (
    10012,
    10015,
    10016,
)
TRADE_RETCODE_NO_MONEY, TRADE_RETCODE_NO_CHANGES, TRADE_RETCODE_POSITION_CLOSED = (
    10019,
    10025,
    10036,
)
TRADE_RETCODE_INVALID_CLOSE_VOLUME = 10038
TRADE_RETCODE_INVALID_VOLUME = 10014  # HFM's answer to a volume below the symbol's minimum
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15 = 1, 5, 15
TIMEFRAME_H1, TIMEFRAME_D1 = 16385, 16408  # MetaTrader5's own values
TRADE_RETCODE_CLIENT_DISABLES_AT = 10027  # Algo Trading is switched off in the terminal
SYMBOL_TRADE_MODE_FULL = 4

SPECS = {
    "XAUUSD": {"contract": D(100), "digits": 2, "tick": D("0.01"), "vmax": D(100)},
    "#BTCUSD": {"contract": D(1), "digits": 3, "tick": D("0.001"), "vmax": D(200)},
}
COMMISSION_PER_LOT = D("-3")  # per deal side, in USD (a test assumption)


def _f(x: D) -> float:
    return float(x)


@dataclass
class Position:
    ticket: int
    symbol: str
    volume: D
    price_open: D
    magic: int
    comment: str
    time_msc: int
    sl: D = D(0)
    tp: D = D(0)
    swap: D = D(0)
    type: int = POSITION_TYPE_BUY  # a short (SELL) since bridge version 5

    @property
    def long(self) -> bool:
        return self.type == POSITION_TYPE_BUY

    def view(self, mark: D, contract: D) -> SimpleNamespace:
        move = mark - self.price_open if self.long else self.price_open - mark
        return SimpleNamespace(
            ticket=self.ticket,
            time_msc=self.time_msc,
            type=self.type,
            magic=self.magic,
            identifier=self.ticket,
            volume=_f(self.volume),
            price_open=_f(self.price_open),
            sl=_f(self.sl),
            tp=_f(self.tp),
            price_current=_f(mark),
            swap=_f(self.swap),
            profit=_f(move * self.volume * contract),
            symbol=self.symbol,
            comment=self.comment,
        )


@dataclass
class MT5Emulator:
    trade_mode: int = ACCOUNT_TRADE_MODE_DEMO
    currency: str = "USD"  # a Cent account reports USC
    number: int = 12345678  # the account's login; another is another account (fingerprint)
    server: str = "HFMarketsGlobal-Demo"
    # Sign-in (S83): accounts HFM knows, by (login, server): password, investor password, mode,
    # currency and balance. The account above is added with password "demo-pass" at start.
    known: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    signed_in: bool = True  # False: MT5 runs signed out (after a restart without saved login)
    running: bool = True  # False: the MT5 program is closed (no IPC)
    investor: bool = False  # signed in with the read-only (investor) password
    login_delay_fault: str = ""  # "timeout": the next sign-in answers -10005
    balance: D = D(100_000)
    now_ms: int = 1_791_900_000_000
    book: dict[str, tuple[D, D]] = field(
        default_factory=lambda: {
            "XAUUSD": (D("4100.00"), D("4100.30")),
            "#BTCUSD": (D("82990.000"), D("83000.000")),
        }
    )
    connected: bool = True
    algo_trading: bool = True  # the terminal's Algo Trading button
    # A broker may tag the deal of a stop-out with magic 0, whoever opened the position (S59).
    sl_deal_magic_zero: bool = False
    server_offset_ms: int = 0  # broker server time minus UTC (HFM: +3 h on 2026-10-15)
    # HFM's real server answered a placed BUY_LIMIT with 10009 DONE and deal 0 (2026-10-15);
    # the documentation's 10008 PLACED is kept as an option.
    pending_retcode: int = TRADE_RETCODE_DONE
    faults: list[str] = field(default_factory=list)  # "none" | "timeout_after"
    positions: dict[int, Position] = field(default_factory=dict)
    orders: dict[int, dict[str, Any]] = field(default_factory=dict)  # pending
    history: list[dict[str, Any]] = field(default_factory=list)
    deals: list[dict[str, Any]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    # HFM's candles by (symbol, timeframe), oldest first, in server time (version 4's RATES)
    candles: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._tickets = itertools.count(5_000_001)
        for name, value in globals().items():
            if name.isupper():
                setattr(self, name, value)
        self.known.setdefault(
            (self.number, self.server),
            {
                "password": "demo-pass",
                "investor": "demo-read",
                "trade_mode": self.trade_mode,
                "currency": self.currency,
                "balance": self.balance,
            },
        )
        self.saved: dict[tuple[int, str], str] = {(self.number, self.server): "demo-pass"}
        self._books: dict[tuple[int, str], dict[str, Any]] = {}
        self._error: tuple[int, str] = (1, "Success")
        self.logins: list[tuple[int, str]] = []  # every sign-in attempt (never the password)

    # ---- signing in (S83) ---------------------------------------------------------------

    def add_account(
        self,
        login: int,
        password: str,
        *,
        server: str = "HFMarketsGlobal-Demo",
        investor: str = "read-only",
        trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
        currency: str = "USD",
        balance: str = "100",
    ) -> None:
        self.known[(login, server)] = {
            "password": password,
            "investor": investor,
            "trade_mode": trade_mode,
            "currency": currency,
            "balance": D(balance),
        }

    def _book(self) -> dict[str, Any]:
        return {
            "positions": self.positions,
            "orders": self.orders,
            "history": self.history,
            "deals": self.deals,
            "balance": self.balance,
        }

    def login(self, login: int, password: str = "", server: str = "", timeout: int = 60000) -> bool:
        """MT5's ``login``: a wrong password or server is refused (-6) and leaves MT5 signed out;
        without a password the one MT5 saved for that account is used."""
        self.logins.append((login, server))
        if not self.running:
            self._error = (-10004, "No IPC connection")
            return False
        if self.login_delay_fault == "timeout":
            self.login_delay_fault = ""
            self._error = (-10005, "IPC timeout")
            return False
        key = (login, server)
        profile = self.known.get(key)
        secret = password or self.saved.get(key, "")
        if profile is None or secret not in (profile["password"], profile["investor"]):
            self.signed_in = False
            self._error = (-6, "Terminal: Authorization failed")
            return False
        old = (self.number, self.server)
        if old != key:
            self._books[old] = self._book()
            book = self._books.pop(key, None) or {
                "positions": {},
                "orders": {},
                "history": [],
                "deals": [],
                "balance": profile["balance"],
            }
            self.positions, self.orders = book["positions"], book["orders"]
            self.history, self.deals, self.balance = book["history"], book["deals"], book["balance"]
            self.number, self.server = login, server
            self.trade_mode, self.currency = profile["trade_mode"], profile["currency"]
        self.investor = secret == profile["investor"]
        self.saved[key] = secret
        self.signed_in = True
        self._error = (1, "Success")
        return True

    def restart(self, *, signed_in: bool = True) -> None:
        """MT5 closed and opened again: it signs in with its saved login unless told not to."""
        self.running = True
        self.signed_in = signed_in

    def positions_total(self) -> int:
        return len(self.positions) if self.signed_in else 0

    def orders_total(self) -> int:
        return len(self.orders) if self.signed_in else 0

    # ---- connection and info ------------------------------------------------------------

    def initialize(self, path: str | None = None, timeout: int = 60000) -> bool:
        return self.connected and self.running

    def shutdown(self) -> None:
        return None

    def last_error(self) -> tuple[int, str]:
        return self._error

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            connected=self.connected,
            trade_allowed=self.algo_trading,
            tradeapi_disabled=False,
            build=6264,
        )

    def account_info(self) -> SimpleNamespace | None:
        if not self.signed_in:
            return None
        return SimpleNamespace(
            login=self.number,
            trade_mode=self.trade_mode,
            margin_mode=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
            currency=self.currency,
            leverage=1000,
            balance=_f(self.balance),
            equity=_f(self.balance + sum(self._profit(p) for p in self.positions.values())),
            margin=0.0,
            margin_free=_f(self.balance),
            server=self.server,
            company="HF Markets",
            trade_allowed=not self.investor,
            margin_level=0.0,
        )

    def symbol_select(self, name: str, enable: bool) -> bool:
        return name in SPECS

    def symbol_info(self, name: str) -> SimpleNamespace | None:
        s = SPECS.get(name)
        if s is None:
            return None
        bid, ask = self.book[name]
        return SimpleNamespace(
            name=name,
            trade_contract_size=_f(s["contract"]),
            volume_min=0.01,
            volume_step=0.01,
            volume_max=_f(s["vmax"]),
            digits=s["digits"],
            trade_tick_size=_f(s["tick"]),
            trade_mode=SYMBOL_TRADE_MODE_FULL,
            filling_mode=1,
            trade_exemode=2,
            trade_stops_level=0,
            swap_long=-78.56,
            swap_short=0.0,
            swap_mode=1,  # points
            swap_rollover3days=3,  # Wednesday
            point=10.0 ** -s["digits"],
            currency_profit="USD",
            trade_tick_value=_f(s["contract"] * s["tick"]),
            currency_margin=name[:3].lstrip("#"),
            bid=_f(bid),
            ask=_f(ask),
        )

    def symbol_info_tick(self, name: str) -> SimpleNamespace | None:
        if name not in self.book:
            return None
        bid, ask = self.book[name]
        return SimpleNamespace(
            bid=_f(bid), ask=_f(ask), last=0.0, time_msc=self.now_ms + self.server_offset_ms
        )

    def positions_get(self, symbol: str | None = None) -> tuple[SimpleNamespace, ...]:
        return tuple(
            p.view(self.book[p.symbol][0], SPECS[p.symbol]["contract"])
            for p in self.positions.values()
            if symbol is None or p.symbol == symbol
        )

    def orders_get(self) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(**o) for o in self.orders.values())

    def history_orders_get(self, start: datetime, end: datetime) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(**o) for o in self.history)

    def history_deals_get(self, start: datetime, end: datetime) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(**d) for d in self.deals)

    # ---- trading ------------------------------------------------------------------------

    def _profit(self, p: Position) -> D:
        return (self.book[p.symbol][0] - p.price_open) * p.volume * SPECS[p.symbol]["contract"]

    def _result(self, code: int, **kw: Any) -> SimpleNamespace:
        base = {"retcode": code, "deal": 0, "order": 0, "volume": 0.0, "price": 0.0}
        base.update(kw)
        base.setdefault("comment", "Request executed" if code == TRADE_RETCODE_DONE else "")
        return SimpleNamespace(**base)

    def order_send(self, request: dict[str, Any]) -> SimpleNamespace | None:
        self.sent.append(dict(request))
        fault = self.faults.pop(0) if self.faults else None
        if fault == "none":
            return None  # lost: nothing happened
        if not self.algo_trading:
            return self._result(TRADE_RETCODE_CLIENT_DISABLES_AT)
        if self.investor or not self.signed_in:
            return self._result(TRADE_RETCODE_TRADE_DISABLED)
        result = self._execute(request)
        if fault == "timeout_after":
            return self._result(TRADE_RETCODE_TIMEOUT)  # it happened, but the answer was lost
        return result

    def _execute(self, r: dict[str, Any]) -> SimpleNamespace:
        action = r["action"]
        if action == TRADE_ACTION_SLTP:
            pos = self.positions.get(r["position"])
            if pos is None:
                return self._result(TRADE_RETCODE_POSITION_CLOSED)
            sl = D(repr(r.get("sl", 0.0)))
            tp = D(repr(r["tp"])) if "tp" in r else pos.tp
            bid, ask = self.book[pos.symbol]
            if sl and ((pos.long and sl >= bid) or (not pos.long and sl <= ask)):
                return self._result(TRADE_RETCODE_INVALID_STOPS)
            if sl == pos.sl and tp == pos.tp:
                return self._result(TRADE_RETCODE_NO_CHANGES)
            pos.sl, pos.tp = sl, tp
            return self._result(TRADE_RETCODE_DONE)
        if action == TRADE_ACTION_REMOVE:
            order = self.orders.pop(r["order"], None)
            if order is None:
                return self._result(10013)
            self.history.append(
                {**order, "state": ORDER_STATE_CANCELED, "time_done_msc": self.now_ms}
            )
            return self._result(TRADE_RETCODE_DONE, order=order["ticket"])
        symbol = r["symbol"]
        volume = D(repr(r["volume"]))
        if volume < D("0.01"):  # every symbol's volume_min here
            return self._result(TRADE_RETCODE_INVALID_VOLUME)
        bid, ask = self.book[symbol]
        sl = D(repr(r.get("sl", 0.0)))
        if action == TRADE_ACTION_PENDING and r["type"] == ORDER_TYPE_BUY_LIMIT:
            price = D(repr(r["price"]))
            if price >= ask:
                return self._result(TRADE_RETCODE_INVALID_PRICE)
            if sl and sl >= price:
                return self._result(TRADE_RETCODE_INVALID_STOPS)
            ticket = next(self._tickets)
            self.orders[ticket] = {
                "ticket": ticket,
                "time_setup_msc": self.now_ms,
                "time_done_msc": 0,
                "type": ORDER_TYPE_BUY_LIMIT,
                "state": ORDER_STATE_PLACED,
                "magic": r["magic"],
                "position_id": 0,
                "volume_initial": _f(volume),
                "volume_current": _f(volume),
                "price_open": _f(price),
                "sl": _f(sl),
                "tp": 0.0,
                "symbol": symbol,
                "comment": r.get("comment", ""),
            }
            return self._result(self.pending_retcode, order=ticket, volume=_f(volume))
        if action == TRADE_ACTION_DEAL and r.get("position"):  # a close, by its ticket
            pos = self.positions.get(r["position"])
            if pos is None:
                return self._result(TRADE_RETCODE_POSITION_CLOSED)
            if (r["type"] == ORDER_TYPE_SELL) != pos.long:  # a sell closes a long, a buy a short
                return self._result(TRADE_RETCODE_INVALID_CLOSE_VOLUME)
            if volume > pos.volume:
                return self._result(TRADE_RETCODE_INVALID_CLOSE_VOLUME)
            price = bid if pos.long else ask
            return self._close(
                pos, volume, price, r["magic"], r.get("comment", ""), DEAL_REASON_EXPERT
            )
        if action == TRADE_ACTION_DEAL and r["type"] in (ORDER_TYPE_BUY, ORDER_TYPE_SELL):
            buy = r["type"] == ORDER_TYPE_BUY
            cost = (ask if buy else bid) * volume * SPECS[symbol]["contract"]
            if cost > self.balance * 1000:
                return self._result(TRADE_RETCODE_NO_MONEY)
            if sl and ((buy and sl >= bid) or (not buy and sl <= ask)):
                return self._result(TRADE_RETCODE_INVALID_STOPS)
            tp = D(repr(r.get("tp", 0.0)))
            if tp and ((buy and tp <= ask) or (not buy and tp >= bid)):
                return self._result(TRADE_RETCODE_INVALID_STOPS)
            result = self._buy(
                symbol,
                volume,
                ask if buy else bid,
                r["magic"],
                r.get("comment", ""),
                None,
                sl,
                side=POSITION_TYPE_BUY if buy else POSITION_TYPE_SELL,
            )
            if tp:
                self.positions[result.order].tp = tp
                self.history[-1]["tp"] = _f(tp)
            return result
        return self._result(10013)

    def _deal(self, **kw: Any) -> dict[str, Any]:
        deal = {
            "ticket": next(self._tickets),
            "time_msc": self.now_ms,
            "commission": 0.0,
            "swap": 0.0,
            "profit": 0.0,
            "fee": 0.0,
            "comment": "",
            "reason": DEAL_REASON_EXPERT,
        }
        deal.update(kw)
        self.deals.append(deal)
        return deal

    def _buy(
        self,
        symbol: str,
        volume: D,
        price: D,
        magic: int,
        comment: str,
        order: int | None,
        sl: D = D(0),
        *,
        side: int = POSITION_TYPE_BUY,
    ) -> SimpleNamespace:
        """A new position: a long, or a short (``side`` SELL) at the bid."""
        order_ticket = order or next(self._tickets)
        commission = COMMISSION_PER_LOT * volume
        self.balance += commission
        pos = Position(
            order_ticket, symbol, volume, price, magic, comment, self.now_ms, sl, type=side
        )
        self.positions[pos.ticket] = pos
        deal = self._deal(
            order=order_ticket,
            type=DEAL_TYPE_BUY if side == POSITION_TYPE_BUY else DEAL_TYPE_SELL,
            entry=DEAL_ENTRY_IN,
            magic=magic,
            position_id=pos.ticket,
            volume=_f(volume),
            price=_f(price),
            commission=_f(commission),
            symbol=symbol,
            comment=comment,
        )
        self.history.append(
            {
                "ticket": order_ticket,
                "time_setup_msc": self.now_ms,
                "time_done_msc": self.now_ms,
                "type": ORDER_TYPE_BUY if side == POSITION_TYPE_BUY else ORDER_TYPE_SELL,
                "state": ORDER_STATE_FILLED,
                "magic": magic,
                "position_id": pos.ticket,
                "volume_initial": _f(volume),
                "volume_current": 0.0,
                "price_open": _f(price),
                "sl": _f(sl),
                "tp": 0.0,
                "symbol": symbol,
                "comment": comment,
            }
        )
        return self._result(
            TRADE_RETCODE_DONE,
            deal=deal["ticket"],
            order=order_ticket,
            volume=_f(volume),
            price=_f(price),
        )

    def _close(
        self, pos: Position, volume: D, price: D, magic: int, comment: str, reason: int
    ) -> SimpleNamespace:
        contract = SPECS[pos.symbol]["contract"]
        move = price - pos.price_open if pos.long else pos.price_open - price
        profit = move * volume * contract
        commission = COMMISSION_PER_LOT * volume
        swap = pos.swap * volume / pos.volume
        self.balance += profit + commission + swap
        order_ticket = next(self._tickets)
        deal = self._deal(
            order=order_ticket,
            type=DEAL_TYPE_SELL if pos.long else DEAL_TYPE_BUY,
            entry=DEAL_ENTRY_OUT,
            magic=magic,
            position_id=pos.ticket,
            volume=_f(volume),
            price=_f(price),
            commission=_f(commission),
            swap=_f(swap),
            profit=_f(profit),
            symbol=pos.symbol,
            comment=comment,
            reason=reason,
        )
        pos.swap -= swap
        pos.volume -= volume
        if pos.volume <= 0:
            del self.positions[pos.ticket]
        return self._result(
            TRADE_RETCODE_DONE,
            deal=deal["ticket"],
            order=order_ticket,
            volume=_f(volume),
            price=_f(price),
        )

    # ---- the market moves ---------------------------------------------------------------

    def move(self, symbol: str, bid: str, ask: str) -> None:
        self.now_ms += 1000
        self.book[symbol] = (D(bid), D(ask))
        b, a = self.book[symbol]
        for ticket, o in list(self.orders.items()):
            if o["symbol"] == symbol and a <= D(repr(o["price_open"])):
                del self.orders[ticket]
                volume, sl = D(repr(o["volume_initial"])), D(repr(o["sl"]))
                self._buy(symbol, volume, a, o["magic"], o["comment"], ticket, sl)
                self.history[-1]["time_setup_msc"] = o["time_setup_msc"]
        for pos in list(self.positions.values()):
            if pos.symbol != symbol:
                continue
            if pos.sl and (b <= pos.sl if pos.long else a >= pos.sl):  # a short's stop is above
                magic = 0 if self.sl_deal_magic_zero else pos.magic
                self._close(pos, pos.volume, b if pos.long else a, magic, "[sl]", DEAL_REASON_SL)
            elif pos.tp and (b >= pos.tp if pos.long else a <= pos.tp):  # HFM's take-profit
                self._close(
                    pos, pos.volume, b if pos.long else a, pos.magic, "[tp]", DEAL_REASON_TP
                )

    def by_hand(self, side: str, lots: str, *, position: int = 0, sl: str = "") -> int:
        """A trade the owner makes in the MT5 app: no magic number. Returns the position."""
        request: dict[str, Any] = {
            "action": TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "volume": float(lots),
            "type": ORDER_TYPE_BUY if side == "BUY" else ORDER_TYPE_SELL,
            "magic": 0,
            "comment": "",
        }
        if position:
            request["position"] = position
        if sl:
            request["sl"] = float(sl)
        before, deals = set(self.positions), len(self.deals)
        result = self._execute(request)
        assert result.retcode == TRADE_RETCODE_DONE, result
        for d in self.deals[deals:]:
            d["reason"] = DEAL_REASON_MOBILE  # as HFM marks a deal made from the phone
        return position or (set(self.positions) - before).pop()

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start: int, count: int
    ) -> list[dict[str, Any]]:
        rows = self.candles.get((symbol, timeframe), [])
        return rows[max(0, len(rows) - start - count) : len(rows) - start]

    def copy_rates_from(
        self, symbol: str, timeframe: int, date_from: int, count: int
    ) -> list[dict[str, Any]]:
        """MT5's: the ``count`` candles opened at or before ``date_from`` (server time)."""
        rows = [r for r in self.candles.get((symbol, timeframe), []) if r["time"] <= date_from]
        return rows[-count:] if count else []

    def rollover(self, symbol: str, per_lot: str) -> None:
        """Overnight swap accrues on open positions (realised at close)."""
        for pos in self.positions.values():
            if pos.symbol == symbol:
                pos.swap += D(per_lot) * pos.volume


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = ["MT5Emulator", "replace"]
