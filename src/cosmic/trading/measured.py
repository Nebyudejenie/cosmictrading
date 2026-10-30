"""What the venues really charged: the commission per side, measured from the owner's fills.

The risk policy's fee per venue is an assumption until the owner confirms it (B-08), and it
drives sizing, the cost check, every test and the decision guide. The fills already hold what
each venue charged, so the system can measure it:

- only entry fills count: an exit fill on a CFD also carries the overnight swap;
- fills of the paper simulator are left out: it charges the policy's own assumption, so it
  would only echo it;
- a fee in the base asset (Binance) is valued at the fill price; a fee in another asset is
  left out (it cannot be valued here).

It also measures how far each fill landed from the price when the order was sent
(``fill_quality``, S39): an entry against the ask and an exit against the bid, a stop against
its stop price. The replays assume ``REPLAY_SLIPPAGE_BPS`` a fill; a venue's own fills say
whether that holds. The paper simulator, a demo or test network, and real money are kept
apart: only real money settles it.

Read-only: changing the policy stays the owner's decision, with a fresh code.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import BPS, ZERO
from cosmic.db.schema_execution import approvals, fills, orders
from cosmic.db.schema_market import instruments, quotes, venues
from cosmic.db.schema_trading import accounts, risk_check_runs
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, environment_of

SIMULATOR = "paper"
NOTICEABLE_BPS = Decimal(5)  # a measured fee this far from the policy's is worth telling
# The slippage the replays assume a fill (``expect.SLIPPAGE``; trading cannot import research,
# so a test keeps the two equal).
REPLAY_SLIPPAGE_BPS = Decimal(5)
MIN_FILLS_FOR_ADVICE = 3  # fewer fills say too little to judge the replays by
SENT_PROMPTLY = timedelta(seconds=60)  # an order sent within this of its check
QUOTE_MAX_AGE = timedelta(minutes=5)  # the stored quote for an order sent later
RECENT = timedelta(days=90)  # the Risk page and the briefing judge the venues by these fills


@dataclass(frozen=True)
class MeasuredFee:
    venue: str
    fills: int
    notional: Decimal  # the entry fills' value, in the quote currency
    fees: Decimal  # what the venue charged on them

    @property
    def bps(self) -> Decimal:
        """The commission per side, in bps of the trade's value."""
        if self.notional <= ZERO:
            return ZERO
        return (self.fees / self.notional * 10_000).quantize(Decimal("0.01"))


async def measured_fees(conn: AsyncConnection) -> dict[str, MeasuredFee]:
    """The commission each real venue charged on the owner's entry fills, by venue key."""
    rows = await conn.execute(
        select(
            venues.c.key,
            fills.c.quantity,
            fills.c.price,
            fills.c.fee,
            fills.c.fee_currency,
            instruments.c.pair_key,
        )
        .select_from(
            fills.join(orders, orders.c.id == fills.c.order_id)
            .join(accounts, accounts.c.id == orders.c.account_id)
            .join(venues, venues.c.id == accounts.c.venue_id)
            .join(instruments, instruments.c.id == orders.c.instrument_id)
        )
        .where(orders.c.purpose == "ENTRY", accounts.c.broker_adapter != SIMULATOR)
    )
    totals: dict[str, list[Decimal]] = {}
    for r in rows:
        fee = _quote(r.pair_key, r.fee, r.fee_currency, r.price)
        if fee is None:
            continue
        t = totals.setdefault(r.key, [ZERO, ZERO, ZERO])
        t[0] += 1
        t[1] += r.quantity * r.price
        t[2] += fee
    return {k: MeasuredFee(k, int(n), value, fee) for k, (n, value, fee) in totals.items()}


def _quote(pair_key: str, fee: Decimal, fee_currency: str, price: Decimal) -> Decimal | None:
    """A fee in the quote currency: a fee in the base asset is valued at the fill price; one
    in another asset cannot be valued here."""
    base, _, quote = pair_key.partition("/")
    if fee_currency == quote:
        return fee
    if fee_currency == base:
        return fee * price
    return None


async def costs_paid(
    conn: AsyncConnection, account_id: uuid.UUID, since: datetime | None = None
) -> Decimal:
    """What an account's fills were charged, in its quote currency: the commission and, on a
    CFD's closing fill, the overnight swap (simulated on a paper account)."""
    q = (
        select(fills.c.price, fills.c.fee, fills.c.fee_currency, instruments.c.pair_key)
        .select_from(
            fills.join(orders, orders.c.id == fills.c.order_id).join(
                instruments, instruments.c.id == orders.c.instrument_id
            )
        )
        .where(orders.c.account_id == account_id)
    )
    if since is not None:
        q = q.where(fills.c.filled_at >= since)
    total = ZERO
    for r in await conn.execute(q):
        fee = _quote(r.pair_key, r.fee, r.fee_currency, r.price)
        total += fee or ZERO
    return total


