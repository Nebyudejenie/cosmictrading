"""Persisted authority state and audited, rule-checked transitions (TRD-01…04, TRD-46)."""

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
from cosmic.db.schema_trading import accounts, authority_state, authority_transitions, golive_gates
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident
from cosmic.trading.modes import (
    LIVE_MODES,
    PERMISSIVENESS,
    Mode,
    account_ref,
    is_more_permissive,
)

GATES = ("A", "B", "C", "D", "E", "F", "G", "H", "I")

# Registered live broker adapters. Empty by design in this build: no broker has been
# selected or tested (B-06/B-07), so live modes are unreachable.
LIVE_ADAPTERS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AuthorityState:
    mode: Mode
    version: int
    mode_before_stop: Mode | None
    changed_at: datetime
    changed_by: str
    reason: str

    @property
    def live_enabled(self) -> bool:
        return self.mode in LIVE_MODES


async def get_state(conn: AsyncConnection, *, lock: str | None = None) -> AuthorityState:
    """Read the single authority row. ``lock='share'`` serialises with mode changes."""
    q = select(authority_state).where(authority_state.c.id == 1)
    if lock == "share":
        q = q.with_for_update(read=True)
    elif lock == "update":
        q = q.with_for_update()
    row = (await conn.execute(q)).first()
    if row is None:
        raise ConflictError("authority state is missing; all trading is blocked (fail closed)")
    return AuthorityState(
        mode=Mode(row.mode),
        version=row.version,
        mode_before_stop=Mode(row.mode_before_stop) if row.mode_before_stop else None,
        changed_at=row.changed_at,
        changed_by=row.changed_by,
        reason=row.reason,
    )


async def live_preconditions(conn: AsyncConnection) -> list[str]:
    """Unmet requirements for any live mode. Empty list = all met (never in this build)."""
    unmet: list[str] = []
    if not LIVE_ADAPTERS:
        unmet.append("no live broker adapter is registered (no broker selected and tested)")
    latest: dict[str, str] = {}
    rows = await conn.execute(
        select(golive_gates.c.gate_key, golive_gates.c.status).order_by(golive_gates.c.recorded_at)
    )
    for r in rows:
        latest[r.gate_key] = r.status
    for gate in GATES:
        if latest.get(gate) != "PASSED":
            unmet.append(f"go-live gate {gate} has not passed")
    live_accounts = await conn.scalar(
        select(func.count())
        .select_from(accounts)
        .where(accounts.c.kind == "LIVE", accounts.c.status == "ACTIVE")
    )
    if not live_accounts:
        unmet.append("no live account is configured")
    return unmet


async def accounts_reconciled(conn: AsyncConnection) -> list[str]:
    rows = await conn.execute(
        select(
            accounts.c.alias,
            accounts.c.kind,
            accounts.c.broker_adapter,
            accounts.c.reconciliation_status,
        ).where(accounts.c.status == "ACTIVE")
    )
    return [
        f"account {account_ref(r.alias, r.kind, r.broker_adapter)} reconciliation is "
        f"{r.reconciliation_status}"
        for r in rows
        if r.reconciliation_status != "OK"
    ]


def mode_cap(settings: Settings) -> Mode:
    try:
        return Mode(settings.max_authority_mode)
    except ValueError:
        return Mode.RESEARCH_ONLY


async def _transition(
    conn: AsyncConnection,
    *,
    current: AuthorityState,
    target: Mode,
    actor: Actor,
    reason: str,
    now: datetime,
    preconditions: dict[str, Any],
    mode_before_stop: Mode | None,
) -> AuthorityState:
    new_version = current.version + 1
    result = await conn.execute(
        update(authority_state)
        .where(authority_state.c.id == 1, authority_state.c.version == current.version)
        .values(
            mode=target.value,
            version=new_version,
            changed_at=now,
            changed_by=f"{actor.type.value}:{actor.id}",
            reason=reason[:500],
            mode_before_stop=mode_before_stop.value if mode_before_stop else None,
        )
    )
    if result.rowcount != 1:
        raise ConflictError("authority state changed concurrently; reload and retry")
    await conn.execute(
        authority_transitions.insert().values(
            id=uuid7(),
            from_mode=current.mode.value,
            to_mode=target.value,
            version=new_version,
            actor_type=actor.type.value,
            actor_id=actor.id,
            reason=reason[:500],
            preconditions=preconditions,
            occurred_at=now,
        )
    )
    await record(
        conn,
        action="authority.mode_changed",
        actor=actor,
        occurred_at=now,
        entity_type="authority",
        entity_id="1",
        payload={
            "from": current.mode.value,
            "to": target.value,
            "version": new_version,
            "reason": reason,
        },
    )
    return await get_state(conn)


