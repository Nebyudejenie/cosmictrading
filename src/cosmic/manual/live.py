"""The owner's real HFM account as terminal 3 reports it (read-only), checked before it is shown.

The account is LIVE-CONNECTED only when every one of these holds; otherwise its state says
which failed, and ``LiveAccount.checks`` lists each with what was seen (S114):
- terminal 3 reported within ``connections.FRESH`` and is signed in to an account;
- HFM's own ``account_info`` on that terminal says REAL (never the owner's claim), from HF Markets;
- it is the account Broker Connections verified REAL on that terminal (the same fingerprint: the
  login and the server);
- read-only twice over: HFM refuses orders on this sign-in (``trade_allowed`` false: the
  investor password), and the terminal itself says it refuses every trading command (bridge 13);
  the server has no trading route to terminal 3 at all (its slot has no adapter).

Quotes are fresh only within ``QUOTE_FRESH`` of now, and only while the bridge's measure of HFM's
server clock agrees with HFM's rule (New York + 7 hours): a wrong offset would make an old price
look new. Without a fresh quote nothing that needs the price is calculated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema_trading import bridge_state
from cosmic.market import hfm_candles
from cosmic.trading import connections
from cosmic.trading.brokers.mt5_bridge import BRIDGE_REAL, SLOTS
from cosmic.trading.mt5_brokers import Mt5Broker, broker_of, names

SYMBOL = "XAUUSD"
QUOTE_FRESH = timedelta(seconds=15)
CLOCK_TOLERANCE_MS = 60_000  # the bridge's offset against HFM's rule (whole quarters apart)
BROKER = "HF Markets"  # account_info.company starts with it on every HFM entity
GOLD = "XAUUSD"  # a broker's gold symbol starts with it (Exness may add a suffix: XAUUSDm, …)
CONNECTED, STALE, NOT_LIVE, NOT_LINKED, NO_TERMINAL, SIGNED_OUT, NOT_READ_ONLY, OFFLINE = (
    "CONNECTED",
    "STALE",
    "NOT_LIVE",
    "NOT_LINKED",
    "NO_TERMINAL",
    "SIGNED_OUT",
    "NOT_READ_ONLY",
    "OFFLINE",
)
LABELS = {
    CONNECTED: "LIVE-CONNECTED · READ-ONLY",
    STALE: "NOT CURRENT",
    NOT_LIVE: "NOT A REAL HFM OR EXNESS ACCOUNT",
    NOT_LINKED: "NOT VERIFIED",
    NO_TERMINAL: "NOT CONNECTED",
    SIGNED_OUT: "SIGNED OUT",
    NOT_READ_ONLY: "NOT READ-ONLY",
    OFFLINE: "NOT CONNECTED TO THE BROKER",
}
# MT5's position and deal codes (MetaTrader 5 documentation: ENUM_POSITION_TYPE, ENUM_DEAL_TYPE,
# ENUM_DEAL_ENTRY).
BUY, SELL = "BUY", "SELL"
DEAL_BUY, DEAL_SELL = 0, 1
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3


def dec(value: Any) -> Decimal | None:
    """A reported number as Decimal (MT5 reports floats; the bridge passes them as reported)."""
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


@dataclass(frozen=True)
class Spec:
    """HFM's own contract for the symbol on this account (never assumed: as MT5 reports it)."""

    symbol: str
    contract_size: Decimal
    tick_size: Decimal
    tick_value: Decimal  # account currency, for one lot and one tick
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal
    digits: int
    stops_level: int  # in points: the nearest a stop may be to the price
    point: Decimal
    swap_long: Decimal | None
    swap_short: Decimal | None
    currency_profit: str | None
    currency_margin: str | None
    swap_mode: int | None = None  # MT5's ENUM_SYMBOL_SWAP_MODE (1: points)
    swap_rollover3days: int | None = None  # the night charged three times (0 Sunday ... 6)
    # Bridge 13 (S115): the rest of HFM's terms, None from an older bridge (never assumed).
    freeze_level: int | None = None  # in points: no order or stop changed this near the price
    calc_mode: int | None = None  # MT5's ENUM_SYMBOL_CALC_MODE (0: FOREX, contract x price / lev)
    margin_buy_1lot: Decimal | None = None  # HFM's own margin of 1 lot, at margin_buy_at
    margin_buy_at: Decimal | None = None
    margin_sell_1lot: Decimal | None = None
    margin_sell_at: Decimal | None = None
    profit_1lot_1: Decimal | None = None  # HFM's own profit of 1 lot BUY for a 1.00 rise

    def margin_per_lot(self, side: str, price: Decimal) -> Decimal | None:
        """HFM's own margin for one lot at ``price``, scaled from its figure at the price it was
        computed at (the margin of a gold CFD is proportional to the price); None when HFM's
        calculator has not been read."""
        figure, at = (
            (self.margin_buy_1lot, self.margin_buy_at)
            if side == BUY
            else (self.margin_sell_1lot, self.margin_sell_at)
        )
        if figure is None or not at or at <= 0 or figure <= 0:
            return None
        return figure * price / at

    @property
    def value_per_price(self) -> Decimal:
        """Account currency for one lot when the price moves by 1.00."""
        return self.tick_value / self.tick_size


