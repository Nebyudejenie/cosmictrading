"""Execution authority (V6 §1809–§1813, doc 08 §6, ADR-012).

The only code path that transmits orders. One leader at a time (Postgres advisory
lock). Every state claim is broker-grounded; ambiguity becomes UNKNOWN_ORDER_STATE and
is reconciled, never resubmitted.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.core.labels import DataLabel
from cosmic.core.logs import get_logger
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema import users
from cosmic.db.schema_execution import approvals, fills, post_trade_reviews, trade_intents
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_trading import positions
from cosmic.identity.capabilities import Capability, ServiceIdentity, capabilities_for_roles
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.ops.incidents import Category, Severity, raise_incident
from cosmic.trading import orders
from cosmic.trading.accounts import Account, get_account, set_reconciliation_status
from cosmic.trading.authority import get_state
from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
    SubmitRequest,
    VenueOrder,
    VenueOrderStatus,
)
from cosmic.trading.brokers.registry import BrokerRegistry
from cosmic.trading.context_builder import build_context
from cosmic.trading.fills import FillAnomalyError, apply_fill
from cosmic.trading.modes import Operation, mode_permits
from cosmic.trading.orders import TERMINAL, OrderRow, OrderStatus, Source
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import (
    ManagementPlan,
    get_intent,
    latest_packet,
    record_risk_run,
    set_intent_status,
)
from cosmic.trading.review import create_review_if_closed
from cosmic.trading.risk.context import AuthorizationState, Effect, OrderRequest, Stage
from cosmic.trading.risk.engine import evaluate

log = get_logger(__name__)
S = OrderStatus
EXECUTION_LOCK_KEY = 7201002
ACTOR = Actor.system(ServiceIdentity.EXECUTION_AUTHORITY.value)
NOT_FOUND_GRACE = timedelta(seconds=60)
ACTIVE_STATES = frozenset(OrderStatus) - TERMINAL


@dataclass(frozen=True)
class Outcome:
    code: str  # SKIPPED | BLOCKED | ACKNOWLEDGED | REJECTED | UNKNOWN | FAILED | RACE_LOST
    detail: str = ""
    order_id: uuid.UUID | None = None


def path_to(current: OrderStatus, target: OrderStatus) -> list[OrderStatus] | None:
    """Legal path from current to target (direct, or via UNKNOWN_ORDER_STATE)."""
    if current == target and current is not S.PARTIALLY_FILLED:
        return []
    if orders.can_transition(current, target):
        return [target]
    if orders.can_transition(current, S.UNKNOWN_ORDER_STATE) and orders.can_transition(
        S.UNKNOWN_ORDER_STATE, target
    ):
        return [S.UNKNOWN_ORDER_STATE, target]
    return None


def target_from_venue(
    current: OrderStatus, venue: VenueOrder, filled: Decimal, quantity: Decimal
) -> OrderStatus:
    """Map broker-confirmed venue state + recorded fills to the internal status."""
    if venue.status is VenueOrderStatus.FILLED:
        return S.FILLED if filled >= quantity else S.PARTIALLY_FILLED
    if venue.status is VenueOrderStatus.CANCELLED:
        return S.CANCELLED
    if venue.status is VenueOrderStatus.EXPIRED:
        return S.EXPIRED
    if venue.status is VenueOrderStatus.REJECTED:
        return S.REJECTED
    if current is S.CANCEL_PENDING:
        return S.CANCEL_PENDING  # still open at the venue: the cancel is not confirmed yet
    return S.PARTIALLY_FILLED if filled > ZERO else S.ACKNOWLEDGED


class ExecutionService:
    def __init__(
        self, db: Database, clock: Clock, settings: Settings, brokers: BrokerRegistry
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.brokers = brokers

    # ---- leadership -------------------------------------------------------------------

    @asynccontextmanager
    async def leadership(self) -> AsyncIterator[bool]:
        """Session-level advisory lock: exactly one execution authority at a time."""
        async with self.db.engine.connect() as conn:
            got = bool(
                await conn.scalar(text(f"SELECT pg_try_advisory_lock({EXECUTION_LOCK_KEY})"))
            )
            try:
                yield got
            finally:
                if got:
                    await conn.scalar(text(f"SELECT pg_advisory_unlock({EXECUTION_LOCK_KEY})"))
                await conn.commit()

    async def run_cycle(self) -> dict[str, int]:
        stats = {"executed": 0, "synced": 0, "managed": 0}
        async with self.leadership() as leader:
            if not leader:
                return stats
            async with self.db.connect() as conn:
                approved = [
                    r.id
                    for r in await conn.execute(
                        select(trade_intents.c.id)
                        .where(trade_intents.c.status == "APPROVED")
                        .order_by(trade_intents.c.updated_at)
                    )
                ]
            for intent_id in approved:
                await self.execute_intent(intent_id)
                stats["executed"] += 1
            stats["synced"] = await self.poll_active()
            stats["managed"] = await self.manage_positions()
        return stats

    # ---- entry execution --------------------------------------------------------------

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
            assert approval is not None
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
                    "is_simulated": account.kind == "PAPER",
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
        return await self._transmit(order_id, account, intent_operation=Operation.OPEN_EXPOSURE)

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
        await notify_owners(
            conn,
            kind="execution_blocked",
            now=now,
            dedup_key=f"blocked:{intent.id}:{now.isoformat()}",
            payload={"trade_code": intent.trade_code, "status": status, "reason": reason},
        )
        return Outcome("BLOCKED", reason)

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
            else:
                await self._protection_failed(conn, o, "venue unavailable", now)
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
                severity=Severity.WARNING,
                title=f"Order {o.client_order_id} is in UNKNOWN_ORDER_STATE",
                now=now,
                detail={"reason": reason, "account": account.alias},
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
            await notify_owners(
                conn,
                kind="order_unknown",
                now=now,
                dedup_key=f"order:{o.id}:UNKNOWN",
                payload={
                    "client_order_id": o.client_order_id,
                    "status": "UNKNOWN",
                    "action": "new exposure blocked; reconciling with the venue",
                },
            )
        return Outcome("UNKNOWN", reason, o.id)

    # ---- synchronisation with the venue ---------------------------------------------------

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
        for oid in ids:
            await self.sync_order(oid)
        return len(ids)

    async def sync_order(self, order_id: uuid.UUID, *, source: Source = Source.BROKER) -> OrderRow:
        async with self.db.connect() as conn:
            o = await orders.get_order(conn, order_id)
            account = await get_account(conn, account_id=o.account_id)
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
        now = self.clock.now()
        async with self.db.transaction() as conn:
            o = await orders.get_order(conn, order_id, lock=True)
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
                return o
            target = target_from_venue(o.status, venue, o.filled_qty, o.quantity)
            if target == o.status and not (target is S.PARTIALLY_FILLED and new_fills):
                await conn.execute(
                    update(orders_t).where(orders_t.c.id == o.id).values(last_broker_sync_at=now)
                )
                return o
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
                severity=Severity.CRITICAL,
                title=f"Venue state {target.value} cannot follow {o.status.value}",
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
        elif o.status is S.REJECTED:
            await self._protection_failed(conn, o, "protective order rejected", now)
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

    # ---- approved position management (V6 §1810, §1822) -------------------------------------

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

    # ---- restart recovery (AT-20) --------------------------------------------------------

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
                    severity=Severity.WARNING,
                    title=f"{affected} order(s) were in flight at restart; reconciled from venue",
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
