"""Admin → Broker Connections → HFM MT5 (S83, the owner's directive of 2026-11-11).

The owner adds, tests, saves, edits, disconnects and reconnects HFM MT5 accounts in the portal
and assigns the robots to them; nobody edits the server. Rules kept here, whatever is asked:

- **HFM decides what an account is.** The owner's DEMO/REAL choice is only a claim: the bridge
  signs in and reports the account's mode from HFM's own ``account_info``; a mismatch is refused
  (and the bridge signs back out of it). Nothing is used before that verification.
- **A REAL account is read-only here.** It is held only with its investor (read-only) password,
  on the read-only terminal, whose bridge refuses every trading command; HFM itself refuses
  orders on such a sign-in; its execution switch cannot be turned on (database constraint). Live
  trading stays behind its own, separate authorization (ADR-034), untouched by this module.
- **Passwords:** encrypted at once (``Sealer``, bound to the account's record), never returned
  to the browser, logged or written in a command row; decrypted only when a sign-in command is
  handed to the bridge, in that one answer.
- **Switching never touches positions.** Signing a terminal in to another account, moving a
  robot to another account or disconnecting one is refused while positions or orders are open
  there, unless the owner explicitly leaves them at HFM (with their stop-loss); MT5 never
  closes or moves positions when it changes account.
- **Identity before every order:** an entry on a terminal that Broker Connections manages needs
  that account connected, verified DEMO, its execution switch on and MT5 signed in to exactly it
  (``entry_problem``); bridge 7 checks the account again just before sending.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.audit.service import record as audit
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.redaction import SECRETS
from cosmic.core.sealing import Sealer
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_execution import trading_mandates
from cosmic.db.schema_trading import (
    accounts,
    bridge_commands,
    bridge_state,
    broker_connection_events,
    broker_connections,
    broker_credentials,
    robot_assignments,
)
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.trading.brokers.mt5_bridge import (
    CONNECTION_COMMANDS,
    LOGIN_VERSION,
    SLOTS,
    Slot,
    account_fingerprint,
    bridge_version,
    opened_on,
)

PURPOSE = b"cosmic broker credentials v1"
ROBOTS = {"DEMO_ROBOT": "CosmicTrading Demo Robot", "LIVE_ROBOT": "CosmicTrading Live Robot"}
ROBOT_TYPE = {"DEMO_ROBOT": "DEMO", "LIVE_ROBOT": "REAL"}
PASSWORD_SHAPE = re.compile(r"[\x20-\x7e]{6,64}")
SERVER_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{1,63}")
FRESH = timedelta(seconds=60)  # a terminal's report older than this: the bridge is not running
LOGIN_TTL = timedelta(seconds=90)  # a sign-in the bridge has not taken by then never runs
WAIT_SECONDS = 75.0  # MT5 gives a sign-in up to 60 s
AUTO_LOGIN_AFTER = timedelta(minutes=2)
AUTO_LOGIN_LIMIT = 3  # refused saved passwords before automatic sign-in stops (no lock-out)
TEST_VALID = timedelta(minutes=30)  # "Save" needs a passed test this recent
OPEN_ORDER_STATES = (
    "PENDING_VALIDATION",
    "VALIDATED",
    "AUTHORIZED",
    "SUBMITTING",
    "SUBMITTED",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "UNKNOWN_ORDER_STATE",
)


@dataclass(frozen=True)
class Connection:
    id: uuid.UUID
    nickname: str
    login: int
    server: str
    fingerprint: str
    declared_type: str
    password_kind: str
    slot: str | None
    status: str
    verified_type: str | None
    verified_at: datetime | None
    execution_enabled: bool
    health: str
    health_reason: str | None
    last_sync_at: datetime | None
    login_failures: int
    last_login_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def slot_info(self) -> Slot | None:
        return SLOTS.get(self.slot or "")

    @property
    def usable(self) -> bool:
        return self.status == "CONNECTED" and self.verified_type == self.declared_type


def _row(r: Any) -> Connection:
    return Connection(
        r.id,
        r.nickname,
        int(r.login),
        r.server,
        r.fingerprint,
        r.declared_type,
        r.password_kind,
        r.slot,
        r.status,
        r.verified_type,
        r.verified_at,
        bool(r.execution_enabled),
        r.health,
        r.health_reason,
        r.last_sync_at,
        int(r.login_failures),
        r.last_login_at,
        r.last_error,
        r.created_at,
        r.updated_at,
    )


def _by(actor: Actor) -> str:
    return f"{actor.type.value}:{actor.id}"


def _bound(connection_id: uuid.UUID) -> str:
    """What a sealed password is bound to: its own record, so it opens for nothing else."""
    return f"broker-connection:{connection_id}"


async def get(conn: AsyncConnection, connection_id: uuid.UUID) -> Connection:
    r = (
        await conn.execute(
            select(broker_connections).where(broker_connections.c.id == connection_id)
        )
    ).first()
    if r is None:
        raise NotFoundError("no such broker connection")
    return _row(r)


async def all_connections(conn: AsyncConnection) -> list[Connection]:
    rows = await conn.execute(
        select(broker_connections).order_by(
            broker_connections.c.status == "DISCONNECTED", broker_connections.c.created_at
        )
    )
    return [_row(r) for r in rows]


async def on_slot(conn: AsyncConnection, slot: str) -> Connection | None:
    """The account Broker Connections manages on a terminal (never a disconnected one)."""
    r = (
        await conn.execute(
            select(broker_connections).where(
                broker_connections.c.slot == slot, broker_connections.c.status != "DISCONNECTED"
            )
        )
    ).first()
    return None if r is None else _row(r)


async def event(
    conn: AsyncConnection,
    connection_id: uuid.UUID | None,
    kind: str,
    ok: bool,
    message: str,
    *,
    now: datetime,
    actor: Actor,
    code: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        broker_connection_events.insert().values(
            id=uuid7(),
            connection_id=connection_id,
            at=now,
            kind=kind,
            ok=ok,
            code=code,
            message=message[:500],
            detail=detail,
            actor=_by(actor),
        )
    )


async def events(conn: AsyncConnection, connection_id: uuid.UUID, limit: int = 20) -> list[Any]:
    return list(
        await conn.execute(
            select(broker_connection_events)
            .where(broker_connection_events.c.connection_id == connection_id)
            .order_by(desc(broker_connection_events.c.at), desc(broker_connection_events.c.id))
            .limit(limit)
        )
    )


# ---- what the terminals report -----------------------------------------------------------------


@dataclass(frozen=True)
class Terminal:
    """A terminal's latest report, as Broker Connections reads it."""

    slot: Slot
    received_at: datetime | None
    version: int
    account: dict[str, Any]
    terminal: dict[str, Any]

    def fresh(self, now: datetime) -> bool:
        return self.received_at is not None and now - self.received_at <= FRESH

    @property
    def fingerprint(self) -> str | None:
        fp = self.account.get("fingerprint")
        return str(fp) if fp else None

    @property
    def busy(self) -> int:
        """Positions and orders open on the signed-in account, any symbol (bridge 7)."""
        return int(self.account.get("positions_total") or 0) + int(
            self.account.get("orders_total") or 0
        )