def spec_of(raw: dict[str, Any] | None, symbol: str = SYMBOL) -> Spec | None:
    """The reported contract, or None when any figure needed for sizing is missing."""
    if not raw:
        return None
    need = {
        k: dec(raw.get(k))
        for k in (
            "contract_size",
            "tick_size",
            "tick_value",
            "volume_min",
            "volume_step",
            "volume_max",
        )
    }
    if any(v is None or v <= 0 for v in need.values()):
        return None
    point = dec(raw.get("point")) or need["tick_size"]
    assert point is not None
    return Spec(
        symbol=symbol,
        contract_size=need["contract_size"],  # type: ignore[arg-type]
        tick_size=need["tick_size"],  # type: ignore[arg-type]
        tick_value=need["tick_value"],  # type: ignore[arg-type]
        volume_min=need["volume_min"],  # type: ignore[arg-type]
        volume_step=need["volume_step"],  # type: ignore[arg-type]
        volume_max=need["volume_max"],  # type: ignore[arg-type]
        digits=int(raw.get("digits") or 2),
        stops_level=int(raw.get("stops_level") or 0),
        point=point,
        swap_long=dec(raw.get("swap_long")),
        swap_short=dec(raw.get("swap_short")),
        currency_profit=raw.get("currency_profit"),
        currency_margin=raw.get("currency_margin"),
        swap_mode=_int(raw.get("swap_mode")),
        swap_rollover3days=_int(raw.get("swap_rollover3days")),
        freeze_level=_int(raw.get("freeze_level")),
        calc_mode=_int(raw.get("calc_mode")),
        margin_buy_1lot=dec(raw.get("margin_buy_1lot")),
        margin_buy_at=dec(raw.get("margin_buy_at")),
        margin_sell_1lot=dec(raw.get("margin_sell_1lot")),
        margin_sell_at=dec(raw.get("margin_sell_at")),
        profit_1lot_1=dec(raw.get("profit_1lot_1")),
    )


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    at: datetime  # UTC
    age: timedelta
    clock_ok: bool = True  # the bridge's measure of HFM's clock agrees with HFM's rule

    @property
    def fresh(self) -> bool:
        return self.clock_ok and self.age <= QUOTE_FRESH

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def mid(self) -> Decimal:
        return (self.ask + self.bid) / 2


@dataclass(frozen=True)
class Position:
    ticket: int
    side: str
    lots: Decimal
    price_open: Decimal
    sl: Decimal | None
    tp: Decimal | None
    price_now: Decimal | None
    profit: Decimal
    swap: Decimal
    opened_at: datetime | None
    magic: int
    comment: str


@dataclass(frozen=True)
class Deal:
    ticket: int
    position_id: int
    at: datetime
    server_at: datetime  # the broker's clock, for its trading day
    side: str | None  # BUY / SELL; None for a balance operation
    entry: int | None
    lots: Decimal
    price: Decimal
    profit: Decimal
    commission: Decimal
    swap: Decimal
    fee: Decimal
    magic: int

    @property
    def net(self) -> Decimal:
        return self.profit + self.commission + self.swap + self.fee


