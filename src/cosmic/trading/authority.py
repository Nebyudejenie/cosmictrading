"""Persisted authority state and audited, rule-checked transitions (TRD-01…04, TRD-46)."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, PermissionDeniedError
from cosmic.core.ids import uuid7
from cosmic.db.schema_trading import accounts, authority_state, authority_transitions
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

# Registered live broker adapters: lock 1 of ADR-034, a reviewed code constant. Empty by
# design in this build: the real-money link is built and kept off until the owner decides, so
# live modes are unreachable. Adding one is its own reviewed commit; nothing at runtime can.
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


async def live_preconditions(
    conn: AsyncConnection, now: datetime | None = None, *, live_venues: Collection[str] | None
) -> list[str]:
    """Unmet requirements for any live mode. Empty list = all met (never in this build).

    Every gate must be PASSED as ``golive.readiness`` evaluates it, not merely recorded: a
    record cannot pass a gate the build blocks, and gate I passes only with the owner's active
    live activation (ADR-034). ``live_venues`` is the server's ``COSMIC_LIVE_VENUES``; a caller
    without settings passes None explicitly, relying on ``enforce_cap``, which at startup
    refuses to keep a live mode while it names no venue. Without ``now`` the database clock
    dates the paper period (gate G)."""
    from cosmic.trading import golive  # golive reads LIVE_ADAPTERS from this module

    unmet: list[str] = []
    if not LIVE_ADAPTERS:
        unmet.append("no live broker adapter is registered (no broker selected and tested)")
    if live_venues is not None and not live_venues:
        unmet.append("COSMIC_LIVE_VENUES names no venue for real money")
    when = now if now is not None else await conn.scalar(select(func.now()))
    if not isinstance(when, datetime):  # fail closed
        raise ConflictError("the database clock is unavailable; live modes stay blocked")
    for gate in await golive.readiness(conn, when):
        if gate.status != "PASSED":
            unmet.append(f"go-live gate {gate.key} has not passed")
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
    live_activation_id: uuid.UUID | None = None,
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
            # The activation a live mode rests on (ADR-034); none outside live modes.
            live_activation_id=live_activation_id if target in LIVE_MODES else None,
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
            unmet.extend(await live_preconditions(conn, now, live_venues=settings.live_venues))
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
    activation_id = None
    if target in LIVE_MODES:
        from cosmic.trading import live  # live restricts modes through this module

        active = await live.active_activations(conn, now)
        if not active:  # gate I has passed, so this cannot happen; fail closed anyway
            raise ConflictError("mode change refused: no live activation is active")
        activation_id = active[0].id
    return await _transition(
        conn,
        current=current,
        target=target,
        actor=principal.actor,
        reason=reason,
        now=now,
        preconditions={"checked": True, "permissive": permissive},
        mode_before_stop=None,
        live_activation_id=activation_id,
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
    """Startup check: a stored mode above the configured cap forces PROTECTION_ONLY (TRD-04),
    and so does a live mode while ``COSMIC_LIVE_VENUES`` names no venue (ADR-034 lock 3: the env
    file only takes effect at a restart, so a live mode never runs without its venue)."""
    current = await get_state(conn, lock="update")
    cap = mode_cap(settings)
    if current.mode is Mode.EMERGENCY_STOP:
        return current
    if PERMISSIVENESS[current.mode] > PERMISSIVENESS[cap]:
        title = f"Stored mode {current.mode.value} exceeds configured cap {cap.value}"
        breach = f"was more permissive than COSMIC_MAX_AUTHORITY_MODE ({cap.value})"
        reason = f"stored mode exceeded COSMIC_MAX_AUTHORITY_MODE={cap.value}"
    elif current.mode in LIVE_MODES and not settings.live_venues:
        title = f"Stored mode {current.mode.value} without a live venue"
        breach = "is a live mode, but COSMIC_LIVE_VENUES names no venue for real money"
        reason = "stored live mode while COSMIC_LIVE_VENUES names no venue"
    else:
        return current
    await raise_incident(
        conn,
        dedup_key="authority.cap_breach",
        category=Category.SECURITY,
        alert=Alert(
            title=title,
            severity="CRITICAL",
            what_happened=f"At startup the stored authority mode ({current.mode.value}) {breach}.",
            verified="The configuration and the stored mode disagree.",
            uncertain="Whether the configuration or the database was changed, and by whom.",
            risk_now="Low: new exposure is blocked; existing positions and protective orders "
            "are unchanged.",
            action_taken="The mode was forced to PROTECTION_ONLY.",
            owner_action="Check the audit log and the configuration; change the mode again only "
            "after you understand why they disagreed.",
        ),
        now=now,
        detail={
            "stored": current.mode.value,
            "cap": cap.value,
            "live_venues": list(settings.live_venues),
        },
    )
    return await _transition(
        conn,
        current=current,
        target=Mode.PROTECTION_ONLY,
        actor=Actor.system("authority-cap"),
        reason=reason,
        now=now,
        preconditions={"cap": cap.value, "live_venues": list(settings.live_venues)},
        mode_before_stop=None,
    )
