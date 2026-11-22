"""The owner's real HFM account as terminal 3 reports it (read-only), checked before it is shown.

The account is LIVE only when every one of these holds; otherwise its state says which failed:
- terminal 3 reported within ``connections.FRESH``;
- HFM's own ``account_info`` on that terminal says REAL (never the owner's claim);
- it is the account Broker Connections verified REAL on that terminal (the same fingerprint).

Quotes are fresh only within ``QUOTE_FRESH`` of now (HFM's server clock, converted with the
offset the bridge measured); without a fresh quote nothing that needs the price is calculated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema_trading import bridge_state
from cosmic.trading import connections
from cosmic.trading.brokers.mt5_bridge import BRIDGE_REAL

SYMBOL = "XAUUSD"
QUOTE_FRESH = timedelta(seconds=15)
CONNECTED, STALE, NOT_LIVE, NOT_LINKED, NO_TERMINAL, SIGNED_OUT = (
    "CONNECTED",
    "STALE",
    "NOT_LIVE",
    "NOT_LINKED",
    "NO_TERMINAL",
    "SIGNED_OUT",
)
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
    )


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    at: datetime  # UTC
    age: timedelta

    @property
    def fresh(self) -> bool:
        return self.age <= QUOTE_FRESH

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
    read_only: bool = True
    spec: Spec | None = None
    quote: Quote | None = None
    server_offset: timedelta | None = None
    positions: tuple[Position, ...] = field(default_factory=tuple)
    deals: tuple[Deal, ...] = field(default_factory=tuple)

    @property
    def live(self) -> bool:
        """HFM's own report, fresh, of the verified REAL account."""
        return self.state == CONNECTED

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


def _utc(ms: Any, offset: timedelta | None) -> datetime | None:
    try:
        stamp = datetime.fromtimestamp(int(ms) / 1000, UTC)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp - (offset or timedelta(0))


def _positions(rows: list[dict[str, Any]], offset: timedelta | None) -> tuple[Position, ...]:
    out = []
    for p in rows:
        if p.get("symbol") != SYMBOL:
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


def _deals(rows: list[dict[str, Any]], offset: timedelta | None) -> tuple[Deal, ...]:
    out = []
    for d in rows:
        at = _utc(d.get("time_msc"), offset)
        if at is None:
            continue
        raw_type: Any = d.get("type")
        kind = int(raw_type) if raw_type is not None else -1
        trade = kind in (DEAL_BUY, DEAL_SELL)
        if trade and d.get("symbol") != SYMBOL:
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


async def read(conn: AsyncConnection, now: datetime) -> LiveAccount:
    """The real account now, or why it cannot be shown as live."""
    linked = await connections.on_slot(conn, BRIDGE_REAL)
    row = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == BRIDGE_REAL
            )
        )
    ).first()
    if row is None:
        return LiveAccount(
            NO_TERMINAL,
            "terminal 3 (the real account's read-only terminal) has never reported: it is not "
            "installed on the bridge computer yet",
            nickname=linked.nickname if linked else None,
        )
    snap: dict[str, Any] = row.snapshot or {}
    account: dict[str, Any] = dict(snap.get("account") or {})
    offset_ms = snap.get("server_utc_offset_ms")
    offset = timedelta(milliseconds=int(offset_ms)) if offset_ms is not None else None
    base = {
        "reported_at": row.received_at,
        "nickname": linked.nickname if linked else None,
        "fingerprint": account.get("fingerprint"),
        "trade_mode": account.get("trade_mode"),
        "server": account.get("server"),
        "company": account.get("company"),
        "currency": account.get("currency"),
        "leverage": int(account["leverage"]) if account.get("leverage") else None,
        "balance": dec(account.get("balance")),
        "equity": dec(account.get("equity")),
        "margin": dec(account.get("margin")),
        "margin_free": dec(account.get("margin_free")),
        "margin_level": dec(account.get("margin_level")),
        "read_only": account.get("trade_allowed") is not True,
        "spec": spec_of((snap.get("symbols") or {}).get(SYMBOL)),
        "server_offset": offset,
        "positions": _positions(list(snap.get("positions") or []), offset),
        "deals": _deals(list(snap.get("deals") or []), offset),
    }
    tick = (snap.get("ticks") or {}).get(SYMBOL) or {}
    bid, ask, at = dec(tick.get("bid")), dec(tick.get("ask")), _utc(tick.get("time_msc"), offset)
    if bid and ask and at is not None and ask >= bid > 0:
        base["quote"] = Quote(bid, ask, at, max(now - at, timedelta(0)))
    if not account.get("fingerprint"):
        return LiveAccount(SIGNED_OUT, "terminal 3 runs but MT5 is signed in to no account", **base)
    if now - row.received_at > connections.FRESH:
        age = int((now - row.received_at).total_seconds())
        return LiveAccount(
            STALE,
            f"terminal 3 last reported {age} s ago: the bridge or MT5 is not running, so "
            "nothing here is current",
            **base,
        )
    if account.get("trade_mode") != "REAL":
        return LiveAccount(
            NOT_LIVE,
            f"HFM reports a {account.get('trade_mode') or 'unknown'} account on terminal 3, "
            "not a real one: it is never shown as live",
            **base,
        )
    if (
        linked is None
        or linked.verified_type != "REAL"
        or linked.fingerprint != account.get("fingerprint")
    ):
        return LiveAccount(
            NOT_LINKED,
            "the real account signed in on terminal 3 is not the one verified in Broker "
            "Connections: link it there (with its investor password, read-only)",
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


async def demo_contract(conn: AsyncConnection, now: datetime) -> Preview:
    from cosmic.trading.brokers.mt5_bridge import BRIDGE

    row = (
        await conn.execute(select(bridge_state.c.snapshot).where(bridge_state.c.bridge == BRIDGE))
    ).first()
    snap: dict[str, Any] = (row.snapshot or {}) if row is not None else {}
    offset_ms = snap.get("server_utc_offset_ms")
    offset = timedelta(milliseconds=int(offset_ms)) if offset_ms is not None else None
    tick = (snap.get("ticks") or {}).get(SYMBOL) or {}
    bid, ask, at = dec(tick.get("bid")), dec(tick.get("ask")), _utc(tick.get("time_msc"), offset)
    quote = (
        Quote(bid, ask, at, max(now - at, timedelta(0)))
        if bid and ask and at is not None and ask >= bid > 0
        else None
    )
    leverage: Any = (snap.get("account") or {}).get("leverage")
    return Preview(
        spec_of((snap.get("symbols") or {}).get(SYMBOL)),
        quote,
        int(leverage) if leverage else None,
    )
