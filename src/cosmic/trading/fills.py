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
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident
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
            alert=Alert(
                title="Broker reported more fills than the order quantity",
                severity="CRITICAL",
                what_happened=f"A fill for order {order.client_order_id} would take the filled "
                "quantity above the order quantity.",
                verified=f"Recorded {order.filled_qty} of {order.quantity}; the new fill is "
                f"{fill.quantity}.",
                uncertain="Whether the venue double-reported or our records are incomplete.",
                risk_now="The position and cash for this account may be wrong.",
                action_taken="The fill was NOT applied; the account is marked reconciliation "
                "FAILED, which blocks new exposure.",
                owner_action="Compare the venue's fills with the Orders page and reconcile; "
                "contact the venue if the report is wrong.",
            ),
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
    # A fee in the base asset changes the quantity held (Binance takes a buy's commission from
    # the asset received); a quote fee is part of the cost; any other asset is outside the
    # position and P&L and is reported (ADR-026 §4).
    base_fee = fill.fee if fill.fee_asset == inst.base else ZERO
    quote_fee = fill.fee if fill.fee_asset == inst.quote else ZERO
    if order.side == "BUY":
        if base_fee >= fill.quantity:
            await _fee_anomaly(conn, order, fill, now)
        new_qty = qty + fill.quantity - base_fee
        cost = (qty * (avg or ZERO)) + fill.quantity * fill.price + quote_fee
        new_avg: Decimal | None = cost / new_qty
    else:
        if fill.quantity + base_fee > qty or avg is None:
            await raise_incident(
                conn,
                dedup_key=f"accounting.oversell:{order.id}",
                category=Category.ACCOUNTING,
                alert=Alert(
                    title="Sell fill exceeds the recorded position",
                    severity="CRITICAL",
                    what_happened=f"A sell fill for order {order.client_order_id} is larger than "
                    "the position we hold.",
                    verified=f"Recorded position {qty}; the sell fill is {fill.quantity}.",
                    uncertain="Whether a buy fill is missing from our records or the venue "
                    "sold more than we asked.",
                    risk_now="The account may hold a position we do not know about (or none).",
                    action_taken="The fill was NOT applied; the account is marked reconciliation "
                    "FAILED, which blocks new exposure.",
                    owner_action="Check the venue's balances against the Portfolio page and "
                    "reconcile before resuming.",
                ),
                now=now,
                detail={"order": order.client_order_id, "held": qty},
            )
            raise FillAnomalyError("oversell")
        realized = fill.quantity * (fill.price - avg) - quote_fee - base_fee * avg
        new_qty = qty - fill.quantity - base_fee
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
    if fill.fee > ZERO and fill.fee_asset not in (inst.base, inst.quote):
        await _foreign_fee(conn, order, fill, inst, now)
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


async def _fee_anomaly(
    conn: AsyncConnection, order: OrderRow, fill: VenueFill, now: datetime
) -> None:
    await raise_incident(
        conn,
        dedup_key=f"accounting.fee_exceeds_fill:{order.id}",
        category=Category.ACCOUNTING,
        alert=Alert(
            title="A fill's fee is not smaller than the quantity received",
            severity="CRITICAL",
            what_happened=f"Order {order.client_order_id} reported a fee of {fill.fee} "
            f"{fill.fee_asset} on a fill of {fill.quantity}.",
            verified="The venue's fill report as received.",
            uncertain="Whether the report is wrong or our parsing of it is.",
            risk_now="The position for this account may be wrong.",
            action_taken="The fill was NOT applied; the account is marked reconciliation "
            "FAILED, which blocks new exposure.",
            owner_action="Compare the venue's trade history with the Orders page and reconcile.",
        ),
        now=now,
        detail={"order": order.client_order_id, "fill": fill.venue_fill_id},
    )
    raise FillAnomalyError("fee exceeds fill")


async def _foreign_fee(
    conn: AsyncConnection, order: OrderRow, fill: VenueFill, inst: InstrumentView, now: datetime
) -> None:
    await raise_incident(
        conn,
        dedup_key=f"accounting.foreign_fee:{order.account_id}:{fill.fee_asset}",
        category=Category.ACCOUNTING,
        alert=Alert(
            title=f"Fees are being charged in {fill.fee_asset}",
            severity="WARNING",
            what_happened=f"A fill on {inst.canonical_key} paid its fee in {fill.fee_asset}, "
            f"which is neither {inst.base} nor {inst.quote}.",
            verified=f"Fee {fill.fee} {fill.fee_asset} on order {order.client_order_id}.",
            uncertain="The fee's value in the account currency (not converted).",
            risk_now="Realised P&L leaves these fees out, so it is better than the truth.",
            action_taken=f"The fill was applied; the fee was booked against unmanaged "
            f"{fill.fee_asset} holdings.",
            owner_action=f"Turn off paying fees in {fill.fee_asset} at the venue if you can, "
            "and treat P&L as excluding these fees.",
        ),
        now=now,
        detail={"order": order.client_order_id, "fee": fill.fee, "asset": fill.fee_asset},
    )


async def reload(conn: AsyncConnection, order: OrderRow) -> OrderRow:
    return await get_order(conn, order.id, lock=True)
