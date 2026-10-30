"""The real-money locks the owner opens (ADR-034): the key check and the live activation.

Nothing here opens a lock by itself:
- ``LIVE_ADAPTERS`` is a reviewed code constant (lock 1, empty in this build);
- ``COSMIC_MAX_AUTHORITY_MODE`` and ``COSMIC_LIVE_VENUES`` live in the server's env file
  (locks 2 and 3);
- the database refuses a real-money buy without an activation (migration 0016), whatever
  this module does.

An activation covers one LIVE account up to a money cap until it expires. It ends on the
owner's revoke, its expiry, the emergency stop, a failed key check or one overdue by more than
26 hours. When the last one ends, a live mode falls back to PAPER_TRADING. Every change takes
the authority row's lock first, so an activation, a revoke, a mode change and the emergency
stop never interleave.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, PermissionDeniedError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable_lenient
from cosmic.db.schema_market import venues
from cosmic.db.schema_trading import (
    accounts,
    live_activation_revocations,
    live_activations,
    live_key_checks,
)
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident
from cosmic.trading.authority import LIVE_ADAPTERS, get_state, mode_cap, system_restrict
from cosmic.trading.modes import LIVE_MODES, PERMISSIVENESS, Mode, account_ref

# What a live key may and may not do (``GET /sapi/v1/account/apiRestrictions``, verified
# 2026-10-30, docs/integrations/binance-spot-api.md §12). Every flag must be present.
KEY_MUST_ALLOW = ("enableReading", "enableSpotAndMarginTrading", "ipRestrict")
KEY_MUST_REFUSE = (
    "enableWithdrawals",
    "enableInternalTransfer",
    "permitsUniversalTransfer",
    "enableMargin",
    "enableFutures",
    "enableVanillaOptions",
    "enablePortfolioMarginTrading",
    "enableFixApiTrade",
)
KEY_WORDS = {
    "enableReading": "reading",
    "enableSpotAndMarginTrading": "spot trading",
    "ipRestrict": "an IP restriction",
    "enableWithdrawals": "withdrawals",
    "enableInternalTransfer": "internal transfers",
    "permitsUniversalTransfer": "universal transfers",
    "enableMargin": "margin",
    "enableFutures": "futures",
    "enableVanillaOptions": "options",
    "enablePortfolioMarginTrading": "portfolio margin",
    "enableFixApiTrade": "FIX trading",
}
KEY_CHECK_MAX_AGE = timedelta(hours=24)  # for an activation to start
KEY_CHECK_LAPSE = timedelta(hours=26)  # an active one ends: the daily check is overdue
ACTIVATION_DEFAULT_CAP = Decimal(100)
ACTIVATION_CAP_LIMIT = Decimal(1000)  # in the account's currency; a reviewed code change
ACTIVATION_MAX_DAYS = 90
FALLBACK_MODE = Mode.PAPER_TRADING  # where a live mode goes when no activation is left


def judge_key(restrictions: Mapping[str, Any]) -> list[str]:
    """What is wrong with a live key's permissions, in plain words. Empty: it passes. A flag
    that is missing or not a boolean fails (fail closed)."""
    failures: list[str] = []
    for flag in KEY_MUST_ALLOW:
        value = restrictions.get(flag)
        if value is not True:
            failures.append(
                f"the key must have {KEY_WORDS[flag]} on"
                + ("" if isinstance(value, bool) else f" ({flag} not reported)")
            )
    for flag in KEY_MUST_REFUSE:
        value = restrictions.get(flag)
        if value is not False:
            failures.append(
                f"the key must have {KEY_WORDS[flag]} off"
                + ("" if isinstance(value, bool) else f" ({flag} not reported)")
            )
    return failures


def key_fingerprint(api_key: str) -> str:
    """How a key is named in records: the first 16 hex characters of its SHA-256."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class KeyCheck:
    id: uuid.UUID
    adapter: str
    key_fingerprint: str
    passed: bool
    restrictions: dict[str, Any]
    failures: list[str]
    checked_at: datetime

    def fresh(self, now: datetime) -> bool:
        return now - self.checked_at <= KEY_CHECK_MAX_AGE


def _key_check(r: Any) -> KeyCheck:
    return KeyCheck(
        r.id, r.adapter, r.key_fingerprint, r.passed, r.restrictions, r.failures, r.checked_at
    )


