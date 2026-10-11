"""Post-trade review (V6 §1826): planned vs actual, stored separately from the original
packet (which is immutable). Learning never edits a live mandate or the original thesis."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.money import BPS, ZERO
from cosmic.db.schema_execution import (
    approval_packets,
    fills,
    orders,
    post_trade_reviews,
    trade_intents,
)
from cosmic.notify.outbox import notify_owners

TERMINAL = ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")


async def create_review_if_closed(
    conn: AsyncConnection, *, intent_id: uuid.UUID, now: datetime
) -> bool:
    exists = (
        await conn.execute(
            select(post_trade_reviews.c.id).where(post_trade_reviews.c.intent_id == intent_id)
        )
    ).first()
    if exists is not None:
        return False
    intent = (
        await conn.execute(select(trade_intents).where(trade_intents.c.id == intent_id))
    ).one()
    rows = list(await conn.execute(select(orders).where(orders.c.intent_id == intent_id)))
    entry = next((o for o in rows if o.purpose == "ENTRY"), None)
    if entry is None or entry.status not in TERMINAL or entry.filled_qty == ZERO:
        return False
    if any(o.status not in TERMINAL for o in rows):
        return False
    fill_rows = list(
        await conn.execute(
            select(fills, orders.c.side, orders.c.purpose)
            .join(orders, orders.c.id == fills.c.order_id)
            .where(orders.c.intent_id == intent_id)
        )
    )
    bought = sum((f.quantity for f in fill_rows if f.side == "BUY"), ZERO)
    sold = sum((f.quantity for f in fill_rows if f.side == "SELL"), ZERO)
    if bought - sold != ZERO:
        return False  # position still open
    realized = sum((f.realized_pnl or ZERO for f in fill_rows), ZERO)
    fees = sum((f.fee for f in fill_rows), ZERO)
    exit_value = sum((f.quantity * f.price for f in fill_rows if f.side == "SELL"), ZERO)
    exit_avg = exit_value / sold if sold else None
    packet = (
        await conn.execute(
            select(approval_packets.c.packet)
            .where(approval_packets.c.intent_id == intent_id)
            .order_by(approval_packets.c.version.desc())
            .limit(1)
        )
    ).scalar_one()
    entry_slip = None
    if entry.avg_fill_price is not None:
        entry_slip = (entry.avg_fill_price - entry.limit_price) / entry.limit_price * BPS
    within_authority = (
        all(o.reduce_only for o in rows if o.purpose != "ENTRY")
        and sold <= bought
        and entry.filled_qty <= intent.quantity
    )
    exits = {}
    for o in rows:
        if o.purpose != "ENTRY":
            exits[o.client_order_id] = {
                "purpose": o.purpose,
                "status": o.status,
                "filled": o.filled_qty,
            }
    review_id = uuid7()
    await conn.execute(
        pg_insert(post_trade_reviews)
        .values(
            id=review_id,
            intent_id=intent_id,
            created_at=now,
            realized_pnl=realized,
            is_simulated=intent.is_simulated,
            planned=to_jsonable(
                {
                    "quantity": intent.quantity,
                    "limit_price": intent.limit_price,
                    "management_plan": intent.management_plan,
                    "thesis": intent.thesis,
                    "packet_policy_version": packet["policy_version"],
                }
            ),
            actual=to_jsonable(
                {
                    "entry_avg_price": entry.avg_fill_price,
                    "entry_filled": entry.filled_qty,
                    "exit_avg_price": exit_avg,
                    "exit_quantity": sold,
                    "fees": fees,
                    "realized_pnl_after_fees": realized,
                    "exits": exits,
                    "entry_slippage_vs_limit_bps": entry_slip,
                }
            ),
            assessment=to_jsonable(
                {
                    "outcome": "GAIN" if realized > 0 else ("LOSS" if realized < 0 else "FLAT"),
                    "stayed_within_authorisation": within_authority,
                    "decision_quality": "REQUIRES_OPERATOR_REVIEW",
                    "note": "A good decision can lose and a bad one can win; judge the process. "
                    "The original thesis and packet are preserved unchanged.",
                    "simulated": intent.is_simulated,
                }
            ),
        )
        .on_conflict_do_nothing(index_elements=[post_trade_reviews.c.intent_id])
    )
    await record(
        conn,
        action="trade.review_created",
        actor=Actor.system("review"),
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=intent_id,
        payload={"realized_pnl": realized, "within_authorisation": within_authority},
    )
    await notify_owners(
        conn,
        kind="trade_closed",
        now=now,
        dedup_key=f"closed:{intent_id}",
        payload={
            "trade_code": intent.trade_code,
            "realized_pnl": realized,
            "simulated": intent.is_simulated,
        },
    )
    return True


async def count_reviews(conn: AsyncConnection) -> int:
    return int(await conn.scalar(select(func.count()).select_from(post_trade_reviews)) or 0)


def pnl(x: Decimal | None) -> Decimal:
    return x or ZERO