@dataclass(frozen=True)
class Slip:
    """One filled order against the price the system expected."""

    venue: str
    account_id: uuid.UUID
    environment: str  # PAPER | TESTNET | DEMO | LIVE (``modes.environment_of``)
    simulated: bool  # virtual money: the paper simulator, a test network or a demo
    purpose: str  # ENTRY | EXIT | STOP_LOSS
    side: str
    expected: Decimal  # the ask (buy) or bid (sell) when sent, or the stop price
    fill: Decimal  # the average fill price
    at: datetime  # when the order was sent

    @property
    def bps(self) -> Decimal:
        """What the fill cost against the expected price, in bps (negative: it did better)."""
        if self.expected <= ZERO:
            return ZERO
        moved = self.fill - self.expected if self.side == "BUY" else self.expected - self.fill
        return (moved / self.expected * BPS).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class FillQuality:
    """A venue's fills in one environment. Orders sent at the market price (entries and
    exits) are what the replays' slippage stands for; a stop is apart, against its stop price:
    a gap through it is counted there, which the replays model as a fill at the gapped open."""

    venue: str
    environment: str  # PAPER | TESTNET | DEMO | LIVE
    simulated: bool
    fills: int  # entries and exits
    median_bps: Decimal | None  # the typical one; None without any
    worst_bps: Decimal | None  # the costliest
    stops: int = 0  # stop-loss fills
    stop_median_bps: Decimal | None = None
    stop_worst_bps: Decimal | None = None

    @property
    def label(self) -> str:
        return "SIMULATED" if self.simulated else "MEASURED"

    @property
    def paper(self) -> bool:
        """Filled by the paper simulator, which adds the replays' slippage by design."""
        return self.environment == "PAPER"


def _dec(value: Any) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:
        return None


async def _quote_at(
    conn: AsyncConnection, instrument_id: uuid.UUID, at: datetime, side: str
) -> Decimal | None:
    """The stored ask (buy) or bid (sell) at ``at``, if one is recent enough."""
    row = (
        await conn.execute(
            select(quotes.c.bid, quotes.c.ask)
            .where(
                quotes.c.instrument_id == instrument_id,
                quotes.c.source_ts <= at,
                quotes.c.source_ts >= at - QUOTE_MAX_AGE,
            )
            .order_by(desc(quotes.c.source_ts), desc(quotes.c.id))
            .limit(1)
        )
    ).first()
    return None if row is None else _dec(row.ask if side == "BUY" else row.bid)


async def fill_slips(
    conn: AsyncConnection,
    *,
    account_id: uuid.UUID | None = None,
    since: datetime | None = None,
) -> list[Slip]:
    """Every filled entry, exit and stop-loss order with a fill since ``since``, against the
    price expected when it was sent.

    - **An entry or exit** is compared with the ask (buy) or bid (sell) its pre-submission risk
      check recorded: the latest passed check of the trade its approval was for, made before
      the order. That is the price when sent, as the order goes out in the same cycle.
    - **An order sent later** than ``SENT_PROMPTLY`` after it was made (an exit waiting for its
      stop to be cancelled, a venue that was down) is compared with the stored quote when it
      was sent instead, at most ``QUOTE_MAX_AGE`` old; a price move while it waited is not
      slippage.
    - **A stop** is compared with its stop price.

    A fill without such a price is left out."""
    q = (
        select(
            orders.c.id,
            orders.c.account_id,
            orders.c.instrument_id,
            orders.c.purpose,
            orders.c.side,
            orders.c.stop_price,
            orders.c.avg_fill_price,
            orders.c.created_at,
            orders.c.submitted_at,
            approvals.c.intent_id.label("approved_intent"),
            accounts.c.kind,
            accounts.c.broker_adapter,
            venues.c.key.label("venue"),
        )
        .select_from(
            orders.join(accounts, accounts.c.id == orders.c.account_id)
            .join(venues, venues.c.id == accounts.c.venue_id)
            .outerjoin(approvals, approvals.c.id == orders.c.approval_id)
        )
        .where(
            orders.c.purpose.in_(("ENTRY", "EXIT", "STOP_LOSS")),
            orders.c.filled_qty > 0,
            orders.c.avg_fill_price.is_not(None),
            orders.c.submitted_at.is_not(None),
        )
        .order_by(orders.c.submitted_at)
    )
    if account_id is not None:
        q = q.where(orders.c.account_id == account_id)
    if since is not None:  # by fill time: a stop sent months ago may have filled yesterday
        q = q.where(orders.c.id.in_(select(fills.c.order_id).where(fills.c.filled_at >= since)))
    rows = list(await conn.execute(q))
    intents = {
        r.approved_intent
        for r in rows
        if r.purpose != "STOP_LOSS"
        and r.approved_intent is not None
        and r.submitted_at - r.created_at <= SENT_PROMPTLY
    }
    checked: dict[uuid.UUID, list[tuple[datetime, dict[str, Any]]]] = {}
    if intents:
        for run in await conn.execute(
            select(
                risk_check_runs.c.intent_id,
                risk_check_runs.c.created_at,
                risk_check_runs.c.context["market"].label("market"),
            )
            .where(
                risk_check_runs.c.intent_id.in_(intents),
                risk_check_runs.c.stage == "PRE_SUBMISSION",
                risk_check_runs.c.decision == "PASS",
            )
            .order_by(risk_check_runs.c.created_at)
        ):
            checked.setdefault(run.intent_id, []).append((run.created_at, run.market or {}))
    out: list[Slip] = []
    for r in rows:
        if r.purpose == "STOP_LOSS":
            expected = _dec(r.stop_price)
        elif r.submitted_at - r.created_at <= SENT_PROMPTLY:
            sent = [m for at, m in checked.get(r.approved_intent, []) if at <= r.created_at]
            expected = _dec(sent[-1].get("ask" if r.side == "BUY" else "bid")) if sent else None
        else:
            expected = await _quote_at(conn, r.instrument_id, r.submitted_at, r.side)
        if expected is None or expected <= ZERO:
            continue
        out.append(
            Slip(
                r.venue,
                r.account_id,
                environment_of(r.kind, r.broker_adapter).code,
                r.kind in SIMULATED_ACCOUNT_KINDS,
                r.purpose,
                r.side,
                expected,
                r.avg_fill_price,
                r.submitted_at,
            )
        )
    return out


