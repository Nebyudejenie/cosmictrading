"""Admin → Broker Connections → HFM MT5 (S83): the owner's accounts, their tests, the robots'
assignments and the execution switches, all from the portal. Administrators only; every change
needs a fresh authenticator code, except switching DEMO execution OFF (it only takes risk away).
Passwords are typed into password fields, encrypted at once and never sent back to a page."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.core.errors import NotFoundError, ValidationFailedError
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import require
from cosmic.trading import connections
from cosmic.trading.brokers.mt5_bridge import SLOTS
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.views import render

router = APIRouter()


def _id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise NotFoundError("no such broker connection") from None


async def _owner(request: Request) -> tuple[Any, Any]:
    """An administrator with a fresh authenticator code (asked in the form when it is stale)."""
    st = state(request)
    principal, form = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    require(principal, Capability.ADMINISTER_SYSTEM, now=st.clock.now(), settings=st.settings)
    return principal, form


def _tick(form: Any, name: str) -> bool:
    return str(form.get(name) or "") in ("1", "on", "yes", "true")


async def _overview(conn: Any, now: Any) -> dict[str, Any]:
    rows = []
    for c in await connections.all_connections(conn):
        t = await connections.terminal(conn, c.slot) if c.slot else None
        live = (
            t.account if t is not None and t.fingerprint == c.fingerprint and t.fresh(now) else {}
        )
        rows.append({"c": c, "terminal": t, "account": live})
    assigned = await connections.assignments(conn)
    robots = []
    for key, name in connections.ROBOTS.items():
        ra = await connections.robot_account(conn, key, now)
        robots.append({"key": key, "name": name, "account": ra, "id": assigned.get(key)})
    terminals = [await connections.terminal(conn, s) for s in SLOTS]
    return {"rows": rows, "robots": robots, "terminals": terminals}


@router.get("/brokers", include_in_schema=False)
async def brokers(request: Request) -> Response:
    """Every account, its type as HFM reports it, connection status, balance, equity, currency,
    robot and last synchronisation; the robots' assignments; the terminals."""
    st = state(request)
    principal = await authorize(request, Capability.ADMINISTER_SYSTEM)
    now = st.clock.now()
    async with st.db.connect() as conn:
        view = await _overview(conn, now)
    return await render(request, "brokers.html", principal, now=now, **view)


@router.get("/brokers/new", include_in_schema=False)
async def broker_new(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.ADMINISTER_SYSTEM)
    async with st.db.connect() as conn:
        servers = await connections.known_servers(conn)
        free = {k: await connections.free_slots(conn, k) for k in ("DEMO", "REAL")}
    return await render(request, "broker_new.html", principal, servers=servers, free=free)


@router.post("/brokers", include_in_schema=False)
async def broker_add(request: Request) -> Response:
    """Wizard steps 1-4: the account's details and password, encrypted at once."""
    st = state(request)
    principal, form = await _owner(request)
    server = str(form.get("server_manual") or "").strip() or str(form.get("server") or "")
    async with st.db.transaction() as conn:
        connection_id = await connections.create(
            conn,
            st.settings,
            nickname=str(form.get("nickname") or ""),
            login=str(form.get("login") or ""),
            server=server,
            declared_type=str(form.get("account_type") or ""),
            password=str(form.get("password") or ""),
            slot=str(form.get("slot") or ""),
            actor=principal.actor,
            now=st.clock.now(),
        )
    return RedirectResponse(f"/brokers/{connection_id}?added=1", status_code=303)


@router.get("/brokers/{connection_id}", include_in_schema=False)
async def broker_detail(request: Request, connection_id: str) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.ADMINISTER_SYSTEM)
    now = st.clock.now()
    async with st.db.connect() as conn:
        c = await connections.get(conn, _id(connection_id))
        t = await connections.terminal(conn, c.slot) if c.slot else None
        history = await connections.events(conn, c.id)
        password = await connections.has_password(conn, c.id)
        problem = await connections.switch_problem(conn, c, now)
        robots = [r for r, cid in (await connections.assignments(conn)).items() if cid == c.id]
    live = t.account if t is not None and t.fingerprint == c.fingerprint else {}
    return await render(
        request,
        "broker_detail.html",
        principal,
        c=c,
        terminal=t,
        account=live,
        fresh=bool(t and t.fresh(now)),
        history=history,
        has_password=password,
        switch_problem=problem,
        robots=[connections.ROBOTS[r] for r in robots],
        now=now,
    )