async def terminal(conn: AsyncConnection, slot: str) -> Terminal:
    info = SLOTS.get(slot)
    if info is None:
        raise NotFoundError("no such terminal")
    r = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == slot
            )
        )
    ).first()
    snap = (r.snapshot or {}) if r is not None else {}
    return Terminal(
        info,
        r.received_at if r is not None else None,
        bridge_version(snap),
        dict(snap.get("account") or {}),
        dict(snap.get("terminal") or {}),
    )


async def known_servers(conn: AsyncConnection) -> list[str]:
    """Servers HFM itself named to our terminals, and those of saved accounts (the wizard's list;
    any other may be typed in)."""
    names: set[str] = set()
    for r in await conn.execute(select(bridge_state.c.snapshot)):
        name = ((r.snapshot or {}).get("account") or {}).get("server")
        if name:
            names.add(str(name))
    names |= {r.server for r in await conn.execute(select(broker_connections.c.server))}
    return sorted(names)


# ---- adding, editing and the password ----------------------------------------------------------


def _clean(nickname: str, login: str, server: str, declared_type: str) -> tuple[str, int, str, str]:
    nickname = " ".join(nickname.split())
    if not 1 <= len(nickname) <= 60:
        raise ValidationFailedError("Nickname: 1 to 60 characters, for example 'Cent demo 100'")
    digits = login.strip()
    if not digits.isdigit() or not 1 <= len(digits) <= 15 or int(digits) <= 0:
        raise ValidationFailedError(
            "Login: the MT5 account number, digits only (myHF → Accounts shows it)"
        )
    server = server.strip()
    if not SERVER_SHAPE.fullmatch(server):
        raise ValidationFailedError(
            "Server: the MT5 server name exactly as myHF shows it, for example HFMarketsGlobal-Demo"
        )
    if declared_type not in ("DEMO", "REAL"):
        raise ValidationFailedError("Account type: DEMO or REAL")
    return nickname, int(digits), server, declared_type


def _password(value: str) -> str:
    if not PASSWORD_SHAPE.fullmatch(value):
        raise ValidationFailedError("Password: 6 to 64 printable characters, as HFM issued it")
    return value


async def _seal(
    conn: AsyncConnection,
    settings: Settings,
    connection_id: uuid.UUID,
    kind: str,
    password: str,
    *,
    actor: Actor,
    now: datetime,
) -> None:
    await conn.execute(
        update(broker_credentials)
        .where(
            broker_credentials.c.connection_id == connection_id,
            broker_credentials.c.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by=_by(actor))
    )
    nonce, ciphertext = Sealer(settings, PURPOSE).seal(password, _bound(connection_id))
    await conn.execute(
        broker_credentials.insert().values(
            id=uuid7(),
            connection_id=connection_id,
            kind=kind,
            ciphertext=ciphertext,
            nonce=nonce,
            added_at=now,
            added_by=_by(actor),
        )
    )


async def has_password(conn: AsyncConnection, connection_id: uuid.UUID) -> bool:
    found = await conn.scalar(
        select(broker_credentials.c.id).where(
            broker_credentials.c.connection_id == connection_id,
            broker_credentials.c.revoked_at.is_(None),
        )
    )
    return found is not None


async def free_slots(conn: AsyncConnection, declared_type: str) -> list[Slot]:
    taken = {
        r.slot
        for r in await conn.execute(
            select(broker_connections.c.slot).where(
                broker_connections.c.slot.is_not(None),
                broker_connections.c.status != "DISCONNECTED",
            )
        )
    }
    return [s for s in SLOTS.values() if s.kind == declared_type and s.name not in taken]


async def _signed_in_to(conn: AsyncConnection, fingerprint: str, now: datetime) -> Terminal | None:
    """The terminal whose MT5 is signed in to this very account now, if one reports it."""
    for name in SLOTS:
        t = await terminal(conn, name)
        if t.fresh(now) and t.fingerprint == fingerprint:
            return t
    return None


async def choose_slot(
    conn: AsyncConnection, declared_type: str, fingerprint: str, now: datetime
) -> Slot:
    """The terminal for a new account (S85: the owner never picks one; on 2026-11-11 theirs
    landed on a terminal that is not installed, and could not be tested):
    1. the terminal whose MT5 is already signed in to this very account;
    2. else a free terminal of the account's type that is installed (reporting);
    otherwise a refusal that says what is in the way."""
    free = {s.name: s for s in await free_slots(conn, declared_type)}
    here = await _signed_in_to(conn, fingerprint, now)
    if here is not None:
        reported = here.account.get("trade_mode")
        if here.slot.kind != declared_type:
            raise ValidationFailedError(
                f"MT5 on {here.slot.label} is signed in to this account, and HFM reports it as "
                f"{reported}: choose that account type"
            )
        if here.slot.name not in free:
            raise ConflictError(
                f"MT5 on {here.slot.label} is signed in to this account, but that terminal "
                "already holds another saved account: disconnect that one first"
            )
        return here.slot
    for name, info in free.items():
        if (await terminal(conn, name)).fresh(now):
            return info
    held = [c for c in await all_connections(conn) if c.status != "DISCONNECTED" and c.slot]
    names = ", ".join(
        f"{c.slot_info.label if c.slot_info else c.slot} holds {c.nickname}" for c in held
    )
    raise ConflictError(
        f"No installed terminal is free for a {declared_type} account"
        + (f" ({names})" if names else "")
        + ". Disconnect the account that holds it, or a further terminal must be installed on "
        "the bridge computer first."
    )