@dataclass(frozen=True)
class Check:
    """One condition of the live connection, as seen now: passed, failed or not known yet."""

    name: str
    ok: bool | None
    seen: str


@dataclass(frozen=True)
class LiveAccount:
    state: str
    problem: str | None
    reported_at: datetime | None = None
    nickname: str | None = None
    fingerprint: str | None = None
    trade_mode: str | None = None
    server: str | None = None
    company: str | None = None
    currency: str | None = None
    leverage: int | None = None
    balance: Decimal | None = None
    equity: Decimal | None = None
    margin: Decimal | None = None
    margin_free: Decimal | None = None
    margin_level: Decimal | None = None
    stop_out: Decimal | None = None  # bridge 13: HFM closes positions itself at this level
    margin_call: Decimal | None = None
    stop_out_mode: str | None = None  # PERCENT (of margin level) or MONEY
    broker: str | None = None  # S115: HFM or Exness, from MT5's own account_info.company
    symbol: str = SYMBOL  # the account's own gold symbol (XAUUSD, XAUUSDm, …)
    read_only: bool = True
    spec: Spec | None = None
    quote: Quote | None = None
    server_offset: timedelta | None = None
    positions: tuple[Position, ...] = field(default_factory=tuple)
    deals: tuple[Deal, ...] = field(default_factory=tuple)
    checks: tuple[Check, ...] = ()

    @property
    def live(self) -> bool:
        """HFM's own report, fresh, of the verified REAL account, read-only twice over."""
        return self.state == CONNECTED

    @property
    def label(self) -> str:
        return LABELS.get(self.state, self.state)

    @property
    def floating(self) -> Decimal | None:
        if self.equity is None or self.balance is None:
            return None
        return self.equity - self.balance

    @property
    def priced(self) -> bool:
        """Whether figures that need the price now may be calculated."""
        return self.live and self.quote is not None and self.quote.fresh

    def server_day(self, now: datetime) -> datetime:
        """The start of the broker's trading day containing ``now``, in UTC."""
        offset = self.server_offset or timedelta(0)
        local = now + offset
        return local.replace(hour=0, minute=0, second=0, microsecond=0) - offset


def clock_ok(
    offset_ms: Any,
    now: datetime,
    broker: Mt5Broker | None = None,
    confirmed: Any = None,
) -> bool:
    """Whether the bridge's measure of the broker's server clock can be trusted. HFM (and an
    unknown broker): it must be HFM's own rule, New York + 7 hours. A broker without a rule
    checked here (Exness, S115): a whole quarter hour within 14 hours, and confirmed by the
    bridge from a price that has just arrived (bridge 13), never from a closed market's."""
    try:
        offset = int(offset_ms)
    except (TypeError, ValueError):
        return False
    if broker is None or broker.published_clock:
        return abs(offset - hfm_candles.server_offset_ms(now)) <= CLOCK_TOLERANCE_MS
    return confirmed is True and offset % 900_000 == 0 and abs(offset) <= 14 * 3_600_000


def gold_symbol(snap: dict[str, Any]) -> str:
    """The account's own gold symbol as its terminal reports it: XAUUSD when it exists, else a
    name with only an account-type letter after it (Exness's XAUUSDm, XAUUSDc), the shortest
    first, before any other (S117: Exness also lists XAUUSD247m, another product, which sorts
    first); XAUUSD when none is reported."""
    names = [str(n) for n in (snap.get("symbols") or {}) if str(n).startswith(GOLD)]
    if SYMBOL in names or not names:
        return SYMBOL
    plain = sorted((n for n in names if n[len(GOLD) :].isalpha()), key=lambda n: (len(n), n))
    return plain[0] if plain else sorted(names)[0]


