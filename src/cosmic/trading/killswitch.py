"""Global kill switch and recovery checklist (V6 §1817, §1818).

Activation is deterministic and LLM-independent. It stops new exposure, revokes pending
approvals, cancels unfilled *entry* orders per the emergency policy, keeps protective
orders and positions, and requires reconciliation. Stopping new entries does NOT close
existing positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, PermissionDeniedError
from cosmic.core.ids import uuid7
from cosmic.db.schema import incidents
from cosmic.db.schema_execution import approvals, recovery_checklists, trade_intents
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_trading import accounts
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.notify.outbox import notify_owners
from cosmic.trading.authority import _cap, get_state, system_restrict
from cosmic.trading.modes import PERMISSIVENESS, Mode
from cosmic.trading.policy import EmergencyPolicy, active_policy

RESUMABLE = (Mode.PROTECTION_ONLY, Mode.RESEARCH_ONLY, Mode.PAPER_TRADING)
STOP_MEANING = (
    "Emergency stop blocks all new exposure. It does not close existing positions; "
    "protective orders remain active unless the emergency policy says otherwise."
)


@dataclass(frozen=True)
class Activation:
    already_stopped: bool
    approvals_revoked: int
    intents_blocked: int
    entry_orders_to_cancel: tuple[Any, ...]


async def activate(
    conn: AsyncConnection,
    *,
    actor: Actor,
    reason: str,
    now: datetime,
    principal: Principal | None = None,
) -> Activation:
    """Transaction part of the kill switch. The caller then cancels the listed entry
    orders through the execution service and runs reconciliation."""
    if principal is not None and not principal.has(Capability.ACTIVATE_EMERGENCY_STOP):
        raise PermissionDeniedError("missing capability ACTIVATE_EMERGENCY_STOP")
    reason = reason.strip() or "no reason given"
    current = await get_state(conn, lock="update")
    already = current.mode is Mode.EMERGENCY_STOP
    if not already:
        current = await system_restrict(
            conn, target=Mode.EMERGENCY_STOP, reason=reason, now=now, actor=actor
        )
        open_list = (
            await conn.execute(
                select(recovery_checklists.c.id).where(recovery_checklists.c.status == "OPEN")
            )
        ).first()
        if open_list is None:
            await conn.execute(
                recovery_checklists.insert().values(
                    id=uuid7(),
                    opened_at=now,
                    opened_by=f"{actor.type.value}:{actor.id}",
                    stop_reason=reason[:500],
                    stop_version=current.version,
                    status="OPEN",
                )
            )
    revoked = list(
        await conn.execute(
            update(approvals)
            .where(approvals.c.status == "ACTIVE")
            .values(status="REVOKED", status_reason="emergency stop")
            .returning(approvals.c.id)
        )
    )
    blocked = list(
        await conn.execute(
            update(trade_intents)
            .where(trade_intents.c.status.in_(["PROPOSED", "APPROVED", "REQUIRES_REAPPROVAL"]))
            .values(status="BLOCKED", status_reason="emergency stop", updated_at=now)
            .returning(trade_intents.c.id)
        )
    )
    await conn.execute(
        update(accounts)
        .where(accounts.c.status == "ACTIVE")
        .values(reconciliation_status="REQUIRED", updated_at=now)
    )
    # The stop must work even if the risk policy is missing or corrupt: fall back to the
    # safe default emergency behaviour (cancel unfilled entries; never liquidate).
    try:
        _, policy = await active_policy(conn)
        emergency = policy.emergency
    except ConflictError:
        emergency = EmergencyPolicy()
    entries: tuple[Any, ...] = ()
    if emergency.cancel_unfilled_entries:
        entries = tuple(
            r.id
            for r in await conn.execute(
                select(orders_t.c.id).where(
                    orders_t.c.purpose == "ENTRY",
                    orders_t.c.status.in_(["ACKNOWLEDGED", "PARTIALLY_FILLED"]),
                )
            )
        )
    await record(
        conn,
        action="killswitch.activated",
        actor=actor,
        occurred_at=now,
        entity_type="authority",
        entity_id="1",
        payload={
            "reason": reason,
            "approvals_revoked": len(revoked),
            "intents_blocked": len(blocked),
            "entry_orders_to_cancel": len(entries),
            "already_stopped": already,
            "emergency_policy": emergency.model_dump(),
        },
    )
    await notify_owners(
        conn,
        kind="emergency_stop",
        now=now,
        dedup_key=f"estop:{current.version}",
        payload={
            "reason": reason,
            "meaning": STOP_MEANING,
            "entry_orders_cancelling": len(entries),
            "approvals_revoked": len(revoked),
        },
    )
    return Activation(already, len(revoked), len(blocked), entries)


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    label: str
    met: bool
    detail: str


async def recovery_status(conn: AsyncConnection) -> tuple[Any, list[ChecklistItem]]:
    checklist = (
        await conn.execute(
            select(recovery_checklists).where(recovery_checklists.c.status == "OPEN")
        )
    ).first()
    if checklist is None:
        return None, []
    opened = checklist.opened_at
    acct_rows = list(
        await conn.execute(
            select(
                accounts.c.alias, accounts.c.reconciliation_status, accounts.c.last_reconciled_at
            ).where(accounts.c.status == "ACTIVE")
        )
    )
    not_reconciled = [
        a.alias
        for a in acct_rows
        if a.reconciliation_status != "OK"
        or a.last_reconciled_at is None
        or a.last_reconciled_at < opened
    ]
    unknown = int(
        await conn.scalar(
            select(func.count())
            .select_from(orders_t)
            .where(orders_t.c.status == "UNKNOWN_ORDER_STATE")
        )
        or 0
    )
    open_critical = int(
        await conn.scalar(
            select(func.count())
            .select_from(incidents)
            .where(incidents.c.status == "OPEN", incidents.c.severity == "CRITICAL")
        )
        or 0
    )
    items = [
        ChecklistItem(
            "reconciled",
            "Every active account reconciled successfully after the stop",
            not not_reconciled,
            ", ".join(not_reconciled) or "all accounts reconciled",
        ),
        ChecklistItem(
            "no_unknown_orders",
            "No order is in UNKNOWN_ORDER_STATE",
            unknown == 0,
            f"{unknown} unknown order(s)",
        ),
        ChecklistItem(
            "incidents_acknowledged",
            "Critical incidents acknowledged or resolved",
            open_critical == 0,
            f"{open_critical} open critical incident(s)",
        ),
    ]
    return checklist, items


async def resume(
    conn: AsyncConnection,
    *,
    principal: Principal,
    target: Mode,
    reason: str,
    positions_reviewed: bool,
    now: datetime,
    settings: Settings,
) -> Mode:
    require(principal, Capability.ADMINISTER_SYSTEM, now=now, settings=settings)
    state = await get_state(conn, lock="update")
    if state.mode is not Mode.EMERGENCY_STOP:
        raise ConflictError("the system is not in EMERGENCY_STOP")
    if target not in RESUMABLE:
        raise ConflictError(f"resume only into {', '.join(m.value for m in RESUMABLE)}")
    before = state.mode_before_stop or Mode.RESEARCH_ONLY
    if PERMISSIVENESS[target] > PERMISSIVENESS[before]:
        raise ConflictError(
            f"cannot resume into a mode more permissive than before the stop ({before.value})"
        )
    if PERMISSIVENESS[target] > PERMISSIVENESS[_cap(settings)]:
        raise ConflictError("COSMIC_MAX_AUTHORITY_MODE does not allow that mode")
    if len(reason.strip()) < 10:
        raise ConflictError("a recovery reason of at least 10 characters is required")
    checklist, items = await recovery_status(conn)
    unmet = [i.label for i in items if not i.met]
    if not positions_reviewed:
        unmet.append(
            "Operator confirmation that open positions and protective orders were reviewed"
        )
    if checklist is None or unmet:
        raise ConflictError(
            "recovery checklist incomplete: " + "; ".join(unmet or ["no open checklist"])
        )
    from cosmic.trading.authority import _transition

    await _transition(
        conn,
        current=state,
        target=target,
        actor=principal.actor,
        reason=reason,
        now=now,
        preconditions={
            "recovery_checklist": str(checklist.id),
            "items": [i.key for i in items],
            "positions_reviewed": True,
        },
        mode_before_stop=None,
    )
    await conn.execute(
        update(recovery_checklists)
        .where(recovery_checklists.c.id == checklist.id)
        .values(
            status="COMPLETED",
            completed_at=now,
            completed_by=principal.user_id,
            resume_mode=target.value,
            resume_reason=reason[:500],
            evidence={"items": [i.__dict__ for i in items]},
        )
    )
    await record(
        conn,
        action="killswitch.resumed",
        actor=principal.actor,
        occurred_at=now,
        entity_type="authority",
        entity_id="1",
        payload={"to": target.value, "reason": reason},
    )
    return target