async def _relocate(
    conn: AsyncConnection, c: Connection, now: datetime, actor: Actor
) -> Connection | None:
    """An account saved on a terminal that is not signed in to it, while another, free terminal
    is: it is moved there (nothing is sent to MT5; no position is touched). None when there is
    no such terminal."""
    here = await _signed_in_to(conn, c.fingerprint, now)
    if here is None or here.slot.name == c.slot or here.slot.kind != c.declared_type:
        return None
    if here.slot.name not in {s.name for s in await free_slots(conn, c.declared_type)}:
        return None
    await conn.execute(
        update(broker_connections)
        .where(broker_connections.c.id == c.id)
        .values(slot=here.slot.name, updated_at=now, updated_by=_by(actor))
    )
    await event(
        conn,
        c.id,
        "EDITED",
        True,
        f"moved to {here.slot.label}: MT5 there is signed in to this account",
        now=now,
        actor=actor,
    )
    return await get(conn, c.id)


async def create(
    conn: AsyncConnection,
    settings: Settings,
    *,
    nickname: str,
    login: str,
    server: str,
    declared_type: str,
    password: str,
    actor: Actor,
    now: datetime,
    slot: str | None = None,
) -> uuid.UUID:
    """Add an account (status NEW: not usable until HFM has verified it and the owner saved
    it). DEMO: its trading password, on a demo terminal. REAL: its investor (read-only) password
    only, on the read-only terminal. The terminal is chosen here, never by the owner (S85,
    ``choose_slot``); ``slot`` names one only for a terminal being set up."""
    nickname, number, server, declared_type = _clean(nickname, login, server, declared_type)
    password = _password(password)
    if slot:
        info = SLOTS.get(slot)
        if info is None or info.kind != declared_type:
            raise ValidationFailedError(
                f"Terminal: a {declared_type} account goes on a {declared_type.lower()} terminal"
            )
        if info.name not in {s.name for s in await free_slots(conn, declared_type)}:
            raise ConflictError(
                f"{info.label} already holds another account: disconnect that one first"
            )
    else:
        info = await choose_slot(conn, declared_type, account_fingerprint(number, server), now)
    connection_id = uuid7()
    try:
        async with conn.begin_nested():
            await conn.execute(
                broker_connections.insert().values(
                    id=connection_id,
                    nickname=nickname,
                    broker="HFM",
                    platform="MT5",
                    login=number,
                    server=server,
                    fingerprint=account_fingerprint(number, server),
                    declared_type=declared_type,
                    password_kind="MASTER" if declared_type == "DEMO" else "INVESTOR",
                    slot=info.name,
                    status="NEW",
                    created_at=now,
                    created_by=_by(actor),
                    updated_at=now,
                    updated_by=_by(actor),
                )
            )
    except IntegrityError:
        raise ConflictError(
            "this MT5 account (login and server) is already in Broker Connections: open it there"
        ) from None
    kind = "MASTER" if declared_type == "DEMO" else "INVESTOR"
    await _seal(conn, settings, connection_id, kind, password, actor=actor, now=now)
    message = f"added {nickname} ({declared_type} as declared; not verified yet) on {info.label}"
    await event(conn, connection_id, "ADDED", True, message, now=now, actor=actor)
    await audit(
        conn,
        action="broker.connection_added",
        actor=actor,
        occurred_at=now,
        entity_type="broker_connection",
        entity_id=connection_id,
        payload={
            "nickname": nickname,
            "server": server,
            "declared_type": declared_type,
            "terminal": info.name,
        },
    )
    return connection_id


async def edit(
    conn: AsyncConnection,
    settings: Settings,
    connection_id: uuid.UUID,
    *,
    nickname: str,
    login: str,
    server: str,
    password: str,
    actor: Actor,
    now: datetime,
) -> None:
    """Rename it, or correct its login or server: a corrected identity needs the password again
    and a new verification (until then the account is not usable)."""
    c = await get(conn, connection_id)
    nickname, number, server, _ = _clean(nickname, login, server, c.declared_type)
    changed = number != c.login or server != c.server
    if changed and await _robot_running_on(conn, c):
        raise ConflictError("a robot is running on this account: stop it before changing it")
    values: dict[str, Any] = {"nickname": nickname, "updated_at": now, "updated_by": _by(actor)}
    if changed:
        if not password:
            raise ValidationFailedError("a new login or server needs the password again")
        values |= {
            "login": number,
            "server": server,
            "fingerprint": account_fingerprint(number, server),
            "status": "NEW" if c.status != "DISCONNECTED" else "DISCONNECTED",
            "verified_type": None,
            "verified_at": None,
            "execution_enabled": False,
            "login_failures": 0,
        }
    try:
        async with conn.begin_nested():
            await conn.execute(
                update(broker_connections)
                .where(broker_connections.c.id == connection_id)
                .values(**values)
            )
    except IntegrityError:
        raise ConflictError("another saved account already has this login and server") from None
    if password:
        await _seal(
            conn,
            settings,
            connection_id,
            c.password_kind,
            _password(password),
            actor=actor,
            now=now,
        )
    what = "login or server corrected; verify it again" if changed else "renamed"
    if password and not changed:
        what = "password replaced"
    await event(conn, connection_id, "EDITED", True, what, now=now, actor=actor)
    await audit(
        conn,
        action="broker.connection_edited",
        actor=actor,
        occurred_at=now,
        entity_type="broker_connection",
        entity_id=connection_id,
        payload={"nickname": nickname, "identity_changed": changed, "password": bool(password)},
    )


async def revoke_password(
    conn: AsyncConnection, connection_id: uuid.UUID, *, actor: Actor, now: datetime
) -> bool:
    """The saved password is never used again (the terminal stays signed in until it signs out;
    automatic sign-in after a restart then needs a new password)."""
    done = await conn.execute(
        update(broker_credentials)
        .where(
            broker_credentials.c.connection_id == connection_id,
            broker_credentials.c.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by=_by(actor))
    )
    if done.rowcount:
        await event(
            conn, connection_id, "PASSWORD", True, "saved password revoked", now=now, actor=actor
        )
        await audit(
            conn,
            action="broker.password_revoked",
            actor=actor,
            occurred_at=now,
            entity_type="broker_connection",
            entity_id=connection_id,
            payload={},
        )
    return bool(done.rowcount)


async def password_for(
    conn: AsyncConnection, settings: Settings, connection_id: uuid.UUID
) -> str | None:
    """The saved password, decrypted: only to hand one sign-in command to the bridge."""
    r = (
        await conn.execute(
            select(broker_credentials.c.nonce, broker_credentials.c.ciphertext).where(
                broker_credentials.c.connection_id == connection_id,
                broker_credentials.c.revoked_at.is_(None),
            )
        )
    ).first()
    if r is None:
        return None
    password = Sealer(settings, PURPOSE).open(r.nonce, r.ciphertext, _bound(connection_id))
    if password is not None:
        SECRETS.register([password])  # masked in any log line, should one ever carry it
    return password