async def record_key_check(
    conn: AsyncConnection,
    *,
    adapter: str,
    fingerprint: str,
    restrictions: Mapping[str, Any],
    actor: Actor,
    now: datetime,
) -> KeyCheck:
    """Judge a key's permissions as read from the venue and keep the result (append-only).
    Only the known flags are stored. A failure ends every activation on that adapter and
    opens a security incident: a key that can do more than trade spot must not hold money.
    The incident is CRITICAL (R15 then blocks all new exposure until it is reviewed) only if
    real money was active; a failure while setting up is a WARNING, so the rehearsals go on."""
    failures = judge_key(restrictions)
    known = {
        k: restrictions.get(k) if isinstance(restrictions.get(k), bool) else None
        for k in (*KEY_MUST_ALLOW, *KEY_MUST_REFUSE)
    }
    check = KeyCheck(uuid7(), adapter, fingerprint, not failures, known, failures, now)
    await conn.execute(
        live_key_checks.insert().values(
            id=check.id,
            adapter=adapter,
            key_fingerprint=fingerprint,
            passed=check.passed,
            restrictions=known,
            failures=failures,
            checked_at=now,
            actor_type=actor.type.value,
            actor_id=actor.id,
        )
    )
    await record(
        conn,
        action="live.key_checked",
        actor=actor,
        occurred_at=now,
        entity_type="live_key",
        entity_id=fingerprint,
        payload={"adapter": adapter, "passed": check.passed, "failures": failures},
    )
    if failures:
        await get_state(conn, lock="update")
        ended = 0
        for a in await unrevoked_activations(conn, now):
            if a.adapter == adapter:
                ended += await _end(
                    conn,
                    activation_id=a.id,
                    actor=Actor.system("live-key-check"),
                    reason="the live key check failed: " + "; ".join(failures),
                    now=now,
                )
        await enforce(conn, now)
        await raise_incident(
            conn,
            dedup_key=f"live.key_check:{adapter}",
            category=Category.SECURITY,
            alert=Alert(
                title="The real-money key failed its check",
                severity="CRITICAL" if ended else "WARNING",
                what_happened=f"The key {fingerprint} for {adapter} does not have the "
                "permissions real money requires.",
                verified="; ".join(failures),
                uncertain="Whether the key's settings were changed at the venue, and by whom.",
                risk_now="Low: no real-money order can be sent without an activation.",
                action_taken=f"{ended} live activation(s) ended."
                if ended
                else "No live activation was active.",
                owner_action="At the venue, give the key spot trading and reading only, with an "
                "IP restriction; then run the key check again.",
            ),
            now=now,
            detail={"adapter": adapter, "fingerprint": fingerprint, "failures": failures},
        )
    return check


async def latest_key_check(conn: AsyncConnection, adapter: str) -> KeyCheck | None:
    row = (
        await conn.execute(
            select(live_key_checks)
            .where(live_key_checks.c.adapter == adapter)
            .order_by(desc(live_key_checks.c.checked_at))
            .limit(1)
        )
    ).first()
    return None if row is None else _key_check(row)


@dataclass(frozen=True)
class Activation:
    id: uuid.UUID
    account_id: uuid.UUID
    alias: str
    adapter: str
    currency: str
    cap: Decimal
    reason: str
    activated_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None
    revoke_reason: str | None

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.activated_at <= now < self.expires_at

    @property
    def account_ref(self) -> str:
        return account_ref(self.alias, "LIVE", self.adapter)


def _activations() -> Any:
    r = live_activation_revocations
    return (
        select(
            live_activations,
            accounts.c.alias,
            accounts.c.broker_adapter,
            accounts.c.base_currency,
            r.c.revoked_at,
            r.c.revoked_by,
            r.c.reason.label("revoke_reason"),
        )
        .join(accounts, accounts.c.id == live_activations.c.account_id)
        .outerjoin(r, r.c.activation_id == live_activations.c.id)
        .order_by(desc(live_activations.c.activated_at))
    )


def _activation(row: Any) -> Activation:
    return Activation(
        id=row.id,
        account_id=row.account_id,
        alias=row.alias,
        adapter=row.broker_adapter,
        currency=row.base_currency,
        cap=row.cap,
        reason=row.reason,
        activated_at=row.activated_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revoke_reason=row.revoke_reason,
    )


async def activations(conn: AsyncConnection, *, limit: int = 20) -> list[Activation]:
    """The newest activations, active or not."""
    return [_activation(r) for r in await conn.execute(_activations().limit(limit))]


