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
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_trading import accounts, bridge_state
from cosmic.market import data as md
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.brokers.hfm_trade_check import session_closed
from cosmic.trading.mandates import IN_FLIGHT
from cosmic.trading.proposals import Candidate, ProposalResult, create_candidate

GOLD = "CFD:XAU/USD@EXNESS"
MARKUP = Decimal("0.001")  # a marketable limit 0.1 % past the price, as the test trade's
LOCK = "exness-demo-trade"  # one proposal at a time, however often a button is pressed
TTL = timedelta(minutes=5)  # a setup's price is minutes old at most when approved
OPEN_AHEAD = timedelta(minutes=30)


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
        return "a trade on the Exness demo waits for your decision or is being sent: finish it"
    return None


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