# ---- testing: signing the terminal in and reading the account from HFM --------------------------


async def _robot_running_on(conn: AsyncConnection, c: Connection) -> bool:
    """An active mandate on the HFM account this connection's terminal trades."""
    info = c.slot_info
    if info is None or info.adapter is None:
        return False
    found = await conn.scalar(
        select(trading_mandates.c.id)
        .join(accounts, accounts.c.id == trading_mandates.c.account_id)
        .where(accounts.c.broker_adapter == info.adapter, trading_mandates.c.status == "ACTIVE")
    )
    return found is not None


async def _system_open(conn: AsyncConnection, adapter: str | None) -> int:
    """The system's own orders still open on the HFM account of ``adapter``."""
    if adapter is None:
        return 0
    n = await conn.scalar(
        select(func.count())
        .select_from(orders_t)
        .join(accounts, accounts.c.id == orders_t.c.account_id)
        .where(accounts.c.broker_adapter == adapter, orders_t.c.status.in_(OPEN_ORDER_STATES))
    )
    return int(n or 0)


async def switch_problem(conn: AsyncConnection, c: Connection, now: datetime) -> str | None:
    """Why signing this account's terminal in to it would be unsafe now (None: safe). Another
    account signed in there with anything open, or a robot running there, blocks it."""
    if c.slot is None:
        return "this account has no terminal"
    t = await terminal(conn, c.slot)
    if not t.fresh(now):
        return None  # nothing is signed in that could be disturbed; the test says the rest
    if t.fingerprint == c.fingerprint or t.fingerprint is None:
        return None
    if await _robot_running_on(conn, c):
        return (
            f"a robot is running on the account {c.slot_info.label if c.slot_info else c.slot} "
            "holds now: stop it first"
        )
    if t.busy:
        return (
            f"{c.slot_info.label if c.slot_info else c.slot} is signed in to another account with "
            f"{t.busy} open position(s) or order(s)"
        )
    return None


async def start_test(
    conn: AsyncConnection,
    c: Connection,
    *,
    actor: Actor,
    now: datetime,
    leave_open: bool = False,
) -> uuid.UUID | None:
    """Queue the sign-in (or, without a saved password, the identity check) on the account's
    terminal; returns the command. Refused, with nothing sent, when it would be unsafe. A demo
    account MT5 is already signed in to, behind a bridge older than 7, is verified at once from
    the bridge's own report (its fingerprint and HFM's account mode): no command, no password;
    returns None."""
    if c.slot is None or c.slot_info is None:
        raise ConflictError("this account has no terminal")
    if c.status == "DISCONNECTED":
        raise ConflictError("reconnect this account first")
    t = await terminal(conn, c.slot)
    if not (t.fresh(now) and t.fingerprint == c.fingerprint):
        # Saved on another terminal than the one signed in to it (the owner's first try,
        # 2026-11-11): move it there and verify, rather than leave them a dead end.
        moved = await _relocate(conn, c, now, actor)
        if moved is not None and moved.slot is not None and moved.slot_info is not None:
            c, t = moved, await terminal(conn, moved.slot)
    assert c.slot is not None and c.slot_info is not None
    if t.received_at is None:
        raise ConflictError(
            f"{c.slot_info.label} has never reported: its MT5 and bridge are not set up on the "
            "bridge computer yet"
        )
    old = t.version < LOGIN_VERSION
    if old and c.declared_type == "DEMO" and t.fresh(now) and t.fingerprint == c.fingerprint:
        report = {
            "ok": True,
            "fingerprint": t.fingerprint,
            "note": f"read from the bridge's own report (bridge version {t.version})",
            **{
                k: t.account.get(k)
                for k in (
                    "trade_mode",
                    "server",
                    "company",
                    "currency",
                    "leverage",
                    "balance",
                    "equity",
                    "margin_free",
                )
            },
        }
        await record_result(
            conn, _Report("VERIFY", {"connection_id": str(c.id)}), "DONE", report, None, now=now
        )
        return None
    if old:
        raise ConflictError(
            f"the bridge on {c.slot_info.label} is version {t.version}; Broker Connections needs "
            f"version {LOGIN_VERSION} (the bridge update is part of this release)"
        )
    problem = await switch_problem(conn, c, now)
    if problem is not None and not leave_open:
        await event(
            conn,
            c.id,
            "REFUSED",
            False,
            f"test not sent: {problem}",
            now=now,
            actor=actor,
            code="UNSAFE_SWITCH",
        )
        raise ConflictError(
            f"Nothing was sent: {problem}. Close them in MT5, or tick 'leave them at HFM' to "
            "keep them there with their stop-loss (they are never closed or moved)."
        )
    if problem is not None and await _robot_running_on(conn, c):
        raise ConflictError(f"Nothing was sent: {problem}")
    # Already signed in to it: only who is signed in is checked; the password is never sent
    # when it is not needed. Otherwise a sign-in with the saved password.
    here = t.fresh(now) and t.fingerprint == c.fingerprint
    password = not here and await has_password(conn, c.id)
    payload: dict[str, Any] = {
        "connection_id": str(c.id),
        "login": c.login,
        "server": c.server,
        "expected_fingerprint": c.fingerprint,
        "expected_mode": c.declared_type,
        "allow_open": bool(leave_open),
    }
    command_id = uuid7()
    await conn.execute(
        bridge_commands.insert().values(
            id=command_id,
            bridge=c.slot,
            kind="LOGIN" if password else "VERIFY",
            payload=payload,  # never the password: it is added only when handed out
            status="PENDING",
            created_at=now,
            expires_at=now + LOGIN_TTL,
        )
    )
    await conn.execute(
        update(broker_connections).where(broker_connections.c.id == c.id).values(last_login_at=now)
    )
    return command_id


@dataclass(frozen=True)
class _Report:
    """A verification made from the bridge's report, recorded like a command's result."""

    kind: str
    payload: dict[str, Any]


async def wait(db: Database, command_id: uuid.UUID | None, seconds: float = WAIT_SECONDS) -> str:
    """The command's final status, or "WAITING" when the bridge has not answered in time (no
    command: verified at once from the bridge's report, "DONE")."""
    if command_id is None:
        return "DONE"
    deadline = time.monotonic() + seconds
    while True:
        async with db.connect() as conn:
            status = await conn.scalar(
                select(bridge_commands.c.status).where(bridge_commands.c.id == command_id)
            )
        if status in ("DONE", "FAILED", "EXPIRED"):
            return str(status)
        if time.monotonic() > deadline:
            return "WAITING"
        await asyncio.sleep(0.05 if seconds < 15 else 0.5)


