"""Entry execution: authorise, persist SUBMITTING, transmit, record the outcome (V6 §1809)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import record
from cosmic.core.errors import ConflictError
from cosmic.core.money import ZERO
from cosmic.db.schema import users
from cosmic.db.schema_execution import approvals
from cosmic.db.schema_execution import orders as orders_t
from cosmic.identity.capabilities import Capability, capabilities_for_roles
from cosmic.market import data as md
from cosmic.notify.alerts import Alert, notify_alert
from cosmic.notify.outbox import notify_owners
from cosmic.ops.incidents import Category, raise_incident
from cosmic.trading import orders
from cosmic.trading.accounts import Account, get_account, set_reconciliation_status
from cosmic.trading.authority import get_state
from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerUnavailableError,
    SubmitRequest,
)
from cosmic.trading.context_builder import build_context
from cosmic.trading.execution.base import ExecutionBase
from cosmic.trading.execution.common import (
    ACTIVE_STATES,
    ACTOR,
    PROTECTIVE,
    Outcome,
    S,
    log,
)
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, Operation, mode_permits
from cosmic.trading.orders import TERMINAL, OrderRow, Source
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import (
    ManagementPlan,
    entry_available,
    get_intent,
    latest_packet,
    record_risk_run,
    set_intent_status,
)
from cosmic.trading.risk.context import AuthorizationState, Effect, OrderRequest, Stage
from cosmic.trading.risk.engine import evaluate


def _stop_limit(plan: Any) -> Decimal | None:
    stop = ManagementPlan.model_validate(plan).stop_loss
    return stop.limit_price if stop is not None else None


class EntryExecution(ExecutionBase):
    async def _operator_can_approve(self, conn: AsyncConnection, user_id: uuid.UUID) -> bool:
        row = (
            await conn.execute(
                select(users.c.roles, users.c.is_active).where(users.c.id == user_id)
            )
        ).first()
        return bool(
            row and row.is_active and Capability.APPROVE_TRADE in capabilities_for_roles(row.roles)
        )

    async def execute_intent(self, intent_id: uuid.UUID) -> Outcome:
        now = self.clock.now()
        # ---- Tx A: consume approval + revalidate + create order (AUTHORIZED) ----
        async with self.db.transaction() as conn:
            authority = await get_state(conn, lock="share")
            intent = await get_intent(conn, intent_id, lock=True)
            if intent.status != "APPROVED":
                return Outcome("SKIPPED", f"intent is {intent.status}")
            approval = (
                await conn.execute(
                    select(approvals)
                    .where(approvals.c.intent_id == intent_id, approvals.c.decision == "APPROVED")
                    .order_by(approvals.c.decided_at.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).first()
            packet = await latest_packet(conn, intent_id)
            account = await get_account(conn, account_id=intent.account_id)
            inst = await md.instrument_by_id(conn, intent.instrument_id)
            try:
                policy_version, policy = await active_policy(conn)
                self.brokers.for_account(account)
            except ConflictError as exc:
                return await self._block(conn, intent, approval, str(exc), "BLOCKED", now)
            request = OrderRequest(
                account_alias=account.alias,
                instrument_key=inst.canonical_key,
                side=intent.side,
                effect=Effect(intent.effect),
                quantity=intent.quantity,
                order_type=intent.order_type,
                limit_price=intent.limit_price,
                time_in_force=intent.time_in_force,
                price_band_low=intent.price_band_low,
                price_band_high=intent.price_band_high,
                max_slippage_bps=intent.max_slippage_bps,
                closes=tuple(str(x) for x in (intent.details or {}).get("closes", [])),
                stop_limit_price=_stop_limit(intent.management_plan),
            )
            authz = AuthorizationState(
                approval_status=None if approval is None else approval.status,
                approval_expires_at=None if approval is None else approval.expires_at,
                intent_expires_at=intent.intent_expires_at,
                packet_hash_matches=approval is not None
                and approval.packet_id == packet.id
                and approval.packet_hash == packet.packet_hash,
                policy_version_matches=approval is not None
                and approval.policy_version == policy_version,
                operator_can_approve=approval is not None
                and await self._operator_can_approve(conn, approval.operator_user_id),
            )
            ctx = await build_context(
                conn,
                account=account,
                inst=inst,
                request=request,
                stage=Stage.PRE_SUBMISSION,
                mode=authority.mode,
                policy_version=policy_version,
                now=now,
                authorization=authz,
            )
            decision = evaluate(ctx, policy)
            await record_risk_run(
                conn,
                ctx=ctx,
                decision=decision,
                account_id=account.id,
                intent_id=intent_id,
                order_id=None,
                now=now,
            )
            if not decision.passed:
                codes = {r.code for r in decision.blocking}
                status = "BLOCKED" if codes & {"R01", "R15"} else "REQUIRES_REAPPROVAL"
                return await self._block(
                    conn, intent, approval, decision.plain_summary(), status, now
                )
            if approval is None:  # unreachable while R15 holds; never rely on that
                return await self._block(
                    conn, intent, approval, "no approval on record", "BLOCKED", now
                )
            exit_plan: list[tuple[uuid.UUID, Decimal]] | None = None
            if intent.side == "SELL":
                exit_plan = await self._exit_allocation(conn, intent)
                if exit_plan is None:
                    return await self._block(
                        conn,
                        intent,
                        approval,
                        "the position changed since approval: the entries this exit closes no "
                        "longer hold the approved quantity",
                        "REQUIRES_REAPPROVAL",
                        now,
                    )
            consumed = (
                await conn.execute(
                    update(approvals)
                    .where(
                        approvals.c.id == approval.id,
                        approvals.c.status == "ACTIVE",
                        approvals.c.expires_at > now,
                    )
                    .values(status="CONSUMED", consumed_at=now)
                    .returning(approvals.c.id)
                )
            ).first()
            if consumed is None:
                return Outcome("RACE_LOST", "approval no longer active")
            if exit_plan is not None:
                await self._authorize_exit(
                    conn, intent, approval, account, exit_plan, policy_version, now
                )
            else:
                order_id = await orders.create(
                    conn,
                    now=now,
                    detail={"approval_id": approval.id},
                    values={
                        "intent_id": intent_id,
                        "approval_id": approval.id,
                        "purpose": "ENTRY",
                        "account_id": account.id,
                        "instrument_id": inst.id,
                        "client_order_id": f"ct-{intent.trade_code}-E",
                        "side": intent.side,
                        "order_type": intent.order_type,
                        "quantity": intent.quantity,
                        "limit_price": intent.limit_price,
                        "stop_price": None,
                        "time_in_force": intent.time_in_force,
                        "expires_at": intent.intent_expires_at
                        if intent.time_in_force == "GTD"
                        else None,
                        "authority": "APPROVED_EXECUTION",
                        "reduce_only": False,
                        "is_simulated": account.kind in SIMULATED_ACCOUNT_KINDS,
                    },
                )
                o = await orders.get_order(conn, order_id)
                o = await orders.transition(conn, o, S.VALIDATED, source=Source.LOCAL, now=now)
                o = await orders.transition(
                    conn,
                    o,
                    S.AUTHORIZED,
                    source=Source.LOCAL,
                    now=now,
                    detail={"policy_version": policy_version},
                )
                await conn.execute(
                    update(approvals)
                    .where(approvals.c.id == approval.id)
                    .values(consumed_order_id=order_id)
                )
                await set_intent_status(conn, intent_id, "EXECUTING", None, now)
                await record(
                    conn,
                    action="trade.order_authorized",
                    actor=ACTOR,
                    occurred_at=now,
                    entity_type="order",
                    entity_id=order_id,
                    payload={
                        "intent_id": intent_id,
                        "client_order_id": o.client_order_id,
                        "approval_id": approval.id,
                    },
                )
        if exit_plan is not None:
            await self.release_exits(intent_id)
            return Outcome("AUTHORIZED", "exit authorised; sent once its protection is replaced")
        return await self._transmit(order_id, account, intent_operation=Operation.OPEN_EXPOSURE)

    async def _exit_allocation(
        self, conn: AsyncConnection, intent: Any
    ) -> list[tuple[uuid.UUID, Decimal]] | None:
        """Split an exit across the entries it closes (by what each still holds).

        A sell without ``closes`` is a plain reduce: its order lives on the exit intent itself.
        Returns None when the entries no longer hold the approved quantity.
        """
        closes = [uuid.UUID(str(x)) for x in (intent.details or {}).get("closes", [])]
        if not closes:
            return [(intent.id, intent.quantity)]
        remaining: Decimal = intent.quantity
        plan: list[tuple[uuid.UUID, Decimal]] = []
        for entry_id in closes:
            if remaining <= ZERO:
                break
            take = min(await entry_available(conn, entry_id), remaining)
            if take > ZERO:
                plan.append((entry_id, take))
                remaining -= take
        return plan if remaining <= ZERO else None

    async def _authorize_exit(
        self,
        conn: AsyncConnection,
        intent: Any,
        approval: Any,
        account: Account,
        plan: list[tuple[uuid.UUID, Decimal]],
        policy_version: int,
        now: datetime,
    ) -> None:
        """One reduce-only EXIT order per closed entry, AUTHORIZED but not yet sent."""
        created: list[uuid.UUID] = []
        for n, (owner_intent, qty) in enumerate(plan, start=1):
            order_id = await orders.create(
                conn,
                now=now,
                detail={"approval_id": approval.id, "exit_intent_id": intent.id},
                values={
                    "intent_id": owner_intent,
                    "approval_id": approval.id,
                    "purpose": "EXIT",
                    "account_id": account.id,
                    "instrument_id": intent.instrument_id,
                    "client_order_id": f"ct-{intent.trade_code}-X{n}",
                    "side": "SELL",
                    "order_type": intent.order_type,
                    "quantity": qty,
                    "limit_price": intent.limit_price,
                    "stop_price": None,
                    "time_in_force": intent.time_in_force,
                    "expires_at": intent.intent_expires_at
                    if intent.time_in_force == "GTD"
                    else None,
                    "authority": "APPROVED_EXECUTION",
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
                detail={"policy_version": policy_version, "exit_intent_id": str(intent.id)},
            )
            created.append(order_id)
        await conn.execute(
            update(approvals)
            .where(approvals.c.id == approval.id)
            .values(consumed_order_id=created[0])
        )
        await set_intent_status(conn, intent.id, "EXECUTING", None, now)
        await record(
            conn,
            action="trade.exit_authorized",
            actor=ACTOR,
            occurred_at=now,
            entity_type="trade_intent",
            entity_id=intent.id,
            payload={
                "orders": [str(x) for x in created],
                "closes": [str(e) for e, _ in plan],
                "approval_id": approval.id,
            },
        )

    async def release_exits(self, exit_intent_id: uuid.UUID | None = None) -> int:
        """Send approved exits once the protective orders they replace are gone.

        Cancel requests go first; an exit is only transmitted when no protective order of its
        entry is still active (venue-confirmed), and only if the entry still holds the exit's
        quantity — a stop that filled in between means the exit is never sent.
        """
        q = select(orders_t.c.id).where(
            orders_t.c.purpose == "EXIT", orders_t.c.status == "AUTHORIZED"
        )
        if exit_intent_id is not None:
            q = q.where(
                orders_t.c.approval_id.in_(
                    select(approvals.c.id).where(approvals.c.intent_id == exit_intent_id)
                )
            )
        async with self.db.connect() as conn:
            ids = [r.id for r in await conn.execute(q.order_by(orders_t.c.created_at))]
        released = 0
        for order_id in ids:
            async with self.db.connect() as conn:
                o = await orders.get_order(conn, order_id)
                exit_intent = await conn.scalar(
                    select(approvals.c.intent_id).where(approvals.c.id == o.approval_id)
                )
                account = await get_account(conn, account_id=o.account_id)
                siblings = await orders.list_orders(conn, intent_id=o.intent_id)
            for p in siblings:
                if p.purpose in PROTECTIVE and p.status in (S.ACKNOWLEDGED, S.PARTIALLY_FILLED):
                    await self.request_cancel(
                        p.id, reason="an approved exit replaces this protective order"
                    )
            async with self.db.connect() as conn:
                siblings = await orders.list_orders(conn, intent_id=o.intent_id)
                if any(p.purpose in PROTECTIVE and p.status in ACTIVE_STATES for p in siblings):
                    continue  # cancellation not confirmed yet: try again next cycle
                held = (
                    await entry_available(conn, o.intent_id, exclude_order_id=o.id)
                    if o.intent_id != exit_intent
                    else o.quantity  # plain reduce: R17 checked the position moments ago
                )
            if held < o.quantity:
                now = self.clock.now()
                async with self.db.transaction() as conn:
                    o = await orders.get_order(conn, order_id, lock=True)
                    if o.status is not S.AUTHORIZED:
                        continue
                    reason = (
                        f"never transmitted: the entry now holds {held}, less than this exit's "
                        f"{o.quantity} (a protective order filled first)"
                    )
                    o = await orders.transition(
                        conn, o, S.REJECTED, source=Source.LOCAL, now=now, detail={"reason": reason}
                    )
                    await self._exit_settled(conn, o, now)
                    await record(
                        conn,
                        action="trade.order_not_transmitted",
                        actor=ACTOR,
                        occurred_at=now,
                        entity_type="order",
                        entity_id=o.id,
                        payload={"reason": reason},
                    )
                continue
            await self._transmit(order_id, account, intent_operation=Operation.REDUCE_EXPOSURE)
            released += 1
        return released

    async def _block(
        self,
        conn: AsyncConnection,
        intent: Any,
        approval: Any,
        reason: str,
        status: str,
        now: datetime,
    ) -> Outcome:
        if approval is not None and approval.status == "ACTIVE":
            await conn.execute(
                update(approvals)
                .where(approvals.c.id == approval.id)
                .values(status="SUPERSEDED", status_reason=reason[:500])
            )
        await set_intent_status(conn, intent.id, status, reason, now)
        await record(
            conn,
            action="trade.execution_blocked",
            actor=ACTOR,
            occurred_at=now,
            entity_type="trade_intent",
            entity_id=intent.id,
            payload={"reason": reason, "status": status},
        )
        await notify_alert(
            conn,
            kind="execution_blocked",
            alert=Alert(
                title=f"{intent.trade_code}: approved trade not executed",
                severity="WARNING",
                what_happened=f"The re-check just before submission blocked the approved trade: "
                f"{reason}",
                verified="No order was sent to the venue.",
                uncertain="Whether the blocking condition will clear.",
                risk_now="None from this trade; no exposure was created.",
                action_taken=f"Proposal marked {status}; the approval is no longer usable.",
                owner_action="Read the reason on the approval page; re-propose if the trade is "
                "still wanted (it needs a new approval).",
            ),
            now=now,
            dedup_key=f"blocked:{intent.id}:{now.isoformat()}",
            context={"trade_code": intent.trade_code, "status": status},
        )
        return Outcome("BLOCKED", reason)

    async def _attached_stop(self, conn: AsyncConnection, o: OrderRow) -> Decimal | None:
        """The approved plan's stop price, for a venue that holds the stop with the entry."""
        intent = await get_intent(conn, o.intent_id)
        plan = ManagementPlan.model_validate(intent.management_plan)
        return plan.stop_loss.stop_price if plan.stop_loss is not None else None

    async def _transmit(
        self, order_id: uuid.UUID, account: Account, *, intent_operation: Operation
    ) -> Outcome:
        now = self.clock.now()
        broker = self.brokers.for_account(account)
        # ---- Tx B: authority re-read under lock, persist SUBMITTING before sending ----
        async with self.db.transaction() as conn:
            authority = await get_state(conn, lock="share")
            o = await orders.get_order(conn, order_id, lock=True)
            if o.status is not S.AUTHORIZED:
                return Outcome("SKIPPED", f"order is {o.status.value}", order_id)
            allowed, why = mode_permits(authority.mode, intent_operation, account_kind=account.kind)
            if not allowed:
                await orders.transition(
                    conn,
                    o,
                    S.REJECTED,
                    source=Source.LOCAL,
                    now=now,
                    detail={"reason": f"never transmitted: {why}"},
                )
                if o.purpose == "ENTRY":
                    await set_intent_status(conn, o.intent_id, "BLOCKED", why, now)
                elif o.purpose == "EXIT":
                    await self._exit_settled(conn, o, now)
                await record(
                    conn,
                    action="trade.order_not_transmitted",
                    actor=ACTOR,
                    occurred_at=now,
                    entity_type="order",
                    entity_id=order_id,
                    payload={"reason": why},
                )
                return Outcome("BLOCKED", why, order_id)
            o = await orders.transition(
                conn, o, S.SUBMITTING, source=Source.LOCAL, now=now, extra={"submitted_at": now}
            )
            attached = (
                await self._attached_stop(conn, o)
                if o.purpose == "ENTRY" and broker.capabilities().attached_stop
                else None
            )
        req = SubmitRequest(
            client_order_id=o.client_order_id,
            instrument_key=await self._key(o),
            side=o.side,
            order_type=o.order_type,
            quantity=o.quantity,
            limit_price=o.limit_price,
            stop_price=o.stop_price,
            time_in_force=o.time_in_force,
            expires_at=o.expires_at,
            attached_stop=attached,
        )
        try:
            result = await broker.submit_order(account.id, req)
        except BrokerUnavailableError as exc:
            return await self._record_unavailable(o, str(exc))
        except BrokerAmbiguousError as exc:
            return await self._record_unknown(o, account, str(exc))
        except Exception as exc:  # unexpected: treat as ambiguous (safe)
            log.error("submit_unexpected_error", order=o.client_order_id, error=type(exc).__name__)
            return await self._record_unknown(o, account, f"unexpected {type(exc).__name__}")
        # ---- Tx C: record the venue response ----
        now = self.clock.now()
        async with self.db.transaction() as conn:
            o = await orders.get_order(conn, order_id, lock=True)
            if o.status is not S.SUBMITTING:
                return Outcome("SKIPPED", f"order moved to {o.status.value} meanwhile", order_id)
            o = await orders.transition(
                conn, o, S.SUBMITTED, source=Source.LOCAL, now=now, detail={"transport": "returned"}
            )
            if result.accepted:
                o = await orders.transition(
                    conn,
                    o,
                    S.ACKNOWLEDGED,
                    source=Source.BROKER,
                    now=now,
                    detail={"venue_order_id": result.venue_order_id, **result.raw},
                    extra={
                        "broker_order_id": result.venue_order_id,
                        "acknowledged_at": now,
                        "last_broker_sync_at": now,
                    },
                )
                code = "ACKNOWLEDGED"
            else:
                o = await orders.transition(
                    conn,
                    o,
                    S.REJECTED,
                    source=Source.BROKER,
                    now=now,
                    detail={"reason": result.reject_reason, **result.raw},
                )
                if o.purpose == "ENTRY":
                    await set_intent_status(
                        conn,
                        o.intent_id,
                        "EXECUTION_FAILED",
                        f"venue rejected: {result.reject_reason}",
                        now,
                    )
                elif o.purpose == "EXIT":
                    await self._exit_settled(conn, o, now)
                else:
                    await self._protection_failed(
                        conn, o, f"venue rejected: {result.reject_reason}", now
                    )
                code = "REJECTED"
            await record(
                conn,
                action=f"trade.order_{code.lower()}",
                actor=ACTOR,
                occurred_at=now,
                entity_type="order",
                entity_id=order_id,
                payload={
                    "client_order_id": o.client_order_id,
                    "venue_order_id": result.venue_order_id,
                    "reason": result.reject_reason,
                    "simulated": o.is_simulated,
                },
            )
            await notify_owners(
                conn,
                kind=f"order_{code.lower()}",
                now=now,
                dedup_key=f"order:{order_id}:{code}",
                payload={
                    "client_order_id": o.client_order_id,
                    "status": o.status.value,
                    "purpose": o.purpose,
                    "simulated": o.is_simulated,
                    "basis": "BROKER_CONFIRMED",
                    **({"reason": str(result.reject_reason)[:200]} if not result.accepted else {}),
                },
            )
        return Outcome(code, result.reject_reason or "", order_id)

    async def _key(self, o: OrderRow) -> str:
        async with self.db.connect() as conn:
            return (await md.instrument_by_id(conn, o.instrument_id)).canonical_key

    async def _record_unavailable(self, o: OrderRow, reason: str) -> Outcome:
        now = self.clock.now()
        async with self.db.transaction() as conn:
            o = await orders.get_order(conn, o.id, lock=True)
            o = await orders.transition(
                conn,
                o,
                S.REJECTED,
                source=Source.LOCAL,
                now=now,
                detail={"reason": f"venue unavailable, not accepted: {reason}"},
            )
            if o.purpose == "ENTRY":
                await set_intent_status(
                    conn, o.intent_id, "EXECUTION_FAILED", "venue unavailable", now
                )
            elif o.purpose == "EXIT":
                await self._exit_settled(conn, o, now)
            else:
                await self._protection_failed(conn, o, "venue unavailable", now)
            if o.purpose in ("ENTRY", "EXIT"):  # an approved trade that never went out
                await notify_owners(
                    conn,
                    kind="order_rejected",
                    now=now,
                    dedup_key=f"order:{o.id}:REJECTED",
                    payload={
                        "client_order_id": o.client_order_id,
                        "status": o.status.value,
                        "purpose": o.purpose,
                        "reason": f"not sent: the venue was unavailable ({reason[:160]})",
                        "simulated": o.is_simulated,
                        "basis": "LOCAL",
                    },
                )
            await record(
                conn,
                action="trade.order_not_accepted",
                actor=ACTOR,
                occurred_at=now,
                entity_type="order",
                entity_id=o.id,
                payload={"reason": reason},
            )
        return Outcome("FAILED", reason, o.id)

    async def _record_unknown(self, o: OrderRow, account: Account, reason: str) -> Outcome:
        now = self.clock.now()
        async with self.db.transaction() as conn:
            o = await orders.get_order(conn, o.id, lock=True)
            if o.status not in TERMINAL and o.status is not S.UNKNOWN_ORDER_STATE:
                o = await orders.transition(
                    conn,
                    o,
                    S.UNKNOWN_ORDER_STATE,
                    source=Source.LOCAL,
                    now=now,
                    detail={"reason": reason},
                )
            await set_reconciliation_status(conn, account_id=account.id, status="REQUIRED", now=now)
            await raise_incident(
                conn,
                dedup_key=f"trading.unknown_order:{o.id}",
                category=Category.TRADING,
                alert=Alert(
                    title=f"Order {o.client_order_id} is in UNKNOWN_ORDER_STATE",
                    severity="WARNING",
                    what_happened=f"The venue's answer for order {o.client_order_id} was lost or "
                    f"ambiguous ({reason}).",
                    verified="The order was persisted as SUBMITTING before it was sent.",
                    uncertain="Whether the venue accepted, filled or rejected it.",
                    risk_now="Exposure may exist that we have not recorded yet.",
                    action_taken="New exposure is blocked; the order is being reconciled by its "
                    "client order ID. It will never be resubmitted automatically.",
                    owner_action="Usually nothing: reconciliation resolves it from venue "
                    "evidence. If it stays UNKNOWN, query the venue and reconcile.",
                ),
                now=now,
                detail={"reason": reason, "account": account.alias},
                notify_kind="order_unknown",
                context={"client_order_id": o.client_order_id, "status": "UNKNOWN"},
            )
            await record(
                conn,
                action="trade.order_unknown",
                actor=ACTOR,
                occurred_at=now,
                entity_type="order",
                entity_id=o.id,
                payload={"reason": reason},
            )
        return Outcome("UNKNOWN", reason, o.id)