async def active_activations(conn: AsyncConnection, now: datetime) -> list[Activation]:
    rows = await conn.execute(
        _activations().where(
            live_activations.c.activated_at <= now,
            live_activations.c.expires_at > now,
            live_activation_revocations.c.revoked_at.is_(None),  # outer join: never revoked
        )
    )
    return [_activation(r) for r in rows]


async def unrevoked_activations(conn: AsyncConnection, now: datetime) -> list[Activation]:
    """Every activation not yet revoked or expired, including one stamped a little ahead of
    ``now`` by another process's clock: what an ending must end."""
    rows = await conn.execute(
        _activations().where(
            live_activations.c.expires_at > now,
            live_activation_revocations.c.revoked_at.is_(None),
        )
    )
    return [_activation(r) for r in rows]


async def active_activation(
    conn: AsyncConnection, account_id: uuid.UUID, now: datetime
) -> Activation | None:
    for a in await active_activations(conn, now):
        if a.account_id == account_id:
            return a
    return None


async def activation_refusals(
    conn: AsyncConnection,
    *,
    settings: Settings,
    account_id: uuid.UUID,
    cap: Decimal,
    days: int,
    reason: str,
    now: datetime,
    lock: bool = False,
) -> list[str]:
    """Why an activation of this account would be refused now (empty: it would not be). Every
    lock before it must be open: the reviewed code, the server's env file, the gates and a
    passing key check. ``lock`` holds the account row until the transaction ends."""
    from cosmic.trading import golive  # golive reads activations from this module

    unmet: list[str] = []
    q = (
        select(accounts, venues.c.key.label("venue_key"))
        .join(venues, venues.c.id == accounts.c.venue_id)
        .where(accounts.c.id == account_id)
    )
    acct = (await conn.execute(q.with_for_update(of=accounts) if lock else q)).first()
    if acct is None or acct.kind != "LIVE":
        return ["only a LIVE account can be activated"]
    mode = (await get_state(conn)).mode
    if mode in (Mode.EMERGENCY_STOP, Mode.PROTECTION_ONLY):
        unmet.append(f"the system is in {mode.value}: recover first")
    if acct.broker_adapter not in LIVE_ADAPTERS:
        unmet.append(f"the live adapter {acct.broker_adapter} is not registered (lock 1)")
    if PERMISSIVENESS[mode_cap(settings)] < PERMISSIVENESS[Mode.APPROVAL_REQUIRED]:
        unmet.append(
            f"COSMIC_MAX_AUTHORITY_MODE is {mode_cap(settings).value}; real money needs "
            "APPROVAL_REQUIRED (lock 2)"
        )
    if acct.venue_key not in settings.live_venues:
        unmet.append(f"COSMIC_LIVE_VENUES does not name {acct.venue_key} (lock 3)")
    for gate in await golive.readiness(conn, now):
        if gate.key != golive.OWNER_GATE and gate.status != "PASSED":
            unmet.append(f"go-live gate {gate.key} has not passed (lock 4)")
    check = await latest_key_check(conn, acct.broker_adapter)
    if check is None:
        unmet.append("the live key has not been checked (lock 5)")
    elif not check.passed:
        unmet.append("the live key's last check failed: " + "; ".join(check.failures))
    elif not check.fresh(now):
        unmet.append("the live key's last passing check is more than 24 hours old (lock 5)")
    if acct.status != "ACTIVE":
        unmet.append(f"the account is {acct.status}")
    if acct.reconciliation_status != "OK":
        unmet.append(f"the account's reconciliation is {acct.reconciliation_status}, not OK")
    if await active_activation(conn, account_id, now) is not None:
        unmet.append("this account already has an active activation; revoke it first")
    if not 0 < cap <= ACTIVATION_CAP_LIMIT:
        unmet.append(f"the cap must be above 0 and at most {ACTIVATION_CAP_LIMIT}")
    if not 1 <= days <= ACTIVATION_MAX_DAYS:
        unmet.append(f"an activation lasts 1 to {ACTIVATION_MAX_DAYS} days")
    if len(reason.strip()) < 5:
        unmet.append("a written reason (at least 5 characters) is required")
    return unmet