@router.post("/brokers/{connection_id}/test", include_in_schema=False)
async def broker_test(request: Request, connection_id: str) -> Response:
    """Wizard step 5-6: sign the terminal in (or check who is signed in) and read the account
    from HFM; waits for the answer (MT5 allows a sign-in up to a minute)."""
    st = state(request)
    principal, form = await _owner(request)
    async with st.db.transaction() as conn:
        c = await connections.get(conn, _id(connection_id))
        command = await connections.start_test(
            conn, c, actor=principal.actor, now=st.clock.now(), leave_open=_tick(form, "leave_open")
        )
    status = await connections.wait(st.db, command)
    return RedirectResponse(f"/brokers/{connection_id}?tested={status.lower()}", status_code=303)


@router.post("/brokers/{connection_id}/save", include_in_schema=False)
async def broker_save(request: Request, connection_id: str) -> Response:
    st = state(request)
    principal, _ = await _owner(request)
    async with st.db.transaction() as conn:
        await connections.save(conn, _id(connection_id), actor=principal.actor, now=st.clock.now())
    return RedirectResponse(f"/brokers/{connection_id}?saved=1", status_code=303)


@router.post("/brokers/{connection_id}/edit", include_in_schema=False)
async def broker_edit(request: Request, connection_id: str) -> Response:
    st = state(request)
    principal, form = await _owner(request)
    async with st.db.transaction() as conn:
        await connections.edit(
            conn,
            st.settings,
            _id(connection_id),
            nickname=str(form.get("nickname") or ""),
            login=str(form.get("login") or ""),
            server=str(form.get("server") or ""),
            password=str(form.get("password") or ""),
            actor=principal.actor,
            now=st.clock.now(),
        )
    return RedirectResponse(f"/brokers/{connection_id}?edited=1", status_code=303)


@router.post("/brokers/{connection_id}/password/revoke", include_in_schema=False)
async def broker_password_revoke(request: Request, connection_id: str) -> Response:
    st = state(request)
    principal, _ = await _owner(request)
    async with st.db.transaction() as conn:
        await connections.revoke_password(
            conn, _id(connection_id), actor=principal.actor, now=st.clock.now()
        )
    return RedirectResponse(f"/brokers/{connection_id}", status_code=303)


@router.post("/brokers/{connection_id}/disconnect", include_in_schema=False)
async def broker_disconnect(request: Request, connection_id: str) -> Response:
    st = state(request)
    principal, form = await _owner(request)
    async with st.db.transaction() as conn:
        await connections.disconnect(
            conn,
            _id(connection_id),
            actor=principal.actor,
            now=st.clock.now(),
            leave_open=_tick(form, "leave_open"),
        )
    return RedirectResponse(f"/brokers/{connection_id}", status_code=303)


@router.post("/brokers/{connection_id}/reconnect", include_in_schema=False)
async def broker_reconnect(request: Request, connection_id: str) -> Response:
    st = state(request)
    principal, _ = await _owner(request)
    async with st.db.transaction() as conn:
        await connections.reconnect(
            conn, _id(connection_id), actor=principal.actor, now=st.clock.now()
        )
    return RedirectResponse(f"/brokers/{connection_id}", status_code=303)


@router.post("/brokers/{connection_id}/execution", include_in_schema=False)
async def broker_execution(request: Request, connection_id: str) -> Response:
    """The account's own execution switch: ON needs a fresh code; OFF never does."""
    st = state(request)
    on = str((await request.form()).get("execution") or "") == "ON"
    if on:
        principal, _ = await _owner(request)
    else:
        principal, _ = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    async with st.db.transaction() as conn:
        await connections.set_execution(
            conn, _id(connection_id), on, actor=principal.actor, now=st.clock.now()
        )
    return RedirectResponse(f"/brokers/{connection_id}", status_code=303)


@router.post("/brokers/assign", include_in_schema=False)
async def broker_assign(request: Request) -> Response:
    st = state(request)
    principal, form = await _owner(request)
    robot = str(form.get("robot") or "")
    raw = str(form.get("connection") or "")
    if raw and raw != "none":
        target: uuid.UUID | None = _id(raw)
    elif raw == "none":
        target = None
    else:
        raise ValidationFailedError("choose an account, or none")
    async with st.db.transaction() as conn:
        await connections.assign(
            conn,
            robot,
            target,
            actor=principal.actor,
            now=st.clock.now(),
            leave_open=_tick(form, "leave_open"),
        )
    return RedirectResponse("/brokers?assigned=1", status_code=303)
