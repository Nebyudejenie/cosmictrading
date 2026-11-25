"""Manual Trading's test on the owner's Exness DEMO (S119, ADR-042).

The owner's choice of 2026-11-25: the portal's BUY and SELL are tested on their Exness demo,
signed in on terminal 3, before any real trade is ever sent from the portal. This reads that
demo as its own bridge reports it (terminal 3's second bridge, bound to that one demo) and works
out the plan's trade on it: the same PDF plan (risk a share of the balance, reward 1:2), in the
demo's own prices, contract and balance. It reads and works out; the trade itself is proposed by
``cosmic.trading.exness_demo_trade`` and approved by the owner with their code.

Virtual money: every figure is the DEMO's and is labelled so; never the real account's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema_trading import bridge_state
from cosmic.manual.calc import Line, Sizing, Ticket, size
from cosmic.manual.live import Quote, Spec, _quote, dec, gold_symbol, spec_of
from cosmic.trading.brokers.mt5_bridge import BRIDGE_EXNESS
from cosmic.trading.mt5_brokers import broker_of

FRESH = timedelta(seconds=15)
STOP_ATRS = Decimal(2)  # the rule's usual stop: twice the 15-minute ATR
FALLBACK_STOP = Decimal("0.0025")  # of the price, when the ATR is not known yet


@dataclass(frozen=True)
class DemoAccount:
    """The Exness demo as its own bridge last reported it (virtual money)."""

    reported_at: datetime | None
    fresh: bool
    trade_mode: str | None = None
    company: str | None = None
    currency: str | None = None
    leverage: int | None = None
    balance: Decimal | None = None
    equity: Decimal | None = None
    margin: Decimal | None = None
    margin_free: Decimal | None = None
    stop_out: Decimal | None = None
    stop_out_mode: str | None = None
    symbol: str = "XAUUSD"
    spec: Spec | None = None
    quote: Quote | None = None
    positions: int = 0

    @property
    def usable(self) -> bool:
        return (
            self.fresh
            and self.trade_mode == "DEMO"
            and self.spec is not None
            and self.quote is not None
            and self.quote.fresh
            and self.balance is not None
        )

    @property
    def base(self) -> Decimal | None:
        """What a trade's risk is a share of: the balance, or the equity when lower."""
        held = [x for x in (self.balance, self.equity) if x is not None]
        return min(held) if held else None


@dataclass(frozen=True)
class DemoTrade:
    side: str
    ticket: Ticket
    sizing: Sizing | None
    contract: Decimal  # ounces a lot, as the demo reports it (100 at Exness)
    lines: tuple[Line, ...] = field(default_factory=tuple)
    why: str = ""

    @property
    def blocked(self) -> bool:
        return self.sizing is None or self.sizing.blocked


async def read(conn: AsyncConnection, now: datetime) -> DemoAccount | None:
    """The demo bridge's last report, or None when it never reported."""
    row = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == BRIDGE_EXNESS
            )
        )
    ).first()
    if row is None:
        return None
    snap: dict[str, Any] = row.snapshot or {}
    a = snap.get("account") or {}
    symbol = gold_symbol(snap)
    leverage = a.get("leverage")
    return DemoAccount(
        reported_at=row.received_at,
        fresh=now - row.received_at <= FRESH,
        trade_mode=a.get("trade_mode"),
        company=a.get("company"),
        currency=a.get("currency"),
        leverage=int(leverage) if leverage else None,
        balance=dec(a.get("balance")),
        equity=dec(a.get("equity")),
        margin=dec(a.get("margin")),
        margin_free=dec(a.get("margin_free")),
        stop_out=dec(a.get("margin_so_so")),
        stop_out_mode=a.get("margin_so_mode"),
        symbol=symbol,
        spec=spec_of((snap.get("symbols") or {}).get(symbol), symbol),
        quote=_quote(snap, now, broker_of(a.get("company"))),  # Exness's own clock rule
        positions=int(a.get("positions_total") or 0),
    )


def _round(value: Decimal, tick: Decimal) -> Decimal:
    step = tick.normalize()
    return ((value / step).to_integral_value(ROUND_HALF_UP) * step).quantize(step)


def work_out(
    demo: DemoAccount,
    side: str,
    *,
    rule_side: str | None,
    rule_entry: Decimal | None,
    rule_stop: Decimal | None,
    atr: Decimal | None,
    rr: Decimal,
    risk_pct: Decimal,
    max_risk_pct: Decimal,
) -> DemoTrade | None:
    """The plan's trade on the demo for ``side``: the setup's own stop distance when the setup
    is on that side, else the rule's usual stop (twice the 15-minute ATR); entered at the demo's
    own price, the target at ``rr`` times the stop. None when it cannot be worked out."""
    if side not in ("BUY", "SELL") or not demo.usable:
        return None
    assert demo.spec is not None and demo.quote is not None and demo.base is not None
    if rule_side == side and rule_entry is not None and rule_stop is not None:
        distance = abs(rule_entry - rule_stop)
        why = "The stop is the setup's own, as the rule sets it now."
    elif atr is not None and atr > 0:
        distance = STOP_ATRS * atr
        why = (
            "No setup on this side now: the stop is the rule's usual one (twice the 15-minute "
            "ATR); a test of the order path, not a recommendation."
        )
    else:
        price = demo.quote.ask if side == "BUY" else demo.quote.bid
        distance = price * FALLBACK_STOP
        why = (
            "No setup on this side, and gold's 15-minute volatility is not known yet: a stop "
            f"{FALLBACK_STOP * 100:.2f} % from the price; a test of the order path, not a "
            "recommendation."
        )
    tick = demo.spec.tick_size
    entry = demo.quote.ask if side == "BUY" else demo.quote.bid
    sign = 1 if side == "BUY" else -1
    stop = _round(entry - sign * distance, tick)
    target = _round(entry + sign * rr * distance, tick)
    ticket = Ticket(side, entry, stop, target, risk_pct=risk_pct, note="Exness demo")
    sizing = size(
        ticket,
        equity=demo.base,
        margin_free=demo.margin_free,
        leverage=demo.leverage,
        currency=demo.currency,
        spec=demo.spec,
        quote=demo.quote,
        max_risk_pct=max_risk_pct,
        atr=atr,
        margin_used=demo.margin,
        stop_out_pct=demo.stop_out if demo.stop_out_mode == "PERCENT" else None,
        broker="Exness (demo)",
    )
    return DemoTrade(side, ticket, sizing, demo.spec.contract_size, sizing.lines, why)
