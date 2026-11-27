"""A trade on the owner's Exness DEMO from the Manual Trading page (S119, ADR-042).

The owner's choice of 2026-11-25: before any real trade is ever sent from the portal, its BUY and
SELL are tested on their Exness demo, signed in on terminal 3 and traded through that terminal's
second bridge, which works only with that one demo account (bridge 15). Virtual money only.

The trade goes the way every trade goes:

1. worked out by the Manual Trading page from the owner's PDF plan (side, lots, stop-loss,
   take-profit) and proposed here, checked against the risk policy;
2. approved by the owner with their code (nothing is sent before);
3. sent through the demo bridge with its stop-loss and take-profit, both held by Exness;
4. closed by Exness at either, or by an exit the owner approves the same way;
5. reconciled against Exness's own balance.

It only proposes. DEMO only: the adapter refuses a report from a REAL account or another account
(``mt5_bridge.report_problem``); the bridge refuses every command while any account but its own
demo is signed in. The real account's own bridge (``BRIDGE_REAL``) is never involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.core.errors import ValidationFailedError
from cosmic.core.labels import DataLabel
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_trading import accounts, bridge_state
from cosmic.market import data as md
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.brokers.hfm_trade_check import session_closed
from cosmic.trading.mandates import IN_FLIGHT
from cosmic.trading.orders import LIVE_AT_VENUE, OrderStatus
from cosmic.trading.proposals import Candidate, ProposalResult, create_candidate

GOLD = "CFD:XAU/USD@EXNESS"
MARKUP = Decimal("0.001")  # a marketable limit 0.1 % past the price, as the test trade's
LOCK = "exness-demo-trade"  # one proposal at a time, however often a button is pressed
TTL = timedelta(minutes=5)  # a setup's price is minutes old at most when approved
OPEN_AHEAD = timedelta(minutes=30)
# S123: a pending order waits at Exness for one of these many hours, then it is cancelled.
PENDING_HOURS = (1, 4, 8)
MAX_AWAY = Decimal("0.03")  # a level further than 3 % from the price is a typing slip
SESSION_STEP = timedelta(minutes=15)
# What a pending order does, by its side and kind, in the owner's words.
WAITS = {
    ("BUY", "LIMIT"): "buy when gold falls to",
    ("BUY", "STOP"): "buy when gold rises to",
    ("SELL", "LIMIT"): "sell when gold rises to",
    ("SELL", "STOP"): "sell when gold falls to",
}


@dataclass(frozen=True)
class DemoTicket:
    """The trade the page worked out, in the demo's own prices and lots (never from a form)."""

    side: str  # BUY or SELL
    ounces: Decimal  # lots × the contract (100 ounces a lot at Exness)
    lots: Decimal
    stop: Decimal
    target: Decimal
    risk_cash: Decimal  # lost at the stop, before the spread
    why: str  # where its side and stop come from
    level: Decimal | None = None  # S123: a pending order's level; None: at the market now
    hours: int | None = None  # S123: how long a pending order waits at Exness