def _quote(snap: dict[str, Any], now: datetime, broker: Mt5Broker | None = None) -> Quote | None:
    offset_ms = snap.get("server_utc_offset_ms")
    offset = timedelta(milliseconds=int(offset_ms)) if offset_ms is not None else None
    tick = (snap.get("ticks") or {}).get(gold_symbol(snap)) or {}
    bid, ask, at = dec(tick.get("bid")), dec(tick.get("ask")), _utc(tick.get("time_msc"), offset)
    if not (bid and ask and at is not None and ask >= bid > 0):
        return None
    ok = clock_ok(offset_ms, now, broker, snap.get("server_utc_offset_confirmed"))
    return Quote(bid, ask, at, max(now - at, timedelta(0)), ok)


def _utc(ms: Any, offset: timedelta | None) -> datetime | None:
    try:
        stamp = datetime.fromtimestamp(int(ms) / 1000, UTC)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp - (offset or timedelta(0))


def _positions(
    rows: list[dict[str, Any]], offset: timedelta | None, symbol: str = SYMBOL
) -> tuple[Position, ...]:
    out = []
    for p in rows:
        if p.get("symbol") != symbol:
            continue
        sl, tp = dec(p.get("sl")), dec(p.get("tp"))
        out.append(
            Position(
                ticket=int(p.get("ticket") or 0),
                side=BUY if int(p.get("type") or 0) == DEAL_BUY else SELL,
                lots=dec(p.get("volume")) or Decimal(0),
                price_open=dec(p.get("price_open")) or Decimal(0),
                sl=sl if sl else None,  # MT5 reports 0.0 for none
                tp=tp if tp else None,
                price_now=dec(p.get("price_current")),
                profit=dec(p.get("profit")) or Decimal(0),
                swap=dec(p.get("swap")) or Decimal(0),
                opened_at=_utc(p.get("time_msc"), offset),
                magic=int(p.get("magic") or 0),
                comment=str(p.get("comment") or "")[:64],
            )
        )
    return tuple(out)


def _deals(
    rows: list[dict[str, Any]], offset: timedelta | None, symbol: str = SYMBOL
) -> tuple[Deal, ...]:
    out = []
    for d in rows:
        at = _utc(d.get("time_msc"), offset)
        if at is None:
            continue
        raw_type: Any = d.get("type")
        kind = int(raw_type) if raw_type is not None else -1
        trade = kind in (DEAL_BUY, DEAL_SELL)
        if trade and d.get("symbol") != symbol:
            continue
        out.append(
            Deal(
                ticket=int(d.get("ticket") or 0),
                position_id=int(d.get("position_id") or 0),
                at=at,
                server_at=at + (offset or timedelta(0)),
                side=(BUY if kind == DEAL_BUY else SELL) if trade else None,
                entry=int(d["entry"]) if d.get("entry") is not None else None,
                lots=dec(d.get("volume")) or Decimal(0),
                price=dec(d.get("price")) or Decimal(0),
                profit=dec(d.get("profit")) or Decimal(0),
                commission=dec(d.get("commission")) or Decimal(0),
                swap=dec(d.get("swap")) or Decimal(0),
                fee=dec(d.get("fee")) or Decimal(0),
                magic=int(d.get("magic") or 0),
            )
        )
    return tuple(sorted(out, key=lambda x: (x.at, x.ticket)))


def _no_route() -> Check:
    slot = SLOTS[BRIDGE_REAL]
    return Check(
        "The server has no way to send an order to terminal 3",
        slot.adapter is None,
        "its slot has no trading connection; only sign-in and identity commands go there"
        if slot.adapter is None
        else f"its slot names {slot.adapter}",
    )


