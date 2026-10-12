"""Approved position management (V6 §1810, §1822).

Protective stops, take-profit and cancel-unfilled — only the actions in the approved plan.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import record
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema_execution import approvals, fills, post_trade_reviews, trade_intents
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.ops.incidents import Category, Severity, raise_incident
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
)
from cosmic.trading.modes import Operation
from cosmic.trading.orders import OrderRow, Source
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import (
    ManagementPlan,
    get_intent,
    record_risk_run,
)
from cosmic.trading.risk.context import Effect, OrderRequest, Stage
from cosmic.trading.risk.engine import evaluate


class ProtectionManager(ExecutionBase):
    async def _intent_held(self, conn: AsyncConnection, intent_id: uuid.UUID) -> Decimal:
        rows = await conn.execute(
            select(orders_t.c.side, func.coalesce(func.sum(fills.c.quantity), 0).label("q"))
            .join(fills, fills.c.order_id == orders_t.c.id)
            .where(orders_t.c.intent_id == intent_id)
            .group_by(orders_t.c.side)
        )
        held = ZERO
        for r in rows:
            held += Decimal(r.q) if r.side == "BUY" else -Decimal(r.q)
        return held

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
        for intent in intents:
            actions += await self._manage_intent(intent)
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
                notional = delta * plan.stop_loss.limit_price
                if inst.min_notional is not None and notional < inst.min_notional:
                    async with self.db.transaction() as conn:
                        await self._set_protection(conn, intent, "DEGRADED", now)
                        await raise_incident(
                            conn,
                            dedup_key=f"trading.unprotected_remainder:{intent.id}",
                            category=Category.TRADING,
                            severity=Severity.WARNING,
                            title=f"{intent.trade_code}: {delta} unprotected (below venue minimum)",
                            now=now,
                            detail={"delta": delta},
                        )
                    return actions
                await self._place_protective(
                    intent,
                    account,
                    inst,
                    "STOP_LOSS",
                    delta,
                    limit=plan.stop_loss.limit_price,
                    stop=plan.stop_loss.stop_price,
                    tif="GTC",
                    n=len(stops) + 1,
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
    ) -> Outcome:
        now = self.clock.now()
        suffix = "SL" if purpose == "STOP_LOSS" else "TP"
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
                    severity=Severity.CRITICAL,
                    title=f"{intent.trade_code}: protective order blocked by risk checks",
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
                    "client_order_id": f"ct-{intent.trade_code}-{suffix}{n}",
                    "side": "SELL",
                    "order_type": "STOP_LIMIT" if stop is not None else "LIMIT",
                    "quantity": qty,
                    "limit_price": limit,
                    "stop_price": stop,
                    "time_in_force": tif,
                    "expires_at": None,
                    "authority": "PROTECTIVE_MANAGEMENT",
                    "reduce_only": True,
                    "is_simulated": account.kind == "PAPER",
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
            severity=Severity.CRITICAL,
            title=f"{intent.trade_code}: protective order failed",
            now=now,
            detail={"order": o.client_order_id, "reason": reason},
        )
        await notify_owners(
            conn,
            kind="protection_degraded",
            now=now,
            dedup_key=f"protect:{o.id}",
            payload={
                "trade_code": intent.trade_code,
                "reason": reason,
                "verified": "position is NOT protected by an active stop",
                "action": "bounded identical retry; stop level is never widened",
            },
        )
