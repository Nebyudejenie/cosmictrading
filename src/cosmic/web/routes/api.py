"""JSON API /api/v1 (doc 10). Same services and authorisation as the portal."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from cosmic.core.errors import NotFoundError, ValidationFailedError
from cosmic.core.jsonutil import to_jsonable_lenient
from cosmic.db.schema_market import sources
from cosmic.identity.capabilities import Capability
from cosmic.market import data as md
from cosmic.sources.freshness import classify
from cosmic.trading import approvals as approvals_svc
from cosmic.trading import killswitch, queries
from cosmic.trading.accounts import get_account
from cosmic.web.deps import authorize, state, verify_csrf
from cosmic.worker import queue

router = APIRouter(prefix="/api/v1")


def ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(to_jsonable_lenient(data), status_code=status)


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise NotFoundError("not found") from None


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise ValidationFailedError("body must be JSON") from None
    if not isinstance(body, dict):
        raise ValidationFailedError("body must be a JSON object")
    return body


@router.get("/status")
async def status(request: Request) -> JSONResponse:
    st = state(request)
    await authorize(request, Capability.VIEW)
    now = st.clock.now()
    async with st.db.connect() as conn:
        banner = await queries.authority_banner(conn)
        ops = await queries.ops_overview(conn, now)
    return ok(
        {
            "authority": banner,
            "integrations": st.settings.integration_status(),
            "workers": ops["heartbeats"],
            "open_incidents": len(ops["incidents"]),
            "time": now,
        }
    )


@router.get("/sources")
async def list_sources(request: Request) -> JSONResponse:
    st = state(request)
    await authorize(request, Capability.READ_MARKET_DATA)
    now = st.clock.now()
    async with st.db.connect() as conn:
        rows = list(await conn.execute(select(sources)))
    return ok(
        [
            {
                "key": s.key,
                "name": s.name,
                "latency_class": s.latency_class,
                "last_success_at": s.last_success_at,
                "label": classify(
                    s.last_success_at,
                    now,
                    s.freshness_limit_seconds,
                    latency_class=s.latency_class,
                    enabled=s.enabled,
                ).value,
                "terms_status": s.terms_status,
            }
            for s in rows
        ]
    )


@router.get("/instruments")
async def instruments(request: Request) -> JSONResponse:
    st = state(request)
    await authorize(request, Capability.READ_MARKET_DATA)
    async with st.db.connect() as conn:
        rows = await md.list_instruments(conn)
    return ok([i.__dict__ for i in rows])


@router.get("/intents")
async def intents(request: Request) -> JSONResponse:
    await authorize(request, Capability.READ_ACCOUNT)
    wanted = request.query_params.get("status")
    async with state(request).db.connect() as conn:
        rows = await queries.intent_list(conn, statuses=(wanted,) if wanted else None)
    return ok(rows)


@router.get("/intents/{intent_id}")
async def intent(request: Request, intent_id: str) -> JSONResponse:
    await authorize(request, Capability.READ_ACCOUNT)
    async with state(request).db.connect() as conn:
        d = await queries.intent_detail(conn, _uuid(intent_id))
    if d is None:
        raise NotFoundError("unknown intent")
    return ok(
        {
            "intent": dict(d.intent._mapping),
            "instrument": d.instrument_key,
            "packet": d.packets[0].packet if d.packets else None,
            "packet_hash": d.packets[0].packet_hash if d.packets else None,
            "risk_check_runs": [dict(r._mapping) for r in d.risk_runs],
            "approvals": [
                {k: v for k, v in dict(a._mapping).items() if k != "nonce"} for a in d.approvals
            ],
            "orders": [dict(o._mapping) for o in d.orders],
        }
    )


@router.post("/intents/{intent_id}/challenge")
async def challenge(request: Request, intent_id: str) -> JSONResponse:
    st = state(request)
    principal = await authorize(request, Capability.APPROVE_TRADE)
    await verify_csrf(request, principal)
    async with st.db.transaction() as conn:
        ch = await approvals_svc.issue_challenge(
            conn,
            principal=principal,
            intent_id=_uuid(intent_id),
            channel="PORTAL",
            now=st.clock.now(),
        )
    return ok(
        {
            "challenge": ch.token,
            "packet_hash": ch.packet_hash,
            "terms": ch.terms,
            "expires_at": ch.expires_at,
        },
        201,
    )


@router.post("/intents/{intent_id}/decision")
async def decision(request: Request, intent_id: str) -> JSONResponse:
    st = state(request)
    principal = await authorize(request, Capability.APPROVE_TRADE)  # includes fresh step-up MFA
    await verify_csrf(request, principal)
    body = await _json(request)
    if body.get("decision") not in ("APPROVE", "REJECT", "REQUEST_CHANGES"):
        raise ValidationFailedError("decision must be APPROVE, REJECT or REQUEST_CHANGES")
    async with st.db.transaction() as conn:
        rec = await approvals_svc.decide(
            conn,
            principal=principal,
            token=str(body.get("challenge", "")),
            packet_hash=str(body.get("packet_hash", "")),
            decision=body["decision"],
            now=st.clock.now(),
            settings=st.settings,
            note=body.get("note"),
        )
    return ok(
        {
            "approval_id": rec.approval_id,
            "decision": rec.decision,
            "status": rec.status,
            "expires_at": rec.expires_at,
            "intent_id": intent_id,
        },
        201,
    )


@router.get("/orders")
async def orders(request: Request) -> JSONResponse:
    await authorize(request, Capability.READ_ACCOUNT)
    async with state(request).db.connect() as conn:
        rows = await queries.order_list(conn)
    return ok(rows)


@router.get("/orders/{order_id}")
async def order(request: Request, order_id: str) -> JSONResponse:
    await authorize(request, Capability.READ_ACCOUNT)
    async with state(request).db.connect() as conn:
        d = await queries.order_detail(conn, _uuid(order_id))
    if d is None:
        raise NotFoundError("unknown order")
    return ok(
        {
            "order": dict(d["order"]._mapping),
            "events": [dict(e._mapping) for e in d["events"]],
            "fills": [dict(f._mapping) for f in d["fills"]],
        }
    )


@router.get("/accounts/{alias}")
async def account(request: Request, alias: str) -> JSONResponse:
    st = state(request)
    await authorize(request, Capability.READ_ACCOUNT)
    async with st.db.connect() as conn:
        views = [
            v for v in await queries.portfolio(conn, st.clock.now()) if v.account.alias == alias
        ]
    if not views:
        raise NotFoundError("unknown account")
    v = views[0]
    return ok(
        {
            "alias": alias,
            "kind": v.account.kind,
            "simulated": v.account.kind == "PAPER",
            "reconciliation_status": v.account.reconciliation_status,
            "cash": v.cash,
            "equity": v.equity,
            "positions": v.positions,
            "open_orders": v.open_orders,
            "unknown_orders": v.unknown_orders,
        }
    )


@router.post("/accounts/{alias}/reconcile")
async def reconcile(request: Request, alias: str) -> JSONResponse:
    st = state(request)
    principal = await authorize(request, Capability.RECONCILE)
    await verify_csrf(request, principal)
    async with st.db.transaction() as conn:
        acct = await get_account(conn, alias=alias)
        job = await queue.enqueue(
            conn,
            kind="trading.reconcile",
            now=st.clock.now(),
            priority=queue.PRIORITY_SAFETY,
            payload={"account_id": str(acct.id), "trigger": "api"},
        )
    return ok({"queued_job": job}, 202)


@router.post("/authority/emergency-stop")
async def emergency_stop(request: Request) -> JSONResponse:
    st = state(request)
    principal = await authorize(request, Capability.ACTIVATE_EMERGENCY_STOP)
    await verify_csrf(request, principal)
    body = await _json(request)
    async with st.db.transaction() as conn:
        act = await killswitch.activate(
            conn,
            actor=principal.actor,
            principal=principal,
            reason=str(body.get("reason", ""))[:300],
            now=st.clock.now(),
        )
        await queue.enqueue(
            conn,
            kind="trading.emergency_followup",
            now=st.clock.now(),
            priority=queue.PRIORITY_SAFETY,
            payload={
                "entry_orders": [str(i) for i in act.entry_orders_to_cancel],
                "user_id": str(principal.user_id),
            },
        )
    return ok(
        {
            "mode": "EMERGENCY_STOP",
            "approvals_revoked": act.approvals_revoked,
            "entry_orders_cancelling": len(act.entry_orders_to_cancel),
            "meaning": killswitch.STOP_MEANING,
        },
        202,
    )