def advice(code: str | None, message: str, label: str) -> str:
    """HFM's or the bridge's answer, in words the owner can act on."""
    c = (code or "").strip()
    if c == "-6":
        return (
            "HFM refused the sign-in: the login number, the password or the server is wrong. "
            "Check all three in myHF (Accounts → your MT5 account) and enter them again; for a "
            "REAL account use its investor (read-only) password."
        )
    if c in ("-10000", "-10001", "-10002", "-10003", "-10004", "-10005"):
        return (
            f"MT5 on {label} did not respond (closed or still starting). The bridge computer "
            "restarts it within a few minutes; test again then."
        )
    if c == "-2":
        return (
            "MT5 refused the request as invalid: check the login is only digits and the server "
            "name is complete, for example HFMarketsGlobal-Demo."
        )
    if c == "OPEN_ON_CURRENT":
        return (
            f"{label} is signed in to another account with open positions or orders; nothing "
            "was changed. Close them in MT5, or tick 'leave them at HFM', then test again."
        )
    if c == "WRONG_TYPE":
        return f"{message}: not connected (the account type comes from HFM, not the form)."
    if c == "OTHER_ACCOUNT":
        return f"MT5 on {label} is signed in to a different account than this one."
    if c == "NOT_SIGNED_IN":
        return f"MT5 on {label} is not signed in, and no password is saved: enter it again."
    if c == "NO_PASSWORD":
        return "No password is saved for this account: enter it again."
    if c == "TRADING_PASSWORD":
        return (
            "This REAL account was signed in with its trading (master) password. The read-only "
            "terminal still refuses every order, but replace it with the investor (read-only) "
            "password from myHF."
        )
    return f"MT5 answered: {message or 'no detail'}" + (f" (code {c})" if c else "")


async def record_result(
    conn: AsyncConnection,
    command: Any,
    status: str,
    result: dict[str, Any],
    error: str | None,
    *,
    now: datetime,
) -> None:
    """A sign-in or identity check came back from the bridge: what HFM said, kept and judged."""
    p = command.payload or {}
    try:
        c = await get(conn, uuid.UUID(str(p.get("connection_id"))))
    except (NotFoundError, ValueError):
        return
    label = c.slot_info.label if c.slot_info else str(c.slot)
    actor = Actor.system("broker-connections")
    ok = status == "DONE" and bool(result.get("ok"))
    code = None if ok else str(result.get("code") or ("BRIDGE" if error else "FAILED"))
    reported = result.get("trade_mode")
    if ok and reported != c.declared_type:  # a bridge before 7 did not check it itself
        ok, code = False, "WRONG_TYPE"
        result = {**result, "message": f"HFM reports a {reported} account, not {c.declared_type}"}
    if ok and c.declared_type == "REAL" and result.get("trade_allowed"):
        ok, code = False, "TRADING_PASSWORD"
    if ok and result.get("fingerprint") != c.fingerprint:
        ok, code = False, "OTHER_ACCOUNT"
    restored = result.get("restored")
    if ok:
        message = (
            f"verified by HFM: {reported} account at {result.get('company')}, server "
            f"{result.get('server')}, {result.get('currency')}, leverage 1:{result.get('leverage')}"
            f"; {result.get('note') or 'signed in'}"
        )
    else:
        message = advice(code, str(result.get("message") or error or ""), label)
        if restored is True:
            message += f" {label} was signed back in to the account it had."
        elif restored is False:
            message += f" {label} could not be signed back in to the account it had."
    detail: dict[str, Any] = {
        k: result.get(k)
        for k in (
            "trade_mode",
            "trade_allowed",
            "server",
            "company",
            "currency",
            "leverage",
            "balance",
            "equity",
            "margin_free",
            "margin_level",
            "restored",
            "note",
        )
        if k in result
    }
    values: dict[str, Any] = {"updated_at": now, "updated_by": _by(actor)}
    if ok:
        values |= {
            "verified_type": reported,
            "verified_at": now,
            "login_failures": 0,
            "last_error": None,
            "health": "OK",
            "health_reason": None,
            "last_sync_at": now,
        }
        if c.status == "FAILED":
            values["status"] = "CONNECTED" if c.verified_at is not None else "NEW"
        server = str(result.get("server") or "")
        if server and server != c.server and server.lower() == c.server.lower():
            values |= {"server": server, "fingerprint": account_fingerprint(c.login, server)}
    else:
        values |= {"last_error": message[:500], "execution_enabled": False}
        if code == "-6":
            values["login_failures"] = c.login_failures + 1
        if code in ("-6", "WRONG_TYPE", "TRADING_PASSWORD", "OTHER_ACCOUNT", "NOT_SIGNED_IN"):
            values |= {
                "status": "FAILED" if c.status != "DISCONNECTED" else c.status,
                "verified_type": None,
                "verified_at": None,
            }
            if code == "TRADING_PASSWORD":  # verified REAL, but not usable this way
                values.pop("verified_type")
                values.pop("verified_at")
    await conn.execute(
        update(broker_connections).where(broker_connections.c.id == c.id).values(**values)
    )
    kind = "SIGN_IN" if command.kind == "LOGIN" and p.get("automatic") else "TEST"
    await event(conn, c.id, kind, ok, message, now=now, actor=actor, code=code, detail=detail)
    refused = not ok and code == "-6"
    if refused and (p.get("automatic") or c.login_failures + 1 >= AUTO_LOGIN_LIMIT):
        await raise_incident(
            conn,
            dedup_key=f"broker.password_refused:{c.id}",
            category=Category.TRADING,
            alert=Alert(
                title=f"HFM refused the saved password of {c.nickname}",
                severity="WARNING",
                what_happened=f"HFM refused the saved password ({c.login_failures + 1} time(s))"
                + (" when MT5 restarted signed out." if p.get("automatic") else "."),
                verified="MT5's own answer, through the bridge.",
                uncertain="Whether the password was changed in myHF.",
                risk_now="No new sign-in is tried automatically (so the account is never locked).",
                action_taken="Automatic sign-in stopped for this account.",
                owner_action="Admin → Broker Connections: enter the account's password again.",
            ),
            now=now,
        )
    elif ok:
        await resolve_by_key(
            conn, dedup_key=f"broker.password_refused:{c.id}", now=now, resolution="signed in"
        )


