"""Cancellation requests and restart recovery (AT-20).

Uncertain orders are resolved from venue evidence and never resubmitted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from cosmic.audit.service import Actor, record
from cosmic.core.errors import ConflictError
from cosmic.db.schema import audit_events, incidents
from cosmic.db.schema_execution import orders as orders_t
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident
from cosmic.trading import killswitch, orders
from cosmic.trading.accounts import get_account, set_reconciliation_status
from cosmic.trading.authority import get_state
from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
)
from cosmic.trading.execution.base import ExecutionBase
from cosmic.trading.execution.common import (
    ACTIVE_STATES,
    ACTOR,
    Outcome,
    S,
    StepErrors,
    log,
)
from cosmic.trading.modes import Mode
from cosmic.trading.orders import Source
from cosmic.trading.proposals import (
    set_intent_status,
)

STOP_ACTOR = Actor.system("killswitch")
CANCEL_SENT = ("trade.cancel_requested", "trade.cancel_resent")
CANCEL_RESEND_AFTER = timedelta(minutes=1)  # spacing of the first sends
CANCEL_RESEND_SLOW = timedelta(minutes=5)  # spacing once the owner has been told
CANCEL_ALERT_AFTER = 5  # sends (the request and four re-sends) before the owner is told


def _alert_key(order_id: uuid.UUID) -> str:
    return f"trading.cancel_unconfirmed:{order_id}"


class CancelAndRecovery(ExecutionBase):
    async def request_cancel(
        self, order_id: uuid.UUID, *, reason: str, actor: Actor | None = None
    ) -> Outcome:
        """Cancel request ≠ cancellation: CANCEL_PENDING until the venue confirms."""
        now = self.clock.now()
        async with self.db.transaction() as conn:
            o = await orders.get_order(conn, order_id, lock=True)
            if o.status not in (S.ACKNOWLEDGED, S.PARTIALLY_FILLED):
                return Outcome("SKIPPED", f"order is {o.status.value}", order_id)
            account = await get_account(conn, account_id=o.account_id)
            broker = self.brokers.for_account(account)  # no adapter here: nothing is recorded
            o = await orders.transition(
                conn, o, S.CANCEL_PENDING, source=Source.LOCAL, now=now, detail={"reason": reason}
            )
            await record(
                conn,
                action="trade.cancel_requested",
                actor=actor or ACTOR,
                occurred_at=now,
                entity_type="order",
                entity_id=order_id,
                payload={"reason": reason},
            )
        try:
            await broker.cancel_order(account.id, o.client_order_id)
        except (BrokerAmbiguousError, BrokerUnavailableError, BrokerOrderNotFoundError) as exc:
            # Unconfirmed: the venue's state decides, and follow_up_cancels sends it again.
            log.warning("cancel_unconfirmed", order=o.client_order_id, error=str(exc))
        await self.sync_order(order_id)
        return Outcome("CANCEL_PENDING", reason, order_id)

    async def cancel_entries_under_stop(self) -> int:
        """While the emergency stop is active, no entry stays open at a venue (when the
        emergency policy cancels unfilled entries). The stop cancels the entries it finds; this
        catches the ones it could not: an entry on its way when the stop was pressed, or an
        unknown outcome later found resting at the venue."""
        async with self.db.connect() as conn:
            if (await get_state(conn)).mode is not Mode.EMERGENCY_STOP:
                return 0
            if not (await killswitch.emergency_policy(conn)).cancel_unfilled_entries:
                return 0
            rows = list(
                await conn.execute(
                    select(orders_t.c.id, orders_t.c.account_id).where(
                        orders_t.c.purpose == "ENTRY",
                        orders_t.c.status.in_(killswitch.OPEN_AT_VENUE),
                    )
                )
            )
            reachable = [
                r.id
                for r in rows
                if self.brokers.serves(await get_account(conn, account_id=r.account_id))
            ]
        errors = StepErrors()
        for order_id in reachable:
            try:
                await self.request_cancel(
                    order_id, reason="emergency stop: cancel unfilled entry", actor=STOP_ACTOR
                )
            except Exception as exc:
                errors.note("stop_cancel", exc, order_id=str(order_id))
        errors.raise_first()
        return len(reachable)

    async def follow_up_cancels(self) -> int:
        """A cancel the venue has not confirmed is sent again for as long as the order stays
        cancel pending: a minute apart for the first five sends, then every five minutes. A
        second cancel of the same order is harmless at each venue, except an HFM stop's while a
        newer stop of the trade holds the shared stop-loss (_newer_stop_holds_it). A lost request
        used to leave the order open there for good. After the fifth send an incident tells the
        owner. An order the venue no longer has is ended by the sync (_missing_at_venue)."""
        now = self.clock.now()
        async with self.db.connect() as conn:
            pending = list(
                await conn.execute(
                    select(
                        orders_t.c.id,
                        orders_t.c.intent_id,
                        orders_t.c.account_id,
                        orders_t.c.client_order_id,
                        orders_t.c.purpose,
                    ).where(orders_t.c.status == S.CANCEL_PENDING.value)
                )
            )
            if not pending:
                return 0
            history = {
                r.entity_id: (int(r.sends), r.last)
                for r in await conn.execute(
                    select(
                        audit_events.c.entity_id,
                        func.count().label("sends"),
                        func.max(audit_events.c.occurred_at).label("last"),
                    )
                    .where(
                        audit_events.c.entity_type == "order",
                        audit_events.c.entity_id.in_([str(p.id) for p in pending]),
                        audit_events.c.action.in_(CANCEL_SENT),
                    )
                    .group_by(audit_events.c.entity_id)
                )
            }
            alerted = set(
                await conn.scalars(
                    select(incidents.c.dedup_key).where(
                        incidents.c.dedup_key.in_([_alert_key(p.id) for p in pending]),
                        incidents.c.status != "RESOLVED",
                    )
                )
            )
            accounts = {
                p.account_id: await get_account(conn, account_id=p.account_id) for p in pending
            }
        resent = 0
        errors = StepErrors()
        for p in pending:
            account = accounts[p.account_id]
            if not self.brokers.serves(account) or await self._newer_stop_holds_it(p, account):
                continue
            sends, last = history.get(str(p.id), (0, None))
            waited = None if last is None else now - last
            try:
                if (
                    sends >= CANCEL_ALERT_AFTER
                    and _alert_key(p.id) not in alerted
                    and (waited is None or waited >= CANCEL_RESEND_AFTER)
                ):
                    await self._cancel_unconfirmed(p, sends, now)
                spacing = CANCEL_RESEND_AFTER if sends < CANCEL_ALERT_AFTER else CANCEL_RESEND_SLOW
                if (waited is None or waited >= spacing) and await self._resend_cancel(
                    p, account, sends, now
                ):
                    resent += 1
            except Exception as exc:
                errors.note("follow_up_cancel", exc, order_id=str(p.id))
        errors.raise_first()
        return resent

    async def _newer_stop_holds_it(self, p: Any, account: Any) -> bool:
        """On a venue with one stop-loss per position (HFM), every stop order of an entry sets
        the same stop, and a cancel clears it for all of them. An old stop's cancel is then not
        sent again while another stop of the same trade is active and not being cancelled
        itself: it would remove the protection that stop holds."""
        if (
            p.purpose != "STOP_LOSS"
            or not self.brokers.for_account(account).capabilities().stops_per_position
        ):
            return False
        holding = [s.value for s in ACTIVE_STATES if s is not S.CANCEL_PENDING]
        async with self.db.connect() as conn:
            others = await conn.scalar(
                select(func.count())
                .select_from(orders_t)
                .where(
                    orders_t.c.intent_id == p.intent_id,
                    orders_t.c.purpose == "STOP_LOSS",
                    orders_t.c.id != p.id,
                    orders_t.c.status.in_(holding),
                )
            )
        return bool(others)

    async def _resend_cancel(self, p: Any, account: Any, sends: int, now: datetime) -> bool:
        async with self.db.transaction() as conn:
            o = await orders.get_order(conn, p.id, lock=True)
            if o.status is not S.CANCEL_PENDING:
                return False  # settled since it was listed
            await record(
                conn,
                action="trade.cancel_resent",
                actor=ACTOR,
                occurred_at=now,
                entity_type="order",
                entity_id=p.id,
                payload={"send": sends + 1},
            )
        try:
            await self.brokers.for_account(account).cancel_order(account.id, p.client_order_id)
        except (BrokerAmbiguousError, BrokerUnavailableError, BrokerOrderNotFoundError) as exc:
            log.warning("cancel_resend_unconfirmed", order=p.client_order_id, error=str(exc))
        await self.sync_order(p.id)
        return True

    async def _cancel_unconfirmed(self, p: Any, sends: int, now: datetime) -> None:
        key = _alert_key(p.id)
        entry = p.purpose == "ENTRY"
        async with self.db.transaction() as conn:
            # The row lock serialises with sync_order, which resolves this incident when the
            # order ends: an order settled meanwhile never gets a fresh alert.
            o = await orders.get_order(conn, p.id, lock=True)
            if o.status is not S.CANCEL_PENDING:
                return
            if await conn.scalar(
                select(incidents.c.id).where(
                    incidents.c.dedup_key == key, incidents.c.status != "RESOLVED"
                )
            ):
                return
            await raise_incident(
                conn,
                dedup_key=key,
                category=Category.TRADING,
                alert=Alert(
                    title="A cancel was not confirmed by the venue",
                    severity="CRITICAL" if entry else "WARNING",
                    what_happened=f"A cancel for order {p.client_order_id} ({p.purpose}) was "
                    f"sent {sends} times, a minute apart, and the venue has not confirmed it. "
                    "The requests may not have reached the venue.",
                    verified="Our record shows the order as cancel pending.",
                    uncertain="Whether the order is still open at the venue. It can still fill.",
                    risk_now=(
                        "An open entry can still fill and add exposure."
                        if entry
                        else "The order can still execute at the venue."
                    ),
                    action_taken="The cancel keeps being sent every 5 minutes until the venue "
                    "confirms it.",
                    owner_action="If you can, cancel the order in the venue's own app (HFM: "
                    "MetaTrader 5), then press Reconcile on Portfolio. This closes by itself "
                    "once the venue reports the order finished.",
                ),
                now=now,
                detail={"order_id": str(p.id), "sends": sends},
            )

    async def startup_recovery(self) -> int:
        """Orders whose transmission state is uncertain are resolved from venue evidence.

        AUTHORIZED orders were never marked SUBMITTING, so they were never transmitted (we
        persist SUBMITTING first). They are closed as REJECTED once the venue confirms it
        has no such order. SUBMITTING/SUBMITTED orders become UNKNOWN_ORDER_STATE and are
        reconciled by client order ID; nothing is ever resubmitted automatically.
        """
        now = self.clock.now()
        async with self.db.connect() as conn:
            rows = await orders.list_orders(
                conn, statuses=frozenset({S.AUTHORIZED, S.SUBMITTING, S.SUBMITTED})
            )
        affected = 0
        for o in rows:
            async with self.db.connect() as conn:
                account = await get_account(conn, account_id=o.account_id)
            venue_has_it: bool | None
            try:
                await self.brokers.for_account(account).get_order(account.id, o.client_order_id)
                venue_has_it = True
            except BrokerOrderNotFoundError:
                venue_has_it = False
            except (BrokerUnavailableError, BrokerAmbiguousError, ConflictError):
                venue_has_it = None
            async with self.db.transaction() as conn:
                o = await orders.get_order(conn, o.id, lock=True)
                if o.status is S.AUTHORIZED and venue_has_it is False:
                    await orders.transition(
                        conn,
                        o,
                        S.REJECTED,
                        source=Source.RECONCILE,
                        now=now,
                        detail={"reason": "never transmitted (process restarted before sending)"},
                    )
                    if o.purpose == "ENTRY":
                        await set_intent_status(
                            conn,
                            o.intent_id,
                            "EXECUTION_FAILED",
                            "interrupted before transmission; propose again",
                            now,
                        )
                    elif o.purpose == "EXIT":
                        await self._exit_settled(conn, o, now)
                else:
                    await orders.transition(
                        conn,
                        o,
                        S.UNKNOWN_ORDER_STATE,
                        source=Source.LOCAL,
                        now=now,
                        detail={"reason": "process restarted while the order was in flight"},
                    )
                    await set_reconciliation_status(
                        conn, account_id=o.account_id, status="REQUIRED", now=now
                    )
                affected += 1
        if affected:
            async with self.db.transaction() as conn:
                await raise_incident(
                    conn,
                    dedup_key="trading.restart_uncertain_orders",
                    category=Category.TRADING,
                    alert=Alert(
                        title=f"{affected} order(s) were in flight at restart; reconciled from "
                        "venue",
                        severity="WARNING",
                        what_happened=f"The execution authority restarted while {affected} "
                        "order(s) were between authorisation and a venue answer.",
                        verified="Orders never transmitted were closed as REJECTED; the others "
                        "were marked UNKNOWN_ORDER_STATE.",
                        uncertain="The final venue state of the UNKNOWN orders until "
                        "reconciliation finishes.",
                        risk_now="New exposure is blocked on affected accounts until they "
                        "reconcile.",
                        action_taken="Each order is being resolved from venue evidence by client "
                        "order ID; nothing is resubmitted.",
                        owner_action="Check the Orders page after a few minutes; investigate why "
                        "the worker restarted if it was not planned.",
                    ),
                    now=now,
                    detail={"count": affected},
                )
                await record(
                    conn,
                    action="trade.startup_recovery",
                    actor=ACTOR,
                    occurred_at=now,
                    payload={"orders_resolved_or_unknown": affected},
                )
            for o in rows:
                await self.sync_order(o.id, source=Source.RECONCILE)
        return affected
