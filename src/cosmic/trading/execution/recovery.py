"""Cancellation requests and restart recovery (AT-20).

Uncertain orders are resolved from venue evidence and never resubmitted.
"""

from __future__ import annotations

import uuid

from cosmic.audit.service import Actor, record
from cosmic.core.errors import ConflictError
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident
from cosmic.trading import orders
from cosmic.trading.accounts import get_account, set_reconciliation_status
from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
)
from cosmic.trading.execution.base import ExecutionBase
from cosmic.trading.execution.common import (
    ACTOR,
    Outcome,
    S,
    log,
)
from cosmic.trading.orders import Source
from cosmic.trading.proposals import (
    set_intent_status,
)


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
            o = await orders.transition(
                conn, o, S.CANCEL_PENDING, source=Source.LOCAL, now=now, detail={"reason": reason}
            )
            account = await get_account(conn, account_id=o.account_id)
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
            await self.brokers.for_account(account).cancel_order(account.id, o.client_order_id)
        except (BrokerAmbiguousError, BrokerUnavailableError) as exc:
            log.warning("cancel_unconfirmed", order=o.client_order_id, error=str(exc))
        await self.sync_order(order_id)
        return Outcome("CANCEL_PENDING", reason, order_id)

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