async def activate(
    conn: AsyncConnection,
    *,
    principal: Principal,
    settings: Settings,
    account_id: uuid.UUID,
    cap: Decimal,
    days: int,
    reason: str,
    now: datetime,
) -> Activation:
    """Gate I: the owner, with a fresh code, lets one LIVE account trade real money up to
    ``cap`` for ``days``. Refused unless every lock before it is open."""
    from cosmic.trading import golive

    if principal.channel != "PORTAL":
        raise PermissionDeniedError("real money is activated in the portal only")
    require(principal, Capability.ADMINISTER_SYSTEM, now=now, settings=settings)
    require(principal, Capability.APPROVE_TRADE, now=now, settings=settings)
    await get_state(conn, lock="update")  # an emergency stop waits for this, or this for it
    unmet = await activation_refusals(
        conn,
        settings=settings,
        account_id=account_id,
        cap=cap,
        days=days,
        reason=reason,
        now=now,
        lock=True,
    )
    if unmet:
        await record(
            conn,
            action="live.activation_refused",
            actor=principal.actor,
            occurred_at=now,
            entity_type="account",
            entity_id=str(account_id),
            payload={"unmet": unmet, "cap": cap, "days": days},
        )
        raise ConflictError("live activation refused: " + "; ".join(unmet))
    gates = [
        {"key": g.key, "status": g.status, "reasons": g.reasons}
        for g in await golive.readiness(conn, now)
    ]
    activation_id = uuid7()
    await conn.execute(
        live_activations.insert().values(
            id=activation_id,
            activated_by=principal.user_id,
            readiness_snapshot=to_jsonable_lenient({"gates": gates}),
            activated_at=now,
            account_id=account_id,
            cap=cap,
            expires_at=now + timedelta(days=days),
            reason=reason.strip()[:500],
        )
    )
    await record(
        conn,
        action="live.activated",
        actor=principal.actor,
        occurred_at=now,
        entity_type="live_activation",
        entity_id=str(activation_id),
        payload={"account_id": account_id, "cap": cap, "days": days},
    )
    made = await active_activation(conn, account_id, now)
    assert made is not None and made.id == activation_id
    return made


async def _end(
    conn: AsyncConnection,
    *,
    activation_id: uuid.UUID,
    actor: Actor,
    reason: str,
    now: datetime,
) -> int:
    """Record that an activation ended (append-only; a second time changes nothing). Returns 1
    if this call ended it. The caller holds the authority lock and runs ``enforce`` after."""
    result = await conn.execute(
        insert(live_activation_revocations)
        .values(
            activation_id=activation_id,
            revoked_at=now,
            revoked_by=f"{actor.type.value}:{actor.id}",
            reason=(reason.strip() or "revoked")[:500],
        )
        .on_conflict_do_nothing(index_elements=["activation_id"])
    )
    if result.rowcount != 1:
        return 0
    await record(
        conn,
        action="live.revoked",
        actor=actor,
        occurred_at=now,
        entity_type="live_activation",
        entity_id=str(activation_id),
        payload={"reason": reason},
    )
    return 1


async def revoke(
    conn: AsyncConnection,
    *,
    activation_id: uuid.UUID,
    actor: Actor,
    reason: str,
    now: datetime,
) -> int:
    """End an activation. Returns 1 if this call ended it. A live mode with no activation left
    falls back to PAPER_TRADING."""
    await get_state(conn, lock="update")  # after any mode change in flight, never beside it
    ended = await _end(conn, activation_id=activation_id, actor=actor, reason=reason, now=now)
    await enforce(conn, now)
    return ended


async def revoke_by_owner(
    conn: AsyncConnection,
    *,
    principal: Principal,
    activation_id: uuid.UUID,
    reason: str,
    now: datetime,
) -> int:
    """Ending real money only restricts, so it needs no fresh code (like a restriction)."""
    if not (
        principal.has(Capability.ADMINISTER_SYSTEM)
        or principal.has(Capability.ACTIVATE_EMERGENCY_STOP)
    ):
        raise PermissionDeniedError("missing capability to end real-money trading")
    if len(reason.strip()) < 3:
        raise ValidationFailedError("say why, in a few words")
    return await revoke(
        conn, activation_id=activation_id, actor=principal.actor, reason=reason, now=now
    )


async def revoke_all(conn: AsyncConnection, *, actor: Actor, reason: str, now: datetime) -> int:
    """The emergency stop ends every activation, even one stamped a little ahead by another
    process's clock: recovery never resumes real money by itself."""
    await get_state(conn, lock="update")
    ended = 0
    for a in await unrevoked_activations(conn, now):
        ended += await _end(conn, activation_id=a.id, actor=actor, reason=reason, now=now)
    await enforce(conn, now)
    return ended