async def save(
    conn: AsyncConnection, connection_id: uuid.UUID, *, actor: Actor, now: datetime
) -> None:
    """The wizard's last step: a verified account becomes usable (CONNECTED)."""
    c = await get(conn, connection_id)
    if c.verified_type != c.declared_type or c.verified_at is None:
        raise ConflictError("test the connection first: HFM must verify the account")
    if now - c.verified_at > TEST_VALID:
        raise ConflictError("the last passed test is over 30 minutes old: test again first")
    await conn.execute(
        update(broker_connections)
        .where(broker_connections.c.id == c.id)
        .values(status="CONNECTED", updated_at=now, updated_by=_by(actor))
    )
    await event(conn, c.id, "SAVED", True, "connection saved", now=now, actor=actor)
    await audit(
        conn,
        action="broker.connection_saved",
        actor=actor,
        occurred_at=now,
        entity_type="broker_connection",
        entity_id=c.id,
        payload={"verified_type": c.verified_type, "terminal": c.slot},
    )


async def _robot_of(conn: AsyncConnection, connection_id: uuid.UUID) -> list[str]:
    rows = await conn.execute(
        select(robot_assignments.c.robot).where(robot_assignments.c.connection_id == connection_id)
    )
    return [r.robot for r in rows]


async def disconnect(
    conn: AsyncConnection,
    connection_id: uuid.UUID,
    *,
    actor: Actor,
    now: datetime,
    leave_open: bool = False,
) -> None:
    """The system stops using the account: no orders, no robot, its terminal freed for another.
    Nothing at HFM is changed: open positions stay there with their stop-loss."""
    c = await get(conn, connection_id)
    if c.status == "DISCONNECTED":
        return
    if await _robot_running_on(conn, c):
        raise ConflictError("a robot is running on this account: stop it first (PAUSE or OFF)")
    open_here = await _system_open(conn, c.slot_info.adapter if c.slot_info else None)
    t = await terminal(conn, c.slot) if c.slot else None
    busy = t.busy if t is not None and t.fingerprint == c.fingerprint else 0
    if (open_here or busy) and not leave_open:
        await event(
            conn,
            c.id,
            "REFUSED",
            False,
            "disconnect refused: open positions or orders",
            now=now,
            actor=actor,
            code="OPEN_POSITIONS",
        )
        raise ConflictError(
            f"This account has {max(open_here, busy)} open position(s) or order(s). Close them, "
            "or tick 'leave them at HFM' to keep them there with their stop-loss (never closed "
            "or moved)."
        )
    await conn.execute(
        update(broker_connections)
        .where(broker_connections.c.id == c.id)
        .values(
            status="DISCONNECTED",
            execution_enabled=False,
            health="UNKNOWN",
            updated_at=now,
            updated_by=_by(actor),
        )
    )
    await conn.execute(
        update(robot_assignments)
        .where(robot_assignments.c.connection_id == c.id)
        .values(connection_id=None, assigned_at=now, assigned_by=_by(actor))
    )
    note = " (open positions left at HFM with their stop-loss)" if (open_here or busy) else ""
    await event(conn, c.id, "DISCONNECTED", True, f"disconnected{note}", now=now, actor=actor)
    await resolve_by_key(
        conn, dedup_key=f"broker.connection:{c.id}", now=now, resolution="disconnected by the owner"
    )
    await audit(
        conn,
        action="broker.connection_disconnected",
        actor=actor,
        occurred_at=now,
        entity_type="broker_connection",
        entity_id=c.id,
        payload={"left_open": bool(open_here or busy)},
    )


async def reconnect(
    conn: AsyncConnection, connection_id: uuid.UUID, *, actor: Actor, now: datetime
) -> None:
    """A disconnected account takes its terminal back (if free); it is usable again once a new
    test has passed (status FAILED until then, so nothing uses it unverified)."""
    c = await get(conn, connection_id)
    if c.status != "DISCONNECTED":
        return
    if c.slot not in {s.name for s in await free_slots(conn, c.declared_type)}:
        raise ConflictError("its terminal now holds another account: disconnect that one first")
    try:
        async with conn.begin_nested():
            await conn.execute(
                update(broker_connections)
                .where(broker_connections.c.id == c.id)
                .values(
                    status="FAILED",
                    last_error="reconnected: test it to use it again",
                    updated_at=now,
                    updated_by=_by(actor),
                )
            )
    except IntegrityError:
        raise ConflictError(
            "this MT5 account was added again since: use that one, or disconnect it first"
        ) from None
    await event(
        conn, c.id, "RECONNECTED", True, "reconnected; test it to use it", now=now, actor=actor
    )


# ---- robots, execution switches ----------------------------------------------------------------


async def assignments(conn: AsyncConnection) -> dict[str, uuid.UUID | None]:
    rows = await conn.execute(select(robot_assignments))
    out: dict[str, uuid.UUID | None] = dict.fromkeys(ROBOTS)
    out.update({r.robot: r.connection_id for r in rows})
    return out