async def read(conn: AsyncConnection, now: datetime) -> LiveAccount:
    """The real account now, or why it cannot be shown as live, with every check as seen."""
    linked = await connections.on_slot(conn, BRIDGE_REAL)
    row = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == BRIDGE_REAL
            )
        )
    ).first()
    link_seen = (
        f"“{linked.nickname}”, verified {linked.verified_type or 'not yet'}"
        if linked is not None
        else "no account linked to terminal 3 in Broker Connections"
    )
    if row is None:
        why = (
            "terminal 3 (the real account's read-only terminal) has never reported: it is not "
            "installed on the bridge computer yet"
        )
        return LiveAccount(
            NO_TERMINAL,
            why,
            nickname=linked.nickname if linked else None,
            checks=(
                Check("Terminal 3 reports to the server", False, "never reported"),
                Check("Linked and verified in Broker Connections", None, link_seen),
                _no_route(),
            ),
        )
    snap: dict[str, Any] = row.snapshot or {}
    account: dict[str, Any] = dict(snap.get("account") or {})
    terminal: dict[str, Any] = dict(snap.get("terminal") or {})
    offset_ms = snap.get("server_utc_offset_ms")
    offset = timedelta(milliseconds=int(offset_ms)) if offset_ms is not None else None
    fingerprint = account.get("fingerprint")
    company = str(account.get("company") or "")
    age = int((now - row.received_at).total_seconds())
    current = now - row.received_at <= connections.FRESH
    broker = broker_of(company)
    who = broker.name if broker is not None else "the broker"
    gold = gold_symbol(snap)
    confirmed = snap.get("server_utc_offset_confirmed")
    real = account.get("trade_mode") == "REAL"
    verified = (
        linked is not None and linked.verified_type == "REAL" and linked.fingerprint == fingerprint
    )
    # S117 follow-up (2026-11-24): a terminal whose sign-in the broker refused stays "signed in"
    # to the account it had, offline, and offline nothing is allowed to trade. Only a connected
    # terminal's "trading not allowed" is the broker's own word on the sign-in.
    online = terminal.get("connected") is True
    investor = online and account.get("trade_allowed") is False
    refuses = terminal.get("read_only") is True
    clock = clock_ok(offset_ms, now, broker, confirmed)
    if broker is None or broker.published_clock:
        clock_seen = "HFM's clock is New York + 7 hours" + (
            "" if clock else f"; the bridge measured {offset_ms} ms: prices treated as old"
        )
    else:
        hours = int(offset_ms) / 3_600_000 if offset_ms is not None else None
        clock_seen = (
            f"{who} publishes no clock rule checked here: measured by the bridge from a price "
            f"that had just arrived, UTC{hours:+g}"
            if clock and hours is not None
            else f"{who}: not yet measured from a price that has just arrived (the market is "
            "closed, or an older bridge): prices treated as old"
        )
    checks = (
        Check("Terminal 3 reports to the server", True, f"last report {age} s ago"),
        Check(
            f"Its report is current (under {int(connections.FRESH.total_seconds())} s old)",
            current,
            f"{age} s old",
        ),
        Check(
            "MT5 is signed in to an account",
            bool(fingerprint),
            "signed in" if fingerprint else "signed in to no account",
        ),
        Check(
            f"The broker is {names()}",
            (broker is not None) if fingerprint else None,
            f"{company or 'no company reported'}, server {account.get('server') or '—'}",
        ),
        Check(
            f"{who.capitalize() if broker is None else who} reports a REAL account",
            real if fingerprint else None,
            f"{who} says {account.get('trade_mode') or 'nothing'}",
        ),
        Check(
            "It is the account you verified in Broker Connections (login and server)",
            verified if fingerprint else None,
            link_seen
            + (
                ""
                if linked is None or linked.fingerprint == fingerprint
                else "; a different account is signed in"
            ),
        ),
        Check(
            f"MT5 is connected to {who}'s server",
            online if fingerprint else None,
            "connected"
            if online
            else f"not connected: its last sign-in was refused (a wrong password or server) or "
            f"the network is down, so what it shows is from its last connection to {who}",
        ),
        Check(
            f"Investor (read-only) sign-in: {who} itself refuses orders on it",
            investor if fingerprint and online else None,
            f"{who} reports trading not allowed on this sign-in"
            if investor
            else f"not confirmed: MT5 is not connected to {who}'s server"
            if not online
            else f"{who} reports trading ALLOWED: the trading password was used",
        ),
        Check(
            "The terminal refuses every trading command",
            refuses,
            "the bridge reports read-only"
            if refuses
            else 'the bridge does not report read-only (it needs bridge 13 with "read_only": true)',
        ),
        _no_route(),
        Check(f"Prices: the bridge's clock agrees with {who}'s server time", clock, clock_seen),
    )
    base: dict[str, Any] = {
        "reported_at": row.received_at,
        "nickname": linked.nickname if linked else None,
        "fingerprint": fingerprint,
        "trade_mode": account.get("trade_mode"),
        "server": account.get("server"),
        "company": company or None,
        "currency": account.get("currency"),
        "leverage": int(account["leverage"]) if account.get("leverage") else None,
        "balance": dec(account.get("balance")),
        "equity": dec(account.get("equity")),
        "margin": dec(account.get("margin")),
        "margin_free": dec(account.get("margin_free")),
        "margin_level": dec(account.get("margin_level")),
        "stop_out": dec(account.get("margin_so_so")),
        "margin_call": dec(account.get("margin_so_call")),
        "stop_out_mode": account.get("margin_so_mode"),
        "read_only": investor and refuses,
        "spec": spec_of((snap.get("symbols") or {}).get(gold), gold),
        "server_offset": offset,
        "positions": _positions(list(snap.get("positions") or []), offset, gold),
        "deals": _deals(list(snap.get("deals") or []), offset, gold),
        "quote": _quote(snap, now, broker),
        "checks": checks,
        "broker": broker.name if broker is not None else None,
        "symbol": gold,
    }
    if not fingerprint:
        return LiveAccount(SIGNED_OUT, "terminal 3 runs but MT5 is signed in to no account", **base)
    if not current:
        return LiveAccount(
            STALE,
            f"terminal 3 last reported {age} s ago: the bridge or MT5 is not running, so "
            "nothing here is current",
            **base,
        )
    if not real or broker is None:
        return LiveAccount(
            NOT_LIVE,
            f"terminal 3 holds a {account.get('trade_mode') or 'unknown'} account of "
            f"{company or 'an unknown broker'}, not a real {names()} one: it is never shown "
            "as live",
            **base,
        )
    if not verified:
        return LiveAccount(
            NOT_LINKED,
            "the real account signed in on terminal 3 is not the one verified in Broker "
            "Connections: link it there (with its investor password, read-only)",
            **base,
        )
    if not online:
        return LiveAccount(
            OFFLINE,
            f"terminal 3 is signed in but not connected to {who}'s server: its last sign-in "
            "was refused (a wrong password or server) or the network is down, so nothing here "
            "is current; sign it in again with the investor (read-only) password",
            **base,
        )
    if not investor:
        return LiveAccount(
            NOT_READ_ONLY,
            "terminal 3 is signed in with the account's trading password: sign in with the "
            "investor (read-only) password instead; until then it is not shown as connected",
            **base,
        )
    if not refuses:
        return LiveAccount(
            NOT_READ_ONLY,
            "terminal 3's bridge does not say it refuses trading commands: it needs bridge 13 "
            'with "read_only": true',
            **base,
        )
    return LiveAccount(CONNECTED, None, **base)


@dataclass(frozen=True)
class Preview:
    """HFM's gold contract and price from the demo terminal (terminal 1), for a PREVIEW of the
    calculator while the live account is not connected. Never shown as the live account's."""

    spec: Spec | None
    quote: Quote | None
    leverage: int | None
    deals: tuple[Deal, ...] = ()  # the demo's own, for its commission per lot
    reported_at: datetime | None = None
    currency: str | None = None


async def demo_contract(conn: AsyncConnection, now: datetime) -> Preview:
    from cosmic.trading.brokers.mt5_bridge import BRIDGE

    row = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == BRIDGE
            )
        )
    ).first()
    snap: dict[str, Any] = (row.snapshot or {}) if row is not None else {}
    leverage: Any = (snap.get("account") or {}).get("leverage")
    return Preview(
        spec_of((snap.get("symbols") or {}).get(SYMBOL)),
        _quote(snap, now),
        int(leverage) if leverage else None,
        _deals(list(snap.get("deals") or []), None),
        row.received_at if row is not None else None,
        (snap.get("account") or {}).get("currency"),
    )