async def enforce(conn: AsyncConnection, now: datetime) -> bool:
    """Real money never outlives its locks. An activation whose key check is overdue (none
    passing within 26 hours) ends. A live mode with no activation left falls back to
    PAPER_TRADING. Returns True if anything changed. Run after each ending and by the worker
    (expiry, overdue checks)."""
    changed = False
    active = await active_activations(conn, now)
    if active:
        await get_state(conn, lock="update")
    for a in active:
        check = await latest_key_check(conn, a.adapter)
        if check is None or not check.passed or now - check.checked_at > KEY_CHECK_LAPSE:
            changed |= bool(
                await _end(
                    conn,
                    activation_id=a.id,
                    actor=Actor.system("live-key-check"),
                    reason="no passing key check in the last 26 hours",
                    now=now,
                )
            )
    before = await get_state(conn)
    if before.mode not in LIVE_MODES or await active_activations(conn, now):
        return changed
    after = await system_restrict(
        conn,
        target=FALLBACK_MODE,
        reason="no live activation is active: real money ended",
        now=now,
        actor=Actor.system("live-activation"),
    )
    return changed or after.mode is not before.mode


@dataclass(frozen=True)
class Lock:
    number: int
    name: str
    state: str  # OPEN | LOCKED | EACH TRADE
    detail: str
    who: str


def _state(is_open: bool) -> str:
    return "OPEN" if is_open else "LOCKED"


async def locks(
    conn: AsyncConnection, settings: Settings, now: datetime, *, live_venues: Collection[str]
) -> list[Lock]:
    """Each lock between the system and real money, as it stands now (the go-live page)."""
    from cosmic.trading import golive

    cap = mode_cap(settings)
    gates = await golive.readiness(conn, now)
    others = [g for g in gates if g.key != golive.OWNER_GATE]
    open_gates = [g.key for g in others if g.status == "PASSED"]
    adapters = (await conn.execute(select(live_key_checks.c.adapter).distinct())).scalars()
    checks = [await latest_key_check(conn, adapter) for adapter in sorted(adapters)]
    passing = [c for c in checks if c is not None and c.passed and c.fresh(now)]
    active = await active_activations(conn, now)
    return [
        Lock(
            1,
            "A live adapter in the reviewed code",
            _state(bool(LIVE_ADAPTERS)),
            ", ".join(sorted(LIVE_ADAPTERS)) or "none: this build has no live adapter",
            "a reviewed code change, after your decision",
        ),
        Lock(
            2,
            "The server allows a live mode",
            _state(PERMISSIVENESS[cap] >= PERMISSIVENESS[Mode.APPROVAL_REQUIRED]),
            f"COSMIC_MAX_AUTHORITY_MODE is {cap.value}",
            "you, in the server's env file",
        ),
        Lock(
            3,
            "The server names the venue",
            _state(bool(live_venues)),
            "COSMIC_LIVE_VENUES: " + (", ".join(sorted(live_venues)) or "none"),
            "you, in the server's env file",
        ),
        Lock(
            4,
            "Go-live gates A to H",
            _state(len(open_gates) == len(others)),
            f"{len(open_gates)} of {len(others)} passed"
            + (f" ({', '.join(open_gates)})" if open_gates else ""),
            "recorded evidence",
        ),
        Lock(
            5,
            "A key that can only trade spot, from one address",
            _state(bool(passing)),
            "passed "
            + ", ".join(
                f"{c.key_fingerprint} at {c.checked_at:%Y-%m-%d %H:%M} UTC" for c in passing
            )
            if passing
            else "no passing check in the last 24 hours",
            "you, in the venue's key settings; then the key check",
        ),
        Lock(
            6,
            "Your activation of one account (gate I)",
            _state(bool(active)),
            "; ".join(
                f"{a.account_ref} up to {a.cap:,.2f} {a.currency} until "
                f"{a.expires_at:%Y-%m-%d %H:%M} UTC"
                for a in active
            )
            or "none",
            "you, with a fresh code",
        ),
        Lock(
            7,
            "Each trade approved by you",
            "EACH TRADE",
            "always: real money never trades on a standing approval",
            "you, with a code, per trade",
        ),
    ]
