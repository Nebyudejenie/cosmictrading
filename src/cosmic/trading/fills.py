"""Apply broker-confirmed fills: fill row, position (average cost), ledger, order totals.

Idempotent per (order, venue fill id): duplicate fill reports change nothing (AT-09).
Callers must hold the order row lock.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.schema_execution import fills
from cosmic.db.schema_trading import positions
from cosmic.market.data import InstrumentView
from cosmic.ops.incidents import Category, Severity, raise_incident
from cosmic.trading import ledger
from cosmic.trading.brokers.base import VenueFill
from cosmic.trading.orders import OrderRow, get_order, update_fill_totals


class FillAnomalyError(Exception):
    """A broker fill contradicts internal state (overfill, oversell). Never auto-repaired."""


async def apply_fill(
    conn: AsyncConnection,
    *,
    order: OrderRow,
    fill: VenueFill,
    inst: InstrumentView,
    source: str,
    now: datetime,
) -> tuple[OrderRow, bool]:
    exists = (
        await conn.execute(
            select(fills.c.id).where(
                fills.c.order_id == order.id, fills.c.broker_fill_id == fill.venue_fill_id
            )
        )
    ).first()
    if exists is not None:
        return order, False
    if order.filled_qty + fill.quantity > order.quantity:
        await raise_incident(
            conn,
            dedup_key=f"accounting.overfill:{order.id}",
            category=Category.ACCOUNTING,
            severity=Severity.CRITICAL,
            title="Broker reported more fills than the order quantity",
            now=now,
            detail={"order": order.client_order_id, "fill": fill.venue_fill_id},
        )
        raise FillAnomalyError("overfill")
    pos = (
        await conn.execute(
            select(positions)
            .where(
                positions.c.account_id == order.account_id,
                positions.c.instrument_id == order.instrument_id,
            )
            .with_for_update()
        )
    ).first()
    qty, avg, realized_total = (
        (pos.quantity, pos.avg_cost, pos.realized_pnl) if pos else (ZERO, None, ZERO)
    )
    realized: Decimal | None = None
    if order.side == "BUY":
        new_qty = qty + fill.quantity
        cost = (qty * (avg or ZERO)) + fill.quantity * fill.price + fill.fee
        new_avg: Decimal | None = cost / new_qty
    else:
        if fill.quantity > qty or avg is None:
            await raise_incident(
                conn,
                dedup_key=f"accounting.oversell:{order.id}",
                category=Category.ACCOUNTING,
                severity=Severity.CRITICAL,
                title="Sell fill exceeds the recorded position",
                now=now,
                detail={"order": order.client_order_id, "held": qty},
            )
            raise FillAnomalyError("oversell")
        realized = fill.quantity * (fill.price - avg) - fill.fee
        new_qty = qty - fill.quantity
        new_avg = avg if new_qty > ZERO else None
        realized_total += realized
    fill_id = uuid7()
    await conn.execute(
        fills.insert().values(
            id=fill_id,
            order_id=order.id,
            broker_fill_id=fill.venue_fill_id,
            quantity=fill.quantity,
            price=fill.price,
            fee=fill.fee,
            fee_currency=fill.fee_asset,
            liquidity=fill.liquidity,
            realized_pnl=realized,
            filled_at=fill.filled_at,
            source=source,
            is_simulated=order.is_simulated,
            recorded_at=now,
        )
    )
    if pos is None:
        await conn.execute(
            positions.insert().values(
                account_id=order.account_id,
                instrument_id=order.instrument_id,
                quantity=new_qty,
                avg_cost=new_avg,
                realized_pnl=realized_total,
                updated_at=now,
            )
        )
    else:
        await conn.execute(
            positions.update()
            .where(
                positions.c.account_id == order.account_id,
                positions.c.instrument_id == order.instrument_id,
            )
            .values(quantity=new_qty, avg_cost=new_avg, realized_pnl=realized_total, updated_at=now)
        )
    await ledger.post(
        conn,
        account_id=order.account_id,
        kind="FILL",
        ref_type="fill",
        ref_id=str(fill_id),
        description=f"{order.side} {fill.quantity} {inst.canonical_key} @ {fill.price}",
        entries=ledger.fill_entries(
            side=order.side,
            base=inst.base,
            quote=inst.quote,
            quantity=fill.quantity,
            price=fill.price,
            fee=fill.fee,
            fee_asset=fill.fee_asset,
        ),
        now=now,
        is_simulated=order.is_simulated,
    )
    filled = order.filled_qty + fill.quantity
    avg_fill = (
        (order.avg_fill_price or ZERO) * order.filled_qty + fill.price * fill.quantity
    ) / filled
    updated = await update_fill_totals(
        conn,
        order,
        filled_qty=filled,
        avg_price=avg_fill,
        fees=order.fees_paid + fill.fee,
        fee_currency=fill.fee_asset,
        now=now,
    )
    return updated, True


async def reload(conn: AsyncConnection, order: OrderRow) -> OrderRow:
    return await get_order(conn, order.id, lock=True)