def _tick(value: Decimal, tick: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    step = tick.normalize()
    return ((value / step).to_integral_value(rounding) * step).quantize(step)


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"


def band(
    side: str, bid: Decimal, ask: Decimal, entry: Decimal, stop: Decimal, tick: Decimal
) -> tuple[Decimal, Decimal]:
    """The range gold's mid may move in between the proposal and the send (R10 checks it at
    both): MARKUP either way of the price, never past halfway to the stop-loss.

    2026-11-27: a BUY's range began at the bid, so a dip of half the spread (gold fell about
    1 USD in the 14 s before the owner approved) held it back, though a lower price is better
    for a BUY. The far side is the order's own limit, as before."""
    if side == "BUY":
        low = max(
            _tick(bid * (1 - MARKUP), tick, ROUND_UP), _tick((bid + stop) / 2, tick, ROUND_UP)
        )
        return low, entry
    high = min(
        _tick(ask * (1 + MARKUP), tick, ROUND_DOWN), _tick((ask + stop) / 2, tick, ROUND_DOWN)
    )
    return entry, high


def pending_kind(side: str, level: Decimal, bid: Decimal, ask: Decimal) -> str | None:
    """What a pending order at ``level`` is (S123): LIMIT, when it waits for the price to come
    back to a better level (a BUY below the price, a SELL above it); STOP, when it waits for the
    price to go on to it (a BUY above the price, a SELL below it). None within a spread of the
    price: that is a trade at the market."""
    spread = ask - bid
    market = ask if side == "BUY" else bid
    if level <= market - spread:
        return "LIMIT" if side == "BUY" else "STOP"
    if level >= market + spread:
        return "STOP" if side == "BUY" else "LIMIT"
    return None


def pending_band(
    side: str,
    kind: str,
    level: Decimal,
    bid: Decimal,
    ask: Decimal,
    stop: Decimal,
    tick: Decimal,
) -> tuple[Decimal, Decimal]:
    """The range gold's mid may be in when a pending order is sent (R10, at the proposal and
    at the send): from the price now (MARKUP beyond it) to the order's level. A LIMIT's level
    side reaches MARKUP past the level, never past halfway to its stop-loss: there the broker
    fills it at once, at the level or better. A STOP's ends at its level: past it the bridge
    places nothing."""
    up, down = ROUND_UP, ROUND_DOWN
    if side == "BUY" and kind == "LIMIT":
        low = max(_tick(level * (1 - MARKUP), tick, up), _tick((level + stop) / 2, tick, up))
        return low, _tick(ask * (1 + MARKUP), tick, down)
    if side == "SELL" and kind == "LIMIT":
        high = min(_tick(level * (1 + MARKUP), tick, down), _tick((level + stop) / 2, tick, down))
        return _tick(bid * (1 - MARKUP), tick, up), high
    if side == "BUY":
        return _tick(bid * (1 - MARKUP), tick, up), level
    return level, _tick(ask * (1 + MARKUP), tick, down)


def session_ends(now: datetime, until: datetime) -> str | None:
    """Why gold closes before ``until`` (None when it stays open): a pending order never waits
    across a daily break or the weekend, where it could fill at a gap."""
    t = now
    while t <= until:
        closed = session_closed(t)
        if closed is not None:
            return closed.replace("HFM's", "the")
        t += SESSION_STEP
    return None


async def account_alias(conn: AsyncConnection) -> str | None:
    """The open SANDBOX account on the Exness demo adapter, if one was opened."""
    alias: str | None = await conn.scalar(
        select(accounts.c.alias).where(
            accounts.c.kind == "SANDBOX",
            accounts.c.broker_adapter == mt5_bridge.ADAPTER_EXNESS,
            accounts.c.status == "ACTIVE",
        )
    )
    return alias


async def refusal(conn: AsyncConnection, now: datetime) -> str | None:
    """Why a demo trade cannot be proposed now, in words; None when it can. The page offers the
    buttons on the same answer, and the action asks it again."""
    alias = await account_alias(conn)
    if alias is None:
        return "your Exness demo is not set up here yet"
    row = (
        await conn.execute(
            select(bridge_state).where(bridge_state.c.bridge == mt5_bridge.BRIDGE_EXNESS)
        )
    ).first()
    if row is None:
        return "the Exness demo's bridge has never reported"
    snap = row.snapshot or {}
    problem = mt5_bridge.report_problem(
        snap, row.received_at, now, await mt5_bridge.opened_on(conn, mt5_bridge.ADAPTER_EXNESS)
    ) or mt5_bridge.trading_off(snap)
    if problem is not None:
        return problem.replace("HFM", "Exness")
    closed = session_closed(now) or session_closed(now + OPEN_AHEAD)
    if closed is not None:
        return closed.replace("HFM's", "the").replace("closes soon", "closes within the hour")
    account_id = await conn.scalar(select(accounts.c.id).where(accounts.c.alias == alias))
    busy = await conn.scalar(
        select(func.count())
        .select_from(trade_intents)
        .where(trade_intents.c.account_id == account_id, trade_intents.c.status.in_(IN_FLIGHT))
    )
    if busy:
        return (
            "a trade on the Exness demo waits for your decision, is being sent, or waits at "
            "Exness as a pending order: finish or cancel it first"
        )
    return None


@dataclass(frozen=True)
class PendingOrder:
    """A pending order of the demo's, as the page lists it (S123)."""

    id: str
    trade_code: str
    side: str
    waits: str  # "buy when gold rises to", in the owner's words
    level: Decimal
    stop: Decimal | None
    target: Decimal | None
    ounces: Decimal
    until: datetime | None
    status: str

    @property
    def cancellable(self) -> bool:
        """Waiting at Exness, so a cancel can be sent (one already sent is CANCEL_PENDING)."""
        return self.status in (OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED)


async def pending_orders(conn: AsyncConnection) -> list[PendingOrder]:
    """The demo's pending orders that Exness may still hold: on their way, waiting at Exness,
    or being cancelled (newest first)."""
    alias = await account_alias(conn)
    if alias is None:
        return []
    rows = await conn.execute(
        select(
            orders_t.c.id,
            orders_t.c.side,
            orders_t.c.order_type,
            orders_t.c.limit_price,
            orders_t.c.quantity,
            orders_t.c.expires_at,
            orders_t.c.status,
            trade_intents.c.trade_code,
            trade_intents.c.management_plan,
        )
        .join(trade_intents, trade_intents.c.id == orders_t.c.intent_id)
        .join(accounts, accounts.c.id == orders_t.c.account_id)
        .where(
            accounts.c.alias == alias,
            orders_t.c.purpose == "ENTRY",
            orders_t.c.time_in_force == "GTD",
            orders_t.c.status.in_([s.value for s in LIVE_AT_VENUE]),
        )
        .order_by(orders_t.c.created_at.desc())
    )
    out = []
    for r in rows:
        plan = r.management_plan or {}
        stop = (plan.get("stop_loss") or {}).get("stop_price")
        target = plan.get("take_profit_price")
        kind = "STOP" if r.order_type == "STOP_LIMIT" else "LIMIT"
        out.append(
            PendingOrder(
                id=str(r.id),
                trade_code=r.trade_code,
                side=r.side,
                waits=WAITS[(r.side, kind)],
                level=r.limit_price,
                stop=None if stop is None else Decimal(str(stop)),
                target=None if target is None else Decimal(str(target)),
                ounces=r.quantity,
                until=r.expires_at,
                status=r.status,
            )
        )
    return out


async def stop_entries_ready(conn: AsyncConnection) -> bool:
    """Whether the demo's bridge leaves an order waiting for gold to rise (BUY) or fall (SELL) to
    its level (bridge 17, S123); an older one would fill it at once."""
    snap = await conn.scalar(
        select(bridge_state.c.snapshot).where(bridge_state.c.bridge == mt5_bridge.BRIDGE_EXNESS)
    )
    return mt5_bridge.bridge_version(snap or {}) >= mt5_bridge.STOP_ENTRY_VERSION


async def propose(
    conn: AsyncConnection, ticket: DemoTicket, *, actor: Actor, now: datetime
) -> ProposalResult:
    """The page's trade, proposed on the demo for the owner's approval with their code."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": LOCK})
    why = await refusal(conn, now)
    if why is not None:
        raise ValidationFailedError(f"no demo trade now: {why}")
    alias = await account_alias(conn)
    assert alias is not None  # refusal() said so
    inst = await md.instrument_by_key(conn, GOLD)
    q = await md.latest_quote(conn, inst.id, now)
    if q.label not in (DataLabel.LIVE, DataLabel.DELAYED) or q.bid is None or q.ask is None:
        raise ValidationFailedError(
            f"no demo trade now: no current gold price from the Exness demo ({q.label.value})"
        )
    tick = inst.tick_size
    if tick is None:
        raise ValidationFailedError("no demo trade now: gold's price precision is unknown")
    buy = ticket.side == "BUY"
    if ticket.side not in ("BUY", "SELL") or ticket.ounces <= 0:
        raise ValidationFailedError("no demo trade now: the trade has no side or no size")
    if ticket.level is not None:
        return await _propose_pending(conn, ticket, alias, q, tick, actor=actor, now=now)
    entry = (
        _tick(q.ask * (1 + MARKUP), tick, ROUND_UP)
        if buy
        else _tick(q.bid * (1 - MARKUP), tick, ROUND_DOWN)
    )
    if (buy and not ticket.stop < q.bid < ticket.target) or (
        not buy and not ticket.target < q.ask < ticket.stop
    ):
        raise ValidationFailedError(
            "no demo trade now: the price has moved past its stop-loss or take-profit; the page "
            "works the trade out again"
        )
    low, high = band(ticket.side, q.bid, q.ask, entry, ticket.stop, tick)
    size = f"{_plain(ticket.lots)} lot = {_plain(ticket.ounces)} oz"
    spread = (ticket.ounces * (q.ask - q.bid)).quantize(Decimal("0.01"))
    word = "buy" if buy else "sell"
    candidate = Candidate.model_validate(
        {
            "account_alias": alias,
            "instrument_key": GOLD,
            "side": ticket.side,
            "effect": "OPEN",
            "quantity": ticket.ounces,
            "limit_price": entry,
            "price_band_low": low,
            "price_band_high": high,
            "max_slippage_bps": Decimal(15),
            # Filled at once or not at all: an order left resting could fill hours later.
            "time_in_force": "IOC",
            "horizon": "your PDF plan's next trade, on the demo",
            "thesis": (
                f"A TEST on your Exness DEMO account, with virtual money: your plan's trade, "
                f"{word} {size} of gold at the market with a stop-loss at {ticket.stop} and a "
                f"take-profit at {ticket.target}, both held by Exness. {ticket.why} If the "
                f"price has moved past {entry}, nothing is done. Nothing goes to your real "
                "account: its terminal's own bridge refuses every order."
            ),
            "plain_summary": (
                f"DEMO (virtual money): {word} {size} of gold, stop-loss {ticket.stop}, "
                f"take-profit {ticket.target}; at most {ticket.risk_cash} USD of virtual money "
                "lost at the stop, before the spread."
            ),
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{GOLD}",
                    "summary": f"Exness demo bid {_tick(q.bid, tick)} / ask {_tick(q.ask, tick)} "
                    f"({q.label.value})",
                    "source_ts": q.source_ts,
                },
                {"kind": "NOTE", "ref": "demo", "summary": "a test of the portal's orders"},
            ],
            "counterarguments": [
                f"It costs the spread (about {spread} USD) and can lose up to the stop; the "
                "setup's record is in-sample and unverified."
            ],
            "red_team": (
                f"At the stop the demo loses about {ticket.risk_cash} USD, more if gold jumps "
                "past it. Its result counts in the demo's results."
            ),
            "scenarios": [
                {
                    "name": "demo",
                    "description": "Filled within seconds; Exness closes it at its stop-loss or "
                    "take-profit, or you close it.",
                }
            ],
            "invalidation": [f"Gold reaches the stop at {ticket.stop}."],
            "entry_conditions": f"{word.capitalize()} at {entry} or better (0.1 % past the price).",
            "management_plan": {
                # held by Exness at market: its limit is its stop price
                "stop_loss": {"stop_price": ticket.stop, "limit_price": ticket.stop},
                "take_profit_price": ticket.target,
            },
        }
    )
    return await create_candidate(conn, candidate=candidate, actor=actor, now=now, ttl=TTL)


async def _propose_pending(
    conn: AsyncConnection,
    ticket: DemoTicket,
    alias: str,
    q: md.QuoteView,
    tick: Decimal,
    *,
    actor: Actor,
    now: datetime,
) -> ProposalResult:
    """A pending order on the demo (S123, the owner's request of 2026-11-27): left at Exness with
    its stop-loss and take-profit until gold reaches its level, or its time is up."""
    assert ticket.level is not None and q.bid is not None and q.ask is not None
    level, buy = _tick(ticket.level, tick), ticket.side == "BUY"
    if ticket.hours not in PENDING_HOURS:
        raise ValidationFailedError("no pending order: choose how long it waits (1, 4 or 8 hours)")
    kind = pending_kind(ticket.side, level, q.bid, q.ask)
    if kind is None:
        raise ValidationFailedError(
            f"no pending order: {level} is within a spread of the price now ({q.bid} / {q.ask}); "
            "for a trade at this price, use BUY or SELL at the market"
        )
    market = q.ask if buy else q.bid
    if abs(level - market) > market * MAX_AWAY:
        raise ValidationFailedError(
            f"no pending order: {level} is more than {MAX_AWAY * 100:.0f} % from the price now "
            f"({market}); check the price you typed"
        )
    if not (ticket.stop < level < ticket.target if buy else ticket.target < level < ticket.stop):
        raise ValidationFailedError("no pending order: its stop-loss and take-profit are wrong")
    if kind == "STOP" and not await stop_entries_ready(conn):
        raise ValidationFailedError(
            "no pending order of this kind yet: the demo's bridge program on the bridge "
            f"computer is older than version {mt5_bridge.STOP_ENTRY_VERSION}"
        )
    until = now + timedelta(hours=ticket.hours)
    ends = session_ends(now, until)
    if ends is not None:
        raise ValidationFailedError(
            f"no pending order for {ticket.hours} h: {ends}; choose a shorter time"
        )
    low, high = pending_band(ticket.side, kind, level, q.bid, q.ask, ticket.stop, tick)
    waits = WAITS[(ticket.side, kind)]
    word = "buy" if buy else "sell"
    size = f"{_plain(ticket.lots)} lot = {_plain(ticket.ounces)} oz"
    spread = (ticket.ounces * (q.ask - q.bid)).quantize(Decimal("0.01"))
    when = f"{until:%H:%M} UTC"
    candidate = Candidate.model_validate(
        {
            "account_alias": alias,
            "instrument_key": GOLD,
            "side": ticket.side,
            "effect": "OPEN",
            "quantity": ticket.ounces,
            "order_type": "STOP_LIMIT" if kind == "STOP" else "LIMIT",
            "limit_price": level,
            "price_band_low": low,
            "price_band_high": high,
            "max_slippage_bps": Decimal(15),
            # Left at Exness until its time is up, then cancelled by the server.
            "time_in_force": "GTD",
            "horizon": f"a pending order on the demo, until {when}",
            "thesis": (
                f"A TEST on your Exness DEMO account, with virtual money: a pending order to "
                f"{waits} {level} ({word} {size}), with a stop-loss at {ticket.stop} and a "
                f"take-profit at {ticket.target}, both held by Exness. It waits at Exness until "
                f"{when}, then it is cancelled. {ticket.why} Nothing goes to your real account: "
                "its terminal's own bridge refuses every order."
            ),
            "plain_summary": (
                f"DEMO (virtual money): pending order, {waits} {level} ({size}), stop-loss "
                f"{ticket.stop}, take-profit {ticket.target}, waiting at Exness until {when}; at "
                f"most {ticket.risk_cash} USD of virtual money lost at the stop, before the "
                "spread."
            ),
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{GOLD}",
                    "summary": f"Exness demo bid {_tick(q.bid, tick)} / ask {_tick(q.ask, tick)} "
                    f"({q.label.value})",
                    "source_ts": q.source_ts,
                },
                {"kind": "NOTE", "ref": "demo", "summary": "a test of the portal's pending orders"},
            ],
            "counterarguments": [
                f"It costs the spread (about {spread} USD) and can lose up to the stop; the "
                "setup's record is in-sample and unverified."
                + (
                    " An order at a level fills at the market once gold gets there: in a fast "
                    "move, a little past it."
                    if kind == "STOP"
                    else ""
                )
            ],
            "red_team": (
                f"At the stop the demo loses about {ticket.risk_cash} USD, more if gold jumps "
                "past it. Gold may reach the level while you are away: the order fills there."
            ),
            "scenarios": [
                {
                    "name": "pending",
                    "description": f"Waits at Exness until {when}: filled once gold reaches "
                    f"{level}, else cancelled.",
                }
            ],
            "invalidation": [f"Gold reaches the stop at {ticket.stop}."],
            "entry_conditions": f"{waits.capitalize()} {level}; waits at Exness until {when}.",
            "management_plan": {
                "stop_loss": {"stop_price": ticket.stop, "limit_price": ticket.stop},
                "take_profit_price": ticket.target,
            },
        }
    )
    return await create_candidate(conn, candidate=candidate, actor=actor, now=now, ttl=until - now)