async def change_mode(
    conn: AsyncConnection,
    *,
    principal: Principal,
    target: Mode,
    reason: str,
    now: datetime,
    settings: Settings,
) -> AuthorityState:
    """Operator-initiated mode change with all safety rules. Raises with unmet reasons."""
    reason = reason.strip()
    if len(reason) < 5:
        raise ConflictError("a written reason (at least 5 characters) is required")
    current = await get_state(conn, lock="update")
    if target is current.mode:
        raise ConflictError(f"mode is already {target.value}")
    if current.mode is Mode.EMERGENCY_STOP:
        raise ConflictError(
            "leave EMERGENCY_STOP through the recovery checklist, not a mode change"
        )
    if target is Mode.EMERGENCY_STOP:
        raise ConflictError("use the emergency stop control to enter EMERGENCY_STOP")
    permissive = is_more_permissive(target, current.mode)
    if permissive:
        require(principal, Capability.ADMINISTER_SYSTEM, now=now, settings=settings)
    elif not (
        principal.has(Capability.ADMINISTER_SYSTEM)
        or principal.has(Capability.ACTIVATE_EMERGENCY_STOP)
    ):
        raise PermissionDeniedError("missing capability to restrict trading authority")
    unmet: list[str] = []
    if permissive:
        cap = mode_cap(settings)
        if PERMISSIVENESS[target] > PERMISSIVENESS[cap]:
            unmet.append(f"COSMIC_MAX_AUTHORITY_MODE caps authority at {cap.value}")
        if target in LIVE_MODES:
            unmet.extend(await live_preconditions(conn))
        if target is Mode.STRATEGY_AUTHORIZED:
            require(principal, Capability.ENABLE_STRATEGY_AUTHORITY, now=now, settings=settings)
            unmet.append(
                "strategy-level mandates are not implemented in this build (disabled by default)"
            )
        if current.mode is Mode.PROTECTION_ONLY:
            unmet.extend(await accounts_reconciled(conn))
    if unmet:
        await record(
            conn,
            action="authority.change_refused",
            actor=principal.actor,
            occurred_at=now,
            entity_type="authority",
            entity_id="1",
            payload={"from": current.mode.value, "to": target.value, "unmet": unmet},
        )
        raise ConflictError("mode change refused: " + "; ".join(unmet))
    return await _transition(
        conn,
        current=current,
        target=target,
        actor=principal.actor,
        reason=reason,
        now=now,
        preconditions={"checked": True, "permissive": permissive},
        mode_before_stop=None,
    )


async def system_restrict(
    conn: AsyncConnection,
    *,
    target: Mode,
    reason: str,
    now: datetime,
    actor: Actor,
) -> AuthorityState:
    """Deterministic, system-initiated restriction (never more permissive)."""
    current = await get_state(conn, lock="update")
    if PERMISSIVENESS[target] >= PERMISSIVENESS[current.mode]:
        return current
    stop_from = current.mode if target is Mode.EMERGENCY_STOP else current.mode_before_stop
    return await _transition(
        conn,
        current=current,
        target=target,
        actor=actor,
        reason=reason,
        now=now,
        preconditions={"system": True},
        mode_before_stop=stop_from,
    )


async def enforce_cap(
    conn: AsyncConnection, *, settings: Settings, now: datetime
) -> AuthorityState:
    """Startup check: a stored mode above the configured cap forces PROTECTION_ONLY (TRD-04)."""
    current = await get_state(conn, lock="update")
    cap = mode_cap(settings)
    if PERMISSIVENESS[current.mode] <= PERMISSIVENESS[cap] or current.mode is Mode.EMERGENCY_STOP:
        return current
    await raise_incident(
        conn,
        dedup_key="authority.cap_breach",
        category=Category.SECURITY,
        alert=Alert(
            title=f"Stored mode {current.mode.value} exceeds configured cap {cap.value}",
            severity="CRITICAL",
            what_happened=f"At startup the stored authority mode ({current.mode.value}) was more "
            f"permissive than COSMIC_MAX_AUTHORITY_MODE ({cap.value}).",
            verified="The configuration cap and the stored mode disagree.",
            uncertain="Whether the configuration or the database was changed, and by whom.",
            risk_now="Low: new exposure is blocked; existing positions and protective orders "
            "are unchanged.",
            action_taken="The mode was forced to PROTECTION_ONLY.",
            owner_action="Check the audit log and the configuration; change the mode again only "
            "after you understand why they disagreed.",
        ),
        now=now,
        detail={"stored": current.mode.value, "cap": cap.value},
    )
    return await _transition(
        conn,
        current=current,
        target=Mode.PROTECTION_ONLY,
        actor=Actor.system("authority-cap"),
        reason=f"stored mode exceeded COSMIC_MAX_AUTHORITY_MODE={cap.value}",
        now=now,
        preconditions={"cap": cap.value},
        mode_before_stop=None,
    )
