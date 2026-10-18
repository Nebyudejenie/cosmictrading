"""Approved position management (V6 §1810, §1822).

Protective stops, take-profit and cancel-unfilled — only the actions in the approved plan.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import record
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema_execution import approvals, post_trade_reviews, trade_intents
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident
from cosmic.trading import orders
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.authority import get_state
from cosmic.trading.context_builder import build_context
from cosmic.trading.execution.base import ExecutionBase
from cosmic.trading.execution.common import (
    ACTIVE_STATES,
    ACTOR,
    Outcome,
    S,
    StepErrors,
)
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, Operation
from cosmic.trading.orders import OrderRow, Source
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import (
    ManagementPlan,
    get_intent,
    record_risk_run,
    sellable,
)
from cosmic.trading.risk.context import Effect, OrderRequest, Stage
from cosmic.trading.risk.engine import evaluate

# Stop orders a newer stop may replace on a venue with one stop-loss per position.
REPLACEABLE = frozenset({S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.CANCEL_PENDING})


class ProtectionManager(ExecutionBase):
    async def manage_positions(self) -> int:
        async with self.db.connect() as conn:
            intents = list(
                await conn.execute(
                    select(trade_intents).where(
                        trade_intents.c.status.in_(["EXECUTING", "EXECUTED"]),
                        trade_intents.c.id.notin_(select(post_trade_reviews.c.intent_id)),
                    )
                )
            )
        actions = 0
        errors = StepErrors()
        for intent in intents:  # one position's failure must not leave the others unprotected
            try:
                actions += await self._manage_intent(intent)
            except Exception as exc:
                errors.note("manage_intent", exc, intent_id=str(intent.id))
        errors.raise_first()
        return actions

    async def _manage_intent(self, intent: Any) -> int:
        plan = ManagementPlan.model_validate(intent.management_plan)
        now = self.clock.now()
        async with self.db.connect() as conn:
            _, policy = await active_policy(conn)
            intent_orders = await orders.list_orders(conn, intent_id=intent.id)
            held = await self._intent_held(conn, intent.id)
            inst = await md.instrument_by_id(conn, intent.instrument_id)
            quote = await md.latest_quote(conn, inst.id, now)
            account = await get_account(conn, account_id=intent.account_id)
        if not self.brokers.serves(account):
            return 0  # no adapter for this account here: nothing is decided or recorded for it
        held = sellable(held, inst.lot_size)  # a remainder below one lot cannot be protected
        entry = next((o for o in intent_orders if o.purpose == "ENTRY"), None)
        if entry is None:
            return 0
        actions = 0
        # 1. Cancel an unfilled entry after the approved time limit.
        if (
            plan.cancel_unfilled_after_seconds
            and entry.status in (S.ACKNOWLEDGED, S.PARTIALLY_FILLED)
            and now >= entry.created_at + timedelta(seconds=plan.cancel_unfilled_after_seconds)
        ):
            await self.request_cancel(
                entry.id, reason="approved plan: cancel unfilled entry after time limit"
            )
            actions += 1
        if held <= ZERO:
            return actions
        if any(o.purpose == "EXIT" and o.status in ACTIVE_STATES for o in intent_orders):
            # An approved exit is replacing this position's protection; if it ends unfilled,
            # this check no longer holds and the stop is placed again on the next cycle.
            return actions
        stops = [o for o in intent_orders if o.purpose == "STOP_LOSS"]
        tps = [o for o in intent_orders if o.purpose == "TAKE_PROFIT"]
        active_stops = [o for o in stops if o.status in ACTIVE_STATES]
        active_tps = [o for o in tps if o.status in ACTIVE_STATES]
        fresh_bid = quote.bid if quote.label is DataLabel.DELAYED else None
        # 2. Take profit (monitored): cancel stops first, then an IOC limit sell at the target.
        if plan.take_profit_price and fresh_bid is not None and fresh_bid >= plan.take_profit_price:
            if active_stops:
                for s in active_stops:
                    if s.status is not S.CANCEL_PENDING:
                        await self.request_cancel(
                            s.id, reason="approved plan: take-profit target reached"
                        )
                        actions += 1
            elif not active_tps:
                await self._place_protective(
                    intent,
                    account,
                    inst,
                    "TAKE_PROFIT",
                    held,
                    limit=plan.take_profit_price,
                    stop=None,
                    tif="IOC",
                    n=len(tps) + 1,
                )
                actions += 1
            return actions
        # 3. Protective stop for any unprotected filled quantity.
        if plan.stop_loss is not None and not active_tps:
            protected = sum((o.remaining for o in active_stops), ZERO)
            delta = held - protected
            rejected = sum(1 for o in stops if o.status is S.REJECTED)
            if delta > ZERO:
                if rejected > policy.protective_retry_max:
                    return (
                        actions  # retry budget exhausted: escalated already, never widen the stop
                    )
                # A venue with one stop-loss per position (HFM) keeps one stop for the whole
                # entry, and gives every stop-loss deal to our newest stop order. So the new stop
                # covers the whole holding and replaces the older ones in our books.
                shared = self.brokers.for_account(account).capabilities().stops_per_position
                if shared and any(o.status not in REPLACEABLE for o in active_stops):
                    return actions  # an older stop is still on its way: wait for its answer
                quantity = held if shared else delta
                notional = quantity * plan.stop_loss.limit_price
                if inst.min_notional is not None and notional < inst.min_notional:
                    async with self.db.transaction() as conn:
                        await self._set_protection(conn, intent, "DEGRADED", now)
                        await raise_incident(
                            conn,
                            dedup_key=f"trading.unprotected_remainder:{intent.id}",
                            category=Category.TRADING,
                            alert=Alert(
                                title=f"{intent.trade_code}: {delta} unprotected "
                                "(below venue minimum)",
                                severity="WARNING",
                                what_happened=f"{delta} of the position has no protective stop "
                                "because a stop for it would be below the venue's minimum size.",
                                verified="The rest of the position is protected by active stops.",
                                uncertain="How the price moves before the remainder is closed "
                                "or protected.",
                                risk_now=f"Losses on {delta} are not limited by any order.",
                                action_taken="Protection marked DEGRADED; the stop level was "
                                "not changed.",
                                owner_action="Decide whether to close the small remainder with "
                                "an approved exit, or accept it unprotected.",
                            ),
                            now=now,
                            detail={"delta": delta},
                        )
                    return actions
                await self._place_protective(
                    intent,
                    account,
                    inst,
                    "STOP_LOSS",
                    quantity,
                    limit=plan.stop_loss.limit_price,
                    stop=plan.stop_loss.stop_price,
                    tif="GTC",
                    n=len(stops) + 1,
                    supersede=tuple(o.id for o in active_stops) if shared else (),
                )
                actions += 1
        return actions

    async def _place_protective(
        self,
        intent: Any,
        account: Account,
        inst: md.InstrumentView,
        purpose: str,
        quantity: Decimal,
        *,
        limit: Decimal,
        stop: Decimal | None,
        tif: str,
        n: int,
        supersede: tuple[uuid.UUID, ...] = (),
    ) -> Outcome:
        now = self.clock.now()
        suffix = "SL" if purpose == "STOP_LOSS" else "TP"
        client_order_id = f"ct-{intent.trade_code}-{suffix}{n}"
        async with self.db.transaction() as conn:
            authority = await get_state(conn, lock="share")
            policy_version, policy = await active_policy(conn)
            approval_id = (
                await conn.execute(
                    select(approvals.c.id).where(
                        approvals.c.intent_id == intent.id, approvals.c.status == "CONSUMED"
                    )
                )
            ).scalar_one_or_none()
            if approval_id is None:
                return Outcome(
                    "BLOCKED", "no consumed approval authorises management of this intent"
                )
            lot = inst.lot_size or Decimal("0.00000001")
            qty = (quantity // lot) * lot
            request = OrderRequest(
                account_alias=account.alias,
                instrument_key=inst.canonical_key,
                side="SELL",
                effect=Effect.REDUCE,
                quantity=qty,
                order_type="LIMIT",
                limit_price=limit,
                time_in_force=tif,
                price_band_low=limit,
                price_band_high=limit,
                max_slippage_bps=ZERO,
            )
            # The stops this one replaces end first, so the reduce-only check (R17) counts the
            # new stop alone; if any check fails they are kept as they were.
            replaced = await conn.begin_nested()
            ended = [await self._supersede(conn, i, client_order_id, now) for i in supersede]
            ctx = await build_context(
                conn,
                account=account,
                inst=inst,
                request=request,
                stage=Stage.PROTECTIVE,
                mode=authority.mode,
                policy_version=policy_version,
                now=now,
            )
            decision = evaluate(ctx, policy)
            if decision.passed:
                await replaced.commit()
                for old in ended:
                    if old is not None:
                        await self._after_terminal(conn, old, now)
            else:
                await replaced.rollback()
            await record_risk_run(
                conn,
                ctx=ctx,
                decision=decision,
                account_id=account.id,
                intent_id=intent.id,
                order_id=None,
                now=now,
            )
            if not decision.passed:
                await self._set_protection(conn, intent, "DEGRADED", now)
                await raise_incident(
                    conn,
                    dedup_key=f"trading.protection_blocked:{intent.id}",
                    category=Category.TRADING,
                    alert=Alert(
                        title=f"{intent.trade_code}: protective order blocked by risk checks",
                        severity="CRITICAL",
                        what_happened="The approved protective order could not be placed: "
                        f"{decision.plain_summary()}",
                        verified="The position exists; the protective order was not sent.",
                        uncertain="Whether the blocking condition (data, account state, mode) "
                        "clears by itself.",
                        risk_now="The position is NOT protected by an active stop.",
                        action_taken="Protection marked DEGRADED; it is retried each cycle "
                        "while the plan allows; the stop is never widened.",
                        owner_action="Check the failed check on the order page; consider an "
                        "approved exit if the condition persists.",
                    ),
                    now=now,
                    detail={"summary": decision.plain_summary()},
                )
                return Outcome("BLOCKED", decision.plain_summary())
            order_id = await orders.create(
                conn,
                now=now,
                detail={"plan": purpose},
                values={
                    "intent_id": intent.id,
                    "approval_id": approval_id,
                    "purpose": purpose,
                    "account_id": account.id,
                    "instrument_id": inst.id,
                    "client_order_id": client_order_id,
                    "side": "SELL",
                    "order_type": "STOP_LIMIT" if stop is not None else "LIMIT",
                    "quantity": qty,
                    "limit_price": limit,
                    "stop_price": stop,
                    "time_in_force": tif,
                    "expires_at": None,
                    "authority": "PROTECTIVE_MANAGEMENT",
                    "reduce_only": True,
                    "is_simulated": account.kind in SIMULATED_ACCOUNT_KINDS,
                },
            )
            o = await orders.get_order(conn, order_id)
            o = await orders.transition(conn, o, S.VALIDATED, source=Source.LOCAL, now=now)
            await orders.transition(
                conn,
                o,
                S.AUTHORIZED,
                source=Source.LOCAL,
                now=now,
                detail={"authorised_by": "approved management plan", "approval_id": approval_id},
            )
            if purpose == "STOP_LOSS":
                await self._set_protection(conn, intent, "PENDING", now)
            await record(
                conn,
                action="trade.protective_authorized",
                actor=ACTOR,
                occurred_at=now,
                entity_type="order",
                entity_id=order_id,
                payload={"intent_id": intent.id, "purpose": purpose, "quantity": qty},
            )
        outcome = await self._transmit(
            order_id, account, intent_operation=Operation.REDUCE_EXPOSURE
        )
        if purpose == "STOP_LOSS" and outcome.code == "ACKNOWLEDGED":
            async with self.db.transaction() as conn:
                await self._set_protection(conn, intent, "ACTIVE", self.clock.now())
        return outcome

    async def _supersede(
        self, conn: AsyncConnection, order_id: uuid.UUID, by: str, now: datetime
    ) -> OrderRow | None:
        """An older stop of an entry on a venue with one stop-loss per position: the venue's
        stop-loss stays where it is, now held by the new stop order, so nothing is sent."""
        old = await orders.get_order(conn, order_id, lock=True)
        if old.status not in REPLACEABLE:
            return None
        return await orders.transition(
            conn,
            old,
            S.CANCELLED,
            source=Source.RECONCILE,
            now=now,
            detail={"reason": f"superseded by {by}: one stop-loss covers the whole entry"},
        )

    async def _set_protection(
        self, conn: AsyncConnection, intent: Any, state: str, now: datetime
    ) -> None:
        await conn.execute(
            update(positions)
            .where(
                positions.c.account_id == intent.account_id,
                positions.c.instrument_id == intent.instrument_id,
            )
            .values(protection_state=state, updated_at=now)
        )

    async def _protection_failed(
        self, conn: AsyncConnection, o: OrderRow, reason: str, now: datetime
    ) -> None:
        intent = await get_intent(conn, o.intent_id)
        await self._set_protection(conn, intent, "DEGRADED", now)
        await raise_incident(
            conn,
            dedup_key=f"trading.protection_degraded:{o.intent_id}",
            category=Category.TRADING,
            alert=Alert(
                title=f"{intent.trade_code}: protective order failed",
                severity="CRITICAL",
                what_happened=f"Protective order {o.client_order_id} failed: {reason}.",
                verified="The position is NOT protected by an active stop.",
                uncertain="Whether the next retry will be accepted by the venue.",
                risk_now="Losses on this position are not limited by any order.",
                action_taken="Protection marked DEGRADED; a bounded number of identical retries; "
                "the stop level is never widened.",
                owner_action="Watch the order page; if retries are exhausted, decide on an "
                "approved exit or re-proposal with a new stop.",
            ),
            now=now,
            detail={"order": o.client_order_id, "reason": reason},
            notify_kind="protection_degraded",
            context={"trade_code": intent.trade_code},
        )
