"""Order state machine (V6 §1812) with event history and optimistic concurrency.

Broker-grounded states (ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, and
REJECTED after transmission) may only be entered with ``source`` BROKER or RECONCILE.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.redaction import redact
from cosmic.db.schema_execution import order_events, orders


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    AUTHORIZED = "AUTHORIZED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REPLACE_PENDING = "REPLACE_PENDING"
    UNKNOWN_ORDER_STATE = "UNKNOWN_ORDER_STATE"


class Source(StrEnum):
    LOCAL = "LOCAL"
    BROKER = "BROKER"
    RECONCILE = "RECONCILE"


S = OrderStatus
TERMINAL = frozenset({S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED})
# States in which the venue may hold the order (it may fill or change without us).
LIVE_AT_VENUE = frozenset(
    {
        S.SUBMITTED,
        S.ACKNOWLEDGED,
        S.PARTIALLY_FILLED,
        S.CANCEL_PENDING,
        S.REPLACE_PENDING,
        S.UNKNOWN_ORDER_STATE,
        S.SUBMITTING,
    }
)
BROKER_GROUNDED = frozenset({S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED})

TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.CREATED: frozenset({S.VALIDATED, S.REJECTED}),
    S.VALIDATED: frozenset({S.AUTHORIZED, S.REJECTED}),
    S.AUTHORIZED: frozenset({S.SUBMITTING, S.REJECTED, S.UNKNOWN_ORDER_STATE}),
    S.SUBMITTING: frozenset({S.SUBMITTED, S.REJECTED, S.UNKNOWN_ORDER_STATE}),
    S.SUBMITTED: frozenset(
        {S.ACKNOWLEDGED, S.REJECTED, S.PARTIALLY_FILLED, S.FILLED, S.UNKNOWN_ORDER_STATE}
    ),
    S.ACKNOWLEDGED: frozenset(
        {
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.CANCEL_PENDING,
            S.REPLACE_PENDING,
            S.EXPIRED,
            S.CANCELLED,
            S.UNKNOWN_ORDER_STATE,
        }
    ),
    S.PARTIALLY_FILLED: frozenset(
        {
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.CANCEL_PENDING,
            S.EXPIRED,
            S.CANCELLED,
            S.UNKNOWN_ORDER_STATE,
        }
    ),
    S.CANCEL_PENDING: frozenset(
        {
            S.CANCELLED,
            S.FILLED,
            S.PARTIALLY_FILLED,
            S.EXPIRED,
            S.CANCEL_PENDING,
            S.UNKNOWN_ORDER_STATE,
        }
    ),
    S.REPLACE_PENDING: frozenset(
        {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.UNKNOWN_ORDER_STATE}
    ),
    S.UNKNOWN_ORDER_STATE: frozenset(
        {
            S.ACKNOWLEDGED,
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.REJECTED,
            S.CANCELLED,
            S.EXPIRED,
            S.CANCEL_PENDING,
        }
    ),
    S.FILLED: frozenset(),
    S.CANCELLED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in TRANSITIONS[current]


@dataclass(frozen=True)
class OrderRow:
    id: uuid.UUID
    intent_id: uuid.UUID
    approval_id: uuid.UUID | None
    parent_order_id: uuid.UUID | None
    purpose: str
    account_id: uuid.UUID
    instrument_id: uuid.UUID
    client_order_id: str
    broker_order_id: str | None
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal | None
    time_in_force: str
    expires_at: datetime | None
    status: OrderStatus
    status_version: int
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    fees_paid: Decimal
    reduce_only: bool
    is_simulated: bool
    created_at: datetime
    submitted_at: datetime | None
    unknown_since: datetime | None
    acknowledged_at: datetime | None = None
    last_broker_sync_at: datetime | None = None  # the venue last confirmed this order

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled_qty


def _row(r: Any) -> OrderRow:
    return OrderRow(
        id=r.id,
        intent_id=r.intent_id,
        approval_id=r.approval_id,
        parent_order_id=r.parent_order_id,
        purpose=r.purpose,
        account_id=r.account_id,
        instrument_id=r.instrument_id,
        client_order_id=r.client_order_id,
        broker_order_id=r.broker_order_id,
        side=r.side,
        order_type=r.order_type,
        quantity=r.quantity,
        limit_price=r.limit_price,
        stop_price=r.stop_price,
        time_in_force=r.time_in_force,
        expires_at=r.expires_at,
        status=OrderStatus(r.status),
        status_version=r.status_version,
        filled_qty=r.filled_qty,
        avg_fill_price=r.avg_fill_price,
        fees_paid=r.fees_paid,
        reduce_only=r.reduce_only,
        is_simulated=r.is_simulated,
        created_at=r.created_at,
        submitted_at=r.submitted_at,
        unknown_since=r.unknown_since,
        acknowledged_at=r.acknowledged_at,
        last_broker_sync_at=r.last_broker_sync_at,
    )


async def get_order(conn: AsyncConnection, order_id: uuid.UUID, *, lock: bool = False) -> OrderRow:
    q = select(orders).where(orders.c.id == order_id)
    if lock:
        q = q.with_for_update()
    row = (await conn.execute(q)).first()
    if row is None:
        raise ConflictError("order not found")
    return _row(row)


async def list_orders(
    conn: AsyncConnection,
    *,
    account_id: uuid.UUID | None = None,
    statuses: frozenset[OrderStatus] | None = None,
    intent_id: uuid.UUID | None = None,
) -> list[OrderRow]:
    q = select(orders)
    if account_id is not None:
        q = q.where(orders.c.account_id == account_id)
    if statuses is not None:
        q = q.where(orders.c.status.in_([s.value for s in statuses]))
    if intent_id is not None:
        q = q.where(orders.c.intent_id == intent_id)
    return [_row(r) for r in await conn.execute(q.order_by(orders.c.created_at))]


async def _next_seq(conn: AsyncConnection, order_id: uuid.UUID) -> int:
    value = await conn.scalar(
        select(func.coalesce(func.max(order_events.c.seq), 0)).where(
            order_events.c.order_id == order_id
        )
    )
    return int(value or 0) + 1


async def record_event(
    conn: AsyncConnection,
    *,
    order_id: uuid.UUID,
    from_status: OrderStatus | None,
    to_status: OrderStatus,
    source: Source,
    detail: dict[str, Any] | None,
    now: datetime,
) -> None:
    await conn.execute(
        order_events.insert().values(
            id=uuid7(),
            order_id=order_id,
            seq=await _next_seq(conn, order_id),
            from_status=from_status.value if from_status else None,
            to_status=to_status.value,
            source=source.value,
            detail=redact(to_jsonable(detail or {})),
            occurred_at=now,
        )
    )


async def create(
    conn: AsyncConnection,
    *,
    values: dict[str, Any],
    now: datetime,
    detail: dict[str, Any] | None = None,
) -> uuid.UUID:
    order_id = uuid7()
    await conn.execute(
        orders.insert().values(
            id=order_id,
            status=S.CREATED.value,
            status_version=1,
            created_at=now,
            updated_at=now,
            **values,
        )
    )
    await record_event(
        conn,
        order_id=order_id,
        from_status=None,
        to_status=S.CREATED,
        source=Source.LOCAL,
        detail=detail,
        now=now,
    )
    return order_id


async def transition(
    conn: AsyncConnection,
    order: OrderRow,
    target: OrderStatus,
    *,
    source: Source,
    now: datetime,
    detail: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> OrderRow:
    """Move ``order`` to ``target`` if allowed; raises ConflictError on illegal or raced moves."""
    if not can_transition(order.status, target):
        raise ConflictError(f"illegal order transition {order.status.value} → {target.value}")
    if target in BROKER_GROUNDED and source is Source.LOCAL:
        raise ConflictError(f"{target.value} requires broker or reconciliation evidence")
    values: dict[str, Any] = {
        "status": target.value,
        "status_version": order.status_version + 1,
        "updated_at": now,
        **(extra or {}),
    }
    if target is S.UNKNOWN_ORDER_STATE:
        values.setdefault("unknown_since", now)
    elif order.status is S.UNKNOWN_ORDER_STATE:
        values["unknown_since"] = None
    result = await conn.execute(
        update(orders)
        .where(orders.c.id == order.id, orders.c.status_version == order.status_version)
        .values(**values)
    )
    if result.rowcount != 1:
        raise ConflictError("order changed concurrently; reload before acting")
    await record_event(
        conn,
        order_id=order.id,
        from_status=order.status,
        to_status=target,
        source=source,
        detail=detail,
        now=now,
    )
    return await get_order(conn, order.id)


async def update_fill_totals(
    conn: AsyncConnection,
    order: OrderRow,
    *,
    filled_qty: Decimal,
    avg_price: Decimal | None,
    fees: Decimal,
    fee_currency: str,
    now: datetime,
) -> OrderRow:
    result = await conn.execute(
        update(orders)
        .where(orders.c.id == order.id, orders.c.status_version == order.status_version)
        .values(
            filled_qty=filled_qty,
            avg_fill_price=avg_price,
            fees_paid=fees,
            fee_currency=fee_currency,
            status_version=order.status_version + 1,
            updated_at=now,
            last_broker_sync_at=now,
        )
    )
    if result.rowcount != 1:
        raise ConflictError("order changed concurrently; reload before acting")
    return await get_order(conn, order.id)


async def count_unknown(conn: AsyncConnection, account_id: uuid.UUID) -> int:
    return int(
        await conn.scalar(
            select(func.count())
            .select_from(orders)
            .where(
                orders.c.account_id == account_id, orders.c.status == S.UNKNOWN_ORDER_STATE.value
            )
        )
        or 0
    )


async def events(conn: AsyncConnection, order_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(order_events).where(order_events.c.order_id == order_id).order_by(order_events.c.seq)
    )
    return [dict(r._mapping) for r in rows]
