"""The overnight swap the stop for good keeps aside (S54, the owner's choice of 2026-11-03).

HFM charges a CFD's swap at the exit, and a trade's plan (its loss at the stop) leaves it out, so
a long-held last trade could end a rehearsal below its floor. The stop for good, the entry check
and a renewal therefore count:

- the swap the account's open CFD positions have run up so far (``accrued``), and
- what a position as large as the risk policy allows would pay over ``RESERVE_NIGHTS`` more
  nights (``ahead``): the next trade's, or, at a renewal, the trade taken over.

Markets without swap (Binance spot) keep nothing aside. A trade held longer than that, or a price
gap past its stop, can still take a trade past its plan; the owner's notice then says so.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import NotFoundError
from cosmic.core.money import ZERO
from cosmic.db.schema_execution import fills, orders
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.trading import venue_terms

RESERVE_NIGHTS = 90  # a run's length: the swap one more trade could pay is kept aside for it


def nightly_cost(inst: md.InstrumentView, price: Decimal) -> tuple[Decimal, int | None]:
    """(the share of the price a long position pays a night, the weekday charged three times);
    zero for a market without swap or one that pays it."""
    if inst.asset_class != "CFD":
        return ZERO, None
    sw = venue_terms.swap_per_night(inst.metadata, float(price))
    if sw is None or sw.long >= 0:
        return ZERO, None
    triple = sw.triple_weekday if inst.base in venue_terms.METALS else None
    return Decimal(str(-sw.long)), triple


async def accrued(conn: AsyncConnection, account_id: uuid.UUID, now: datetime) -> Decimal:
    """The swap the account's open positions have run up since their last buy, not yet charged
    (the paper venue charges it at the exit, as HFM does)."""
    total = ZERO
    held = await conn.execute(
        select(positions.c.instrument_id, positions.c.quantity, positions.c.avg_cost).where(
            positions.c.account_id == account_id, positions.c.quantity > 0
        )
    )
    for row in list(held):
        if row.avg_cost is None:
            continue
        inst = await md.instrument_by_id(conn, row.instrument_id)
        rate, triple = nightly_cost(inst, row.avg_cost)
        if rate == ZERO:
            continue
        opened = await conn.scalar(
            select(func.max(fills.c.filled_at))
            .select_from(fills.join(orders, orders.c.id == fills.c.order_id))
            .where(
                orders.c.account_id == account_id,
                orders.c.instrument_id == row.instrument_id,
                orders.c.side == "BUY",
            )
        )
        if opened is not None:
            total += row.quantity * row.avg_cost * rate * venue_terms.nights(opened, now, triple)
    return total


async def ahead(
    conn: AsyncConnection, keys: Iterable[str], value: Decimal, now: datetime
) -> Decimal:
    """What ``value`` held on the costliest of ``keys`` would pay over ``RESERVE_NIGHTS``."""
    worst = ZERO
    for key in keys:
        try:
            inst = await md.instrument_by_key(conn, key)
        except NotFoundError:
            continue
        q = await md.latest_quote(conn, inst.id, now)
        if q.mid is None:
            continue
        rate, _ = nightly_cost(inst, q.mid)
        worst = max(worst, value * rate * RESERVE_NIGHTS)
    return worst