def _median(values: list[Decimal]) -> Decimal | None:
    return Decimal(statistics.median(values)).quantize(Decimal("0.01")) if values else None


def fill_quality(slips: list[Slip]) -> list[FillQuality]:
    """The slips by venue and environment: the paper simulator, a demo or test network and
    real money are never mixed, nor stops with orders sent at the market price."""
    groups: dict[tuple[str, str, bool], list[Slip]] = {}
    for s in slips:
        groups.setdefault((s.venue, s.environment, s.simulated), []).append(s)
    out: list[FillQuality] = []
    for (venue, environment, simulated), group in sorted(groups.items()):
        sent = [s.bps for s in group if s.purpose != "STOP_LOSS"]
        stops = [s.bps for s in group if s.purpose == "STOP_LOSS"]
        out.append(
            FillQuality(
                venue,
                environment,
                simulated,
                len(sent),
                _median(sent),
                max(sent) if sent else None,
                len(stops),
                _median(stops),
                max(stops) if stops else None,
            )
        )
    return out


def slippage_advice(q: FillQuality) -> str | None:
    """A plain sentence when a venue's own fills differ from what the replays assume.

    Never from the paper simulator (it adds the assumption itself). From a demo or test
    network only when its fills cost more than assumed: a demo tends to fill better than a
    real account, so a better demo proves nothing, but a worse one already shows the replays
    flatter the rules."""
    if q.paper or q.fills < MIN_FILLS_FOR_ADVICE or q.median_bps is None:
        return None
    if abs(q.median_bps - REPLAY_SLIPPAGE_BPS) < NOTICEABLE_BPS:
        return None
    where = f"{q.venue} {q.environment.lower()}" if q.simulated else q.venue
    told = (
        f"Your {where} fills cost a median {q.median_bps:.2f} bps against the price when sent, "
        f"over {q.fills} fill(s) at the market price; the replays assume {REPLAY_SLIPPAGE_BPS} "
        "bps"
    )
    if q.median_bps > REPLAY_SLIPPAGE_BPS:
        return f"{told}. The replays flatter the rules on this venue: expect less than they show."
    if q.simulated:
        return None
    return f"{told}, so they are cautious on this venue."


def advice(measured: MeasuredFee, policy_bps: Decimal) -> str | None:
    """A plain sentence when the measured fee is far from the policy's assumption."""
    if measured.fills == 0 or abs(measured.bps - policy_bps) < NOTICEABLE_BPS:
        return None
    return (
        f"Your {measured.venue} trades show a commission of {measured.bps:.2f} bps a side over "
        f"{measured.fills} fill(s), but the risk policy assumes {policy_bps:.2f} bps. Set Fee "
        f"per venue {measured.venue}={measured.bps.normalize():f} under Risk policy if that is "
        "your account's fee."
    )
