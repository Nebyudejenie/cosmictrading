"""Synchronisation with the venue: every state change is broker-grounded (V6 §1812)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import record
from cosmic.core.money import ZERO
from cosmic.db.schema_execution import approvals
from cosmic.db.schema_execution import orders as orders_t
from cosmic.market import data as md
from cosmic.notify.alerts import Alert
from cosmic.notify.outbox import notify_owners
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.trading import orders
from cosmic.trading.accounts import get_account, set_reconciliation_status
from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
    VenueOrder,
)
from cosmic.trading.execution.base import ExecutionBase
from cosmic.trading.execution.common import (
    ACTOR,
    GONE_IF_MISSING,
    NOT_FOUND_GRACE,
    PROTECTIVE,
    S,
    StepErrors,
    path_to,
    target_from_venue,
)
from cosmic.trading.fills import FillAnomalyError, apply_fill
from cosmic.trading.orders import TERMINAL, OrderRow, OrderStatus, Source
from cosmic.trading.proposals import (
    set_intent_status,
)
from cosmic.trading.review import create_review_if_closed


class OrderSync(ExecutionBase):
    async def poll_active(self) -> int:
        async with self.db.connect() as conn:
            ids = [
                r.id
                for r in await conn.execute(
                    select(orders_t.c.id)
                    .where(
                        orders_t.c.status.in_(
                            [
                                s.value
                                for s in (
                                    S.SUBMITTED,
                                    S.ACKNOWLEDGED,
                                    S.PARTIALLY_FILLED,
                                    S.CANCEL_PENDING,
                                    S.REPLACE_PENDING,
                                    S.UNKNOWN_ORDER_STATE,
                                )
                            ]
                        )
                    )
                    .order_by(orders_t.c.created_at)
                )
            ]
        errors = StepErrors()
        for oid in ids:  # one order (one account's adapter) must not stop the others' sync
            try:
                await self.sync_order(oid)
            except Exception as exc:
                errors.note("sync_order", exc, order_id=str(oid))
        errors.raise_first()
        return len(ids)

    async def sync_order(self, order_id: uuid.UUID, *, source: Source = Source.BROKER) -> OrderRow:
        async with self.db.connect() as conn:
            o = await orders.get_order(conn, order_id)
            account = await get_account(conn, account_id=o.account_id)
        seen = o.status_version
        broker = self.brokers.for_account(account)
        venue: VenueOrder | None
        try:
            venue = await broker.get_order(account.id, o.client_order_id)
            venue_fills = [
                f
                for f in await broker.get_fills(account.id, since=o.created_at)
                if f.client_order_id == o.client_order_id
            ]
        except BrokerOrderNotFoundError:
            venue, venue_fills = None, []
        except (BrokerUnavailableError, BrokerAmbiguousError):
            return o  # cannot verify now; state unchanged (still blocks if UNKNOWN)
        if venue is not None:
            self.missing_since.pop(order_id, None)  # the venue knows it (again)
        now = self.clock.now()
        async with self.db.transaction() as conn:
            o = await orders.get_order(conn, order_id, lock=True)
            if o.status_version != seen:
                # Our record changed while the venue was being read (a cancel, or another sync
                # applied a newer view): this venue view may be older than our record. The next
                # sync reads the venue again.
                return o
            inst = await md.instrument_by_id(conn, o.instrument_id)
            new_fills = 0
            try:
                for f in sorted(venue_fills, key=lambda x: x.filled_at):
                    o, created = await apply_fill(
                        conn,
                        order=o,
                        fill=f,
                        inst=inst,
                        source="BROKER" if source is Source.BROKER else "RECONCILE",
                        now=now,
                    )
                    new_fills += int(created)
            except FillAnomalyError:
                await set_reconciliation_status(
                    conn, account_id=account.id, status="FAILED", now=now
                )
                return o
            if venue is None:
                since = o.unknown_since or o.submitted_at or o.created_at
                if (
                    o.status in (S.AUTHORIZED, S.SUBMITTING, S.SUBMITTED, S.UNKNOWN_ORDER_STATE)
                    and now - since >= NOT_FOUND_GRACE
                ):
                    o = await self._apply_path(
                        conn,
                        o,
                        S.REJECTED,
                        Source.RECONCILE,
                        now,
                        {"reason": "venue has no record of this client order ID"},
                    )
                    await self._after_terminal(conn, o, now)
                elif o.status in GONE_IF_MISSING:
                    first = self.missing_since.setdefault(o.id, now)
                    if now - first >= NOT_FOUND_GRACE:
                        o = await self._missing_at_venue(conn, o, now)
                return o
            target = target_from_venue(o.status, venue, o.filled_qty, o.quantity)
            if target == o.status and not (target is S.PARTIALLY_FILLED and new_fills):
                await conn.execute(
                    update(orders_t).where(orders_t.c.id == o.id).values(last_broker_sync_at=now)
                )
                return o
            before = o.status
            o = await self._apply_path(
                conn,
                o,
                target,
                source,
                now,
                {
                    "venue_status": venue.status.value,
                    "venue_filled": venue.filled_qty,
                    "new_fills": new_fills,
                },
                venue_order_id=venue.venue_order_id,
            )
            if (
                o.purpose in PROTECTIVE
                and o.status in (S.CANCELLED, S.EXPIRED)
                and before is not S.CANCEL_PENDING
            ):
                await self._protection_ended_at_venue(conn, o, now)
            if o.status in TERMINAL:
                await self._after_terminal(conn, o, now)
            if new_fills:
                await notify_owners(
                    conn,
                    kind="order_fill",
                    now=now,
                    dedup_key=f"fill:{o.id}:{o.filled_qty}",
                    payload={
                        "client_order_id": o.client_order_id,
                        "status": o.status.value,
                        "filled_qty": o.filled_qty,
                        "avg_price": o.avg_fill_price,
                        "simulated": o.is_simulated,
                        "basis": "BROKER_CONFIRMED",
                    },
                )
        return o

    async def _apply_path(
        self,
        conn: AsyncConnection,
        o: OrderRow,
        target: OrderStatus,
        source: Source,
        now: datetime,
        detail: dict[str, Any],
        venue_order_id: str | None = None,
    ) -> OrderRow:
        path = path_to(o.status, target)
        if path is None:
            await raise_incident(
                conn,
                dedup_key=f"trading.illegal_transition:{o.id}",
                category=Category.TRADING,
                alert=Alert(
                    title=f"Venue state {target.value} cannot follow {o.status.value}",
                    severity="CRITICAL",
                    what_happened=f"The venue reports order {o.client_order_id} as "
                    f"{target.value}, which cannot follow our recorded {o.status.value}.",
                    verified="Both states are as stated; the transition is not in the order "
                    "state machine.",
                    uncertain="Which record is wrong.",
                    risk_now="This order's true state is unknown to the system.",
                    action_taken="The order state was NOT changed.",
                    owner_action="Compare the venue's record with the order page and reconcile; "
                    "report a venue-side error to the venue.",
                ),
                now=now,
                detail={"order": o.client_order_id},
            )
            return o
        for step in path:
            src = Source.LOCAL if step is S.UNKNOWN_ORDER_STATE else source
            extra: dict[str, Any] = {"last_broker_sync_at": now}
            if venue_order_id and o.broker_order_id is None and step is not S.UNKNOWN_ORDER_STATE:
                extra["broker_order_id"] = venue_order_id  # learned from venue evidence
                extra["acknowledged_at"] = now
            o = await orders.transition(
                conn, o, step, source=src, now=now, detail=detail, extra=extra
            )
        return o

    async def _missing_at_venue(
        self, conn: AsyncConnection, o: OrderRow, now: datetime
    ) -> OrderRow:
        """The venue has answered "no such order" for more than a minute, as this process saw
        it, for an order it had acknowledged (the Binance test network deletes every order when
        it resets, about monthly). It can no longer fill, so it is ended on that evidence:
        CANCELLED if we had asked to cancel it, EXPIRED otherwise. A protective order is then
        replaced under its plan. Fills it may have had before it vanished are not invented; the
        next reconciliation compares balances."""
        self.missing_since.pop(o.id, None)
        before = o.status
        o = await self._apply_path(
            conn,
            o,
            S.CANCELLED if before is S.CANCEL_PENDING else S.EXPIRED,
            Source.RECONCILE,
            now,
            {"reason": "the venue has no record of this order"},
        )
        await raise_incident(
            conn,
            dedup_key=f"trading.order_missing_at_venue:{o.id}",
            category=Category.TRADING,
            alert=Alert(
                title=f"{o.client_order_id}: the venue no longer has this order",
                severity="WARNING",
                what_happened=f"The venue answers that it has no order {o.client_order_id} "
                f"({o.purpose}), which it had acknowledged. Our record said {before.value}.",
                verified="The venue gave that answer for more than a minute.",
                uncertain="Whether the order filled before it disappeared. Its recorded fills "
                "are kept; none are invented.",
                risk_now="None from this order: it can no longer fill. A protective order is "
                "replaced under its approved plan on the next cycle.",
                action_taken=f"The order is recorded as {o.status.value}.",
                owner_action="Check the venue's order and trade history. A test-network reset "
                "explains it; otherwise contact the venue. The next reconciliation compares "
                "balances and positions.",
            ),
            now=now,
            detail={"order": o.client_order_id, "was": before.value},
        )
        await self._after_terminal(conn, o, now)
        return o

    async def _protection_ended_at_venue(
        self, conn: AsyncConnection, o: OrderRow, now: datetime
    ) -> None:
        """A protective order the venue ended although nobody here asked: possibly a manual
        action in the venue's own app, the venue itself, or someone else with access."""
        await raise_incident(
            conn,
            dedup_key=f"trading.protection_ended_at_venue:{o.id}",
            category=Category.TRADING,
            alert=Alert(
                title=f"{o.client_order_id}: protective order {o.status.value} at the venue, "
                "not by COSMICTRADING",
                severity="WARNING",
                what_happened=f"The venue reports {o.client_order_id} as {o.status.value}; "
                "COSMICTRADING did not ask for that.",
                verified="The venue's own record of the order says so.",
                uncertain="Who or what ended it: a manual action in the venue's app, the venue "
                "itself, or someone else with access to the account.",
                risk_now="Until a replacement is accepted, this order no longer protects the "
                "position.",
                action_taken="A replacement under the same approved plan is placed on the next "
                "cycle; the stop level is not changed.",
                owner_action="Check the venue's own order history. If you did not do it, treat "
                "the account's access as compromised and rotate its keys or password.",
            ),
            now=now,
            detail={"order": o.client_order_id, "status": o.status.value},
        )

    async def _after_terminal(self, conn: AsyncConnection, o: OrderRow, now: datetime) -> None:
        if o.purpose == "ENTRY":
            if o.filled_qty > ZERO:
                await set_intent_status(
                    conn,
                    o.intent_id,
                    "EXECUTED",
                    None
                    if o.status is S.FILLED
                    else f"partially filled; remainder {o.status.value}",
                    now,
                    expected=("EXECUTING",),
                )
            else:
                await set_intent_status(
                    conn,
                    o.intent_id,
                    "EXECUTION_FAILED",
                    f"entry {o.status.value} unfilled",
                    now,
                    expected=("EXECUTING",),
                )
        elif o.purpose == "EXIT":
            await self._exit_settled(conn, o, now)
        elif o.status is S.REJECTED:
            await self._protection_failed(conn, o, "protective order rejected", now)
        await resolve_by_key(
            conn,
            dedup_key=f"trading.cancel_unconfirmed:{o.id}",
            now=now,
            resolution=f"the venue reports the order {o.status.value}",
        )
        await record(
            conn,
            action="trade.order_final",
            actor=ACTOR,
            occurred_at=now,
            entity_type="order",
            entity_id=o.id,
            payload={
                "status": o.status.value,
                "filled_qty": o.filled_qty,
                "avg_fill_price": o.avg_fill_price,
                "fees": o.fees_paid,
            },
        )
        await create_review_if_closed(conn, intent_id=o.intent_id, now=now)

    async def _exit_settled(self, conn: AsyncConnection, o: OrderRow, now: datetime) -> None:
        """Close the exit proposal once every order of its approval is final.

        EXECUTED if anything sold; otherwise EXECUTION_FAILED — and because no exit order is
        active any more, protection management places the stops again on the next cycle.
        """
        exit_intent_id = await conn.scalar(
            select(approvals.c.intent_id).where(approvals.c.id == o.approval_id)
        )
        if exit_intent_id is None:
            return
        siblings = list(
            await conn.execute(
                select(orders_t.c.status, orders_t.c.filled_qty, orders_t.c.quantity).where(
                    orders_t.c.approval_id == o.approval_id, orders_t.c.purpose == "EXIT"
                )
            )
        )
        if any(OrderStatus(r.status) not in TERMINAL for r in siblings):
            return
        sold = sum((r.filled_qty for r in siblings), ZERO)
        wanted = sum((r.quantity for r in siblings), ZERO)
        if sold > ZERO:
            await set_intent_status(
                conn,
                exit_intent_id,
                "EXECUTED",
                None if sold >= wanted else f"partially filled: sold {sold} of {wanted}",
                now,
                expected=("EXECUTING",),
            )
        else:
            await set_intent_status(
                conn,
                exit_intent_id,
                "EXECUTION_FAILED",
                f"exit {o.status.value} unfilled; the protective stop is placed again",
                now,
                expected=("EXECUTING",),
            )