async def assign(
    conn: AsyncConnection,
    robot: str,
    connection_id: uuid.UUID | None,
    *,
    actor: Actor,
    now: datetime,
    leave_open: bool = False,
) -> None:
    """Point a robot at an account. The Demo Robot only at an account HFM verified as DEMO, the
    Live Robot only at one HFM verified as REAL (it stays read-only: live trading has its own
    authorization). Moving a robot is refused while it runs, or while it has orders or
    positions open where it is, unless the owner leaves them at HFM (never closed or moved)."""
    if robot not in ROBOTS:
        raise NotFoundError("no such robot")
    current = (await assignments(conn)).get(robot)
    if connection_id is not None:
        c = await get(conn, connection_id)
        want = ROBOT_TYPE[robot]
        if not c.usable or c.verified_type != want:
            await event(
                conn,
                c.id,
                "REFUSED",
                False,
                f"{ROBOTS[robot]} refused: it needs a connected account HFM verified as {want}",
                now=now,
                actor=actor,
                code="WRONG_ACCOUNT",
            )
            raise ConflictError(
                f"The {ROBOTS[robot]} can only use a connected account that HFM verified as "
                f"{want}; this one is {c.verified_type or 'not verified'} ({c.status})."
            )
    if current is not None and current != connection_id:
        old = await get(conn, current)
        if await _robot_running_on(conn, old):
            raise ConflictError(f"the {ROBOTS[robot]} is running: stop it before moving it")
        open_here = await _system_open(conn, old.slot_info.adapter if old.slot_info else None)
        if open_here and not leave_open:
            await event(
                conn,
                old.id,
                "REFUSED",
                False,
                f"{ROBOTS[robot]} move refused: {open_here} open order(s)",
                now=now,
                actor=actor,
                code="OPEN_POSITIONS",
            )
            raise ConflictError(
                f"The {ROBOTS[robot]} has {open_here} order(s) open on {old.nickname}. Let them "
                "finish, or tick 'leave them at HFM' (they keep their stop-loss there)."
            )
    await conn.execute(
        pg_insert(robot_assignments)
        .values(robot=robot, connection_id=connection_id, assigned_at=now, assigned_by=_by(actor))
        .on_conflict_do_update(
            index_elements=[robot_assignments.c.robot],
            set_={"connection_id": connection_id, "assigned_at": now, "assigned_by": _by(actor)},
        )
    )
    target = connection_id or current
    if target is not None:
        await event(
            conn,
            target,
            "ASSIGNED",
            True,
            f"{ROBOTS[robot]} {'assigned' if connection_id else 'unassigned'}",
            now=now,
            actor=actor,
            detail={"robot": robot},
        )
    await audit(
        conn,
        action="broker.robot_assigned",
        actor=actor,
        occurred_at=now,
        entity_type="robot",
        entity_id=robot,
        payload={
            "connection": str(connection_id) if connection_id else None,
            "left_open": bool(leave_open),
        },
    )


async def set_execution(
    conn: AsyncConnection, connection_id: uuid.UUID, on: bool, *, actor: Actor, now: datetime
) -> None:
    """The account's own execution switch. DEMO: on only once connected and verified DEMO.
    REAL: never from here (live trading needs its separate authorization)."""
    c = await get(conn, connection_id)
    if on and c.declared_type == "REAL":
        await event(
            conn,
            c.id,
            "REFUSED",
            False,
            "REAL execution refused here",
            now=now,
            actor=actor,
            code="LIVE_LOCKED",
        )
        raise ConflictError(
            "REAL execution cannot be switched on here. Live trading needs its own, separate "
            "authorization (Go-live: every gate, broker-held stops, the risk settings and your "
            "activation with a code); connecting a REAL account never enables it."
        )
    if on and not (c.usable and c.verified_type == "DEMO"):
        raise ConflictError("connect the account and let HFM verify it as DEMO first")
    await conn.execute(
        update(broker_connections)
        .where(broker_connections.c.id == c.id)
        .values(execution_enabled=on, updated_at=now, updated_by=_by(actor))
    )
    await event(
        conn,
        c.id,
        "EXECUTION",
        True,
        f"DEMO execution switched {'on' if on else 'off'}",
        now=now,
        actor=actor,
    )
    await audit(
        conn,
        action="broker.execution_switched",
        actor=actor,
        occurred_at=now,
        entity_type="broker_connection",
        entity_id=c.id,
        payload={"on": on, "type": c.declared_type},
    )


async def entry_problem(conn: AsyncConnection, slot: str, now: datetime) -> str | None:
    """Why a new position must not be opened through ``slot`` now (None: it may). Checked again
    just before every entry is sent. A terminal Broker Connections does not manage yet keeps the
    adapter's own checks (the account opened on it, demo, fresh)."""
    c = await on_slot(conn, slot)
    if c is None:
        return None
    if not c.usable or c.verified_type != "DEMO":
        return f"{c.nickname} is not a connected, HFM-verified DEMO account ({c.status})"
    if not c.execution_enabled:
        return f"DEMO execution is off for {c.nickname} (Broker Connections)"
    t = await terminal(conn, slot)
    if not t.fresh(now):
        return f"{c.slot_info.label if c.slot_info else slot} has not reported for over a minute"
    if t.fingerprint != c.fingerprint:
        return (
            f"MT5 on {c.slot_info.label if c.slot_info else slot} is not signed in to {c.nickname}"
        )
    if t.account.get("trade_mode") != "DEMO":
        return "HFM no longer reports this account as DEMO"
    if not t.terminal.get("connected"):
        return "MT5 has lost its link to HFM's server"
    info = c.slot_info
    if info is not None and info.adapter is not None:
        opened = await opened_on(conn, info.adapter)
        if opened is not None and opened != c.fingerprint:
            return f"the HFM account opened on {info.label} is not {c.nickname}"
    return None


@dataclass(frozen=True)
class RobotAccount:
    connection: Connection
    account_id: uuid.UUID | None  # the system's HFM account on its terminal (None: not opened)
    account_alias: str | None
    problem: str | None


async def robot_account(conn: AsyncConnection, robot: str, now: datetime) -> RobotAccount | None:
    """The account a robot is assigned to, the system's HFM account on its terminal, and why it
    cannot trade there now (None when it can). Shown before START, checked by START."""
    target = (await assignments(conn)).get(robot)
    if target is None:
        return None
    c = await get(conn, target)
    info = c.slot_info
    row = None
    if info is not None and info.adapter is not None:
        row = (
            await conn.execute(
                select(accounts.c.id, accounts.c.alias).where(
                    accounts.c.broker_adapter == info.adapter,
                    accounts.c.kind == "SANDBOX",
                    accounts.c.status == "ACTIVE",
                )
            )
        ).first()
    problem = await entry_problem(conn, c.slot or "", now) if c.slot else "no terminal"
    if robot == "LIVE_ROBOT":
        problem = "live trading is not enabled (its own, separate authorization)"
    elif row is None and problem is None:
        problem = f"no HFM account is opened on {info.label if info else c.slot} yet"
    return RobotAccount(c, row.id if row else None, row.alias if row else None, problem)


# ---- health: every 30 seconds ------------------------------------------------------------------


