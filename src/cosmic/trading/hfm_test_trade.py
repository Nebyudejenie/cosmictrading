"""One test trade on the HFM demo, through the owner's own approval path (S57).

The trade check (S56) proves HFM's behaviour on a fresh demo before an account is opened, and
refuses an open account: its trades would not be in the account's records. This is the other
half. On the open account, the smallest gold buy goes the way every trade goes:

1. proposed here, with a stop-loss 3 % below, and checked against the risk policy;
2. approved by the owner with their code (nothing is sent before);
3. sent through the bridge with its stop-loss, held by HFM, and filled;
4. closed by an exit the owner approves the same way;
5. reconciled against HFM's own balance to the cent.

It only proposes. DEMO only: a report from a REAL account, from another account, or in any
currency but USD refuses it (``mt5_bridge.report_problem``, the adapter's own rule).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.core.errors import NotFoundError, ValidationFailedError
from cosmic.core.labels import DataLabel
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_trading import bridge_state, positions
from cosmic.market import data as md
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.brokers.hfm_trade_check import session_closed
from cosmic.trading.mandates import IN_FLIGHT
from cosmic.trading.proposals import Candidate, ProposalResult, create_candidate

ACCOUNT = "hfm-demo"
GOLD = "CFD:XAU/USD@HFM"
SYMBOL = "XAUUSD"
STOP_BELOW = Decimal("0.03")  # about three of gold's daily ranges: a few minutes will not hit it
MARKUP = Decimal("0.001")  # a marketable limit at the ask + 0.1 %, as the auto-trader's entries
BAND = Decimal("0.005")
LOCK = "hfm-test-trade"  # one proposal at a time, however often the button is pressed
# The proposal lasts 15 minutes, and gold must trade for the next hour: once approved, at least
# 45 minutes to see it and close it before a break or the weekend (review, 2026-11-04).
TTL = timedelta(minutes=15)
OPEN_AHEAD = timedelta(minutes=30)  # session_closed(t) itself looks half an hour past t


def _tick(value: Decimal, tick: Decimal) -> Decimal:
    step = tick.normalize()
    return ((value / step).to_integral_value(ROUND_DOWN) * step).quantize(step)


def _plain(value: Decimal) -> str:
    """A quantity as people write it: 1, 0.01 (never 1.000000000000000000)."""
    return f"{value.normalize():f}"


async def refusal(conn: AsyncConnection, now: datetime) -> str | None:
    """Why a test trade cannot be proposed now, in words; None when it can. The home page offers
    the button on the same answer, and the action asks it again."""
    try:
        account = await get_account(conn, alias=ACCOUNT)
    except NotFoundError:
        return "no HFM demo account is set up here"
    if (
        account.status != "ACTIVE"
        or account.kind != "SANDBOX"
        or account.broker_adapter != mt5_bridge.ADAPTER
    ):
        return "the HFM demo account is not active"
    if account.reconciliation_status != "OK":
        return "the last check of the HFM account with HFM did not pass"
    row = (
        await conn.execute(select(bridge_state).where(bridge_state.c.bridge == mt5_bridge.BRIDGE))
    ).first()
    if row is None:
        return "the HFM bridge has never connected"
    snap = row.snapshot or {}
    problem = mt5_bridge.report_problem(
        snap, row.received_at, now, await mt5_bridge.opened_on(conn)
    ) or mt5_bridge.trading_off(snap)
    if problem is not None:
        return problem
    closed = session_closed(now) or session_closed(now + OPEN_AHEAD)
    if closed is not None:
        return closed.replace("closes soon", "closes within the hour").replace(
            "enters it soon", "enters it within the hour"
        )
    _, own = mt5_bridge.by_hand(snap)
    if any(p["ticket"] not in own for p in snap.get("positions") or []):
        return "a trade is open on HFM: close it first"  # the system's; the owner's may stay
    held = await conn.scalar(
        select(func.count())
        .select_from(positions)
        .where(positions.c.account_id == account.id, positions.c.quantity > 0)
    )
    if held:
        return (
            "our records still show a trade that HFM no longer holds; they catch up at the next "
            "check with HFM, within five minutes"
        )
    busy = await conn.scalar(
        select(func.count())
        .select_from(trade_intents)
        .where(trade_intents.c.account_id == account.id, trade_intents.c.status.in_(IN_FLIGHT))
    )
    if busy:
        return "a trade on the HFM account waits for a decision or is being sent: finish it first"
    return None


async def propose(conn: AsyncConnection, *, actor: Actor, now: datetime) -> ProposalResult:
    """The test buy, proposed for the owner's approval; refused with the reason in words."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": LOCK})
    why = await refusal(conn, now)
    if why is not None:
        raise ValidationFailedError(f"no test trade now: {why}")
    inst = await md.instrument_by_key(conn, GOLD)
    q = await md.latest_quote(conn, inst.id, now)
    if q.label not in (DataLabel.LIVE, DataLabel.DELAYED) or q.bid is None or q.ask is None:
        raise ValidationFailedError(
            f"no test trade now: no current gold price from HFM ({q.label.value}); try again "
            "in a minute"
        )
    tick, qty = inst.tick_size, inst.min_qty or inst.lot_size
    if tick is None or qty is None:
        raise ValidationFailedError("no test trade now: gold's price or size precision is unknown")
    snap = await conn.scalar(
        select(bridge_state.c.snapshot).where(bridge_state.c.bridge == mt5_bridge.BRIDGE)
    )
    spec = ((snap or {}).get("symbols") or {}).get(SYMBOL) or {}
    contract = Decimal(str(spec.get("contract_size") or 0))
    lots = f"{_plain(qty / contract)} lot = " if contract > 0 else ""
    entry = _tick(q.ask * (1 + MARKUP), tick)
    stop = _tick(entry * (1 - STOP_BELOW), tick)
    risk = (qty * (entry - stop)).quantize(Decimal("0.01"))
    spread = (qty * (q.ask - q.bid)).quantize(Decimal("0.01"))
    size = f"{lots}{_plain(qty)} oz"
    candidate = Candidate.model_validate(
        {
            "account_alias": ACCOUNT,
            "instrument_key": GOLD,
            "side": "BUY",
            "effect": "OPEN",
            "quantity": qty,
            "limit_price": entry,
            "price_band_low": _tick(q.bid * (1 - BAND), tick),
            "price_band_high": entry,
            "max_slippage_bps": Decimal(15),
            # Filled at once or not at all: a buy left resting at HFM could fill hours later.
            "time_in_force": "IOC",
            "horizon": "minutes: a test of the order path",
            "thesis": (
                "A TEST of the whole order path on your HFM demo account, with virtual money; "
                f"not a view on gold. The smallest gold trade ({size}) is bought at the market "
                f"with a stop-loss at {stop}, 3 % below, held by HFM; if the price has moved past "
                f"{entry}, nothing is bought. Close it from the home page within half an hour, "
                "once you have seen it in MT5: the system then checks its records against HFM's "
                "balance, to the cent."
            ),
            "plain_summary": (
                f"TEST on your HFM demo (virtual money): buy {size} of gold with a stop-loss 3 % "
                "below, to prove that orders reach HFM, fill and reconcile. Not a market view."
            ),
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{GOLD}",
                    "summary": f"HFM bid {_tick(q.bid, tick)} / ask {_tick(q.ask, tick)} "
                    f"({q.label.value})",
                    "source_ts": q.source_ts,
                },
                {"kind": "NOTE", "ref": "test", "summary": "a test of the order path on a demo"},
            ],
            "counterarguments": [
                f"This is not a trading idea: it costs the spread (about {spread} USD) and "
                "whatever gold moves while it is open."
            ],
            "red_team": (
                f"A test can lose: if gold falls 3 % to the stop, about {risk} USD of virtual "
                "money, more if it jumps past the stop. Its result counts in the account's "
                "results and in its run of losses."
            ),
            "scenarios": [
                {
                    "name": "test",
                    "description": "Filled within seconds and closed minutes later; the result "
                    "is the spread plus gold's move meanwhile.",
                }
            ],
            "invalidation": [f"Gold falls to the stop at {stop}."],
            "entry_conditions": f"Buy at or below {entry} (the ask + 0.1 %).",
            "management_plan": {"stop_loss": {"stop_price": stop, "limit_price": stop}},
        }
    )
    return await create_candidate(conn, candidate=candidate, actor=actor, now=now, ttl=TTL)