async def check_health(db: Database, now: datetime) -> list[str]:
    """Each connected account's health from its terminal's report, kept on the account, with an
    incident while it is not OK; a terminal that restarted signed out is signed in again with the
    saved password (at most every 2 minutes, never after HFM refused it 3 times)."""
    out: list[str] = []
    async with db.transaction() as conn:
        for c in await all_connections(conn):
            if c.status not in ("CONNECTED", "FAILED") or c.slot is None:
                continue
            t = await terminal(conn, c.slot)
            label = t.slot.label
            health, reason = "OK", None
            if t.received_at is None:
                health, reason = "DOWN", f"{label} has never reported"
            elif not t.fresh(now):
                age = int((now - t.received_at).total_seconds())
                health, reason = (
                    "DOWN",
                    (
                        f"{label} has not reported for {age} s: the bridge computer is off, MT5 is "
                        "closed or the network is down"
                    ),
                )
            elif not t.account:
                health, reason = "DOWN", f"MT5 on {label} is not signed in"
            elif t.fingerprint != c.fingerprint:
                health, reason = "DOWN", f"MT5 on {label} is signed in to another account"
            elif t.account.get("trade_mode") != c.declared_type:
                health, reason = "DOWN", "HFM reports a different account type"
            elif not t.terminal.get("connected"):
                health, reason = "DEGRADED", "MT5 has lost its link to HFM's server (network)"
            elif c.declared_type == "REAL" and t.account.get("trade_allowed"):
                health, reason = "DEGRADED", advice("TRADING_PASSWORD", "", label)
            elif c.declared_type == "DEMO" and not t.terminal.get("trade_allowed"):
                health, reason = "DEGRADED", "Algo Trading is off in MT5"
            values: dict[str, Any] = {}
            if health != c.health or reason != c.health_reason:
                values |= {"health": health, "health_reason": reason}
            if health != "DOWN" and t.received_at is not None:
                values["last_sync_at"] = t.received_at
            if values:
                await conn.execute(
                    update(broker_connections)
                    .where(broker_connections.c.id == c.id)
                    .values(**values)
                )
            if health != c.health:
                await event(
                    conn,
                    c.id,
                    "HEALTH",
                    health == "OK",
                    reason or "healthy",
                    now=now,
                    actor=Actor.system("broker-health"),
                    code=health,
                )
            key = f"broker.connection:{c.id}"
            if health == "OK":
                await resolve_by_key(conn, dedup_key=key, now=now, resolution="healthy again")
            elif c.status == "CONNECTED":
                running = await _robot_running_on(conn, c)
                await raise_incident(
                    conn,
                    dedup_key=key,
                    category=Category.TRADING,
                    alert=Alert(
                        title=f"{c.nickname}: {health}",
                        severity="CRITICAL" if running and health == "DOWN" else "WARNING",
                        what_happened=reason or health,
                        verified="The terminal's own report through the bridge.",
                        uncertain="Whether it recovers by itself (MT5 reconnects on its own).",
                        risk_now="No new trade is opened on this account until it is OK; stops "
                        "already placed stay at HFM.",
                        action_taken="New entries refused on this account.",
                        owner_action="Usually nothing: it recovers by itself. If it lasts, open "
                        "Admin → Broker Connections and press Test.",
                    ),
                    now=now,
                )
            # Restart recovery: a terminal that came back signed out is signed in again.
            if (
                c.status == "CONNECTED"
                and t.fresh(now)
                and not t.account
                and t.version >= LOGIN_VERSION
                and c.login_failures < AUTO_LOGIN_LIMIT
                and (c.last_login_at is None or now - c.last_login_at >= AUTO_LOGIN_AFTER)
                and await has_password(conn, c.id)
            ):
                await _auto_login(conn, c, now)
                out.append(f"{c.nickname}: signing in again")
            out.append(f"{c.nickname}: {health}")
    return out


async def _auto_login(conn: AsyncConnection, c: Connection, now: datetime) -> None:
    command_id = uuid7()
    await conn.execute(
        bridge_commands.insert().values(
            id=command_id,
            bridge=c.slot,
            kind="LOGIN",
            payload={
                "connection_id": str(c.id),
                "login": c.login,
                "server": c.server,
                "expected_fingerprint": c.fingerprint,
                "expected_mode": c.declared_type,
                "allow_open": False,
                "automatic": True,
            },
            status="PENDING",
            created_at=now,
            expires_at=now + LOGIN_TTL,
        )
    )
    await conn.execute(
        update(broker_connections).where(broker_connections.c.id == c.id).values(last_login_at=now)
    )
    await event(
        conn,
        c.id,
        "SIGN_IN",
        True,
        "terminal signed out (restart?): signing in again",
        now=now,
        actor=Actor.system("broker-health"),
        code="AUTO",
    )


# ---- the bridge's side: handing out commands, reading their results ------------------------------


async def hand_out(
    conn: AsyncConnection, settings: Settings | None, command: Any
) -> dict[str, Any] | None:
    """A connection command's payload as handed to the bridge: a sign-in gets its password here,
    in this one answer only. None: it cannot be handed out (no password, or none decryptable)."""
    payload = dict(command.payload or {})
    if command.kind != "LOGIN":
        return payload
    if settings is None:
        return None
    try:
        connection_id = uuid.UUID(str(payload.get("connection_id")))
    except ValueError:
        return None
    password = await password_for(conn, settings, connection_id)
    if password is None:
        return None
    payload.pop("automatic", None)
    return payload | {"password": password}


async def on_results(conn: AsyncConnection, bridge: str, results: list[Any], now: datetime) -> None:
    """Sign-in and identity results the bridge reported in this sync."""
    ids = [r.command_id for r in results]
    if not ids:
        return
    rows = {
        r.id: r
        for r in await conn.execute(
            select(bridge_commands).where(
                bridge_commands.c.id.in_(ids),
                bridge_commands.c.bridge == bridge,
                bridge_commands.c.kind.in_(sorted(CONNECTION_COMMANDS)),
            )
        )
    }
    for r in results:
        command = rows.get(r.command_id)
        if command is not None:
            await record_result(conn, command, r.status, dict(r.result or {}), r.error, now=now)


async def expired(conn: AsyncConnection, command_ids: list[uuid.UUID], now: datetime) -> None:
    """Sign-ins the bridge never took: said on the account, in words."""
    for row in await conn.execute(
        select(bridge_commands).where(
            bridge_commands.c.id.in_(command_ids),
            bridge_commands.c.kind.in_(sorted(CONNECTION_COMMANDS)),
        )
    ):
        p = row.payload or {}
        try:
            c = await get(conn, uuid.UUID(str(p.get("connection_id"))))
        except (NotFoundError, ValueError):
            continue
        label = c.slot_info.label if c.slot_info else str(c.slot)
        message = (
            f"The bridge on {label} did not take the request within "
            f"{int(LOGIN_TTL.total_seconds())} s: the bridge computer is off or not connected."
        )
        await event(
            conn,
            c.id,
            "TEST",
            False,
            message,
            now=now,
            actor=Actor.system("broker-connections"),
            code="NO_BRIDGE",
        )
        await conn.execute(
            update(broker_connections)
            .where(broker_connections.c.id == c.id)
            .values(last_error=message)
        )
