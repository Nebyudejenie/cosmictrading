"""JSON API /api/v1 (doc 10). Same services and authorisation as the portal."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select

from cosmic.ai import admin as ai_admin
from cosmic.core.errors import NotFoundError, ValidationFailedError
from cosmic.core.jsonutil import to_jsonable_lenient
from cosmic.db.schema import audit_events
from cosmic.db.schema_market import sources
from cosmic.identity.capabilities import Capability
from cosmic.market import data as md
from cosmic.ops.integrations import integration_view
from cosmic.research import queries as research_queries
from cosmic.research.search import search as research_search
from cosmic.sources.contract import TIMEFRAME_SECONDS
from cosmic.sources.freshness import classify
from cosmic.trading import approvals as approvals_svc
from cosmic.trading import authority, golive, killswitch, queries
from cosmic.trading.accounts import get_account
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, Mode
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
        integrations = await integration_view(conn, st.settings, now)
    return ok(
        {
            "authority": banner,
            "integrations": integrations,
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
            "review": _row(d.review),
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
            "simulated": v.account.kind in SIMULATED_ACCOUNT_KINDS,
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


# ---- read models that previously existed only as portal pages (doc 10) -----------------


def _row(r: Any) -> dict[str, Any] | None:
    return None if r is None else dict(r._mapping)


@router.get("/market/{canonical_key:path}/bars")
async def market_bars(request: Request, canonical_key: str) -> JSONResponse:
    st = state(request)
    await authorize(request, Capability.READ_MARKET_DATA)
    timeframe = request.query_params.get("timeframe", "1h")
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValidationFailedError(f"timeframe must be one of {', '.join(TIMEFRAME_SECONDS)}")
    try:
        limit = min(max(int(request.query_params.get("limit", "200")), 1), 720)
    except ValueError:
        raise ValidationFailedError("limit must be an integer") from None
    now = st.clock.now()
    async with st.db.connect() as conn:
        inst = await md.instrument_by_key(conn, canonical_key)
        rows = await md.complete_bars(conn, inst.id, timeframe, limit=limit)
        quote = await md.latest_quote(conn, inst.id, now)
    return ok(
        {
            "instrument": inst.canonical_key,
            "timeframe": timeframe,
            "label": "DELAYED",  # public keyless data is never presented as live
            "latest_quote": None
            if quote is None
            else {
                "bid": quote.bid,
                "ask": quote.ask,
                "source_ts": quote.source_ts,
                "label": quote.label,
            },
            "bars": [b.__dict__ for b in rows],  # completed bars only, oldest first
        }
    )


@router.get("/golive/readiness")
async def golive_readiness(request: Request) -> JSONResponse:
    st = state(request)
    await authorize(request, Capability.VIEW)
    async with st.db.connect() as conn:
        gates = await golive.readiness(conn, st.clock.now())
    return ok(
        {
            "live_trading": "DISABLED"
            if any(g.status != "PASSED" for g in gates)
            else "ALL_GATES_PASSED",
            "gates": [g.__dict__ for g in gates],
        }
    )


@router.get("/ai/providers")
async def ai_providers(request: Request) -> JSONResponse:
    """Provider health. Never includes a key — only whether one is configured."""
    st = state(request)
    await authorize(request, Capability.VIEW)
    async with st.db.connect() as conn:
        keys = await integration_view(conn, st.settings, st.clock.now())
        overview = await ai_admin.provider_overview(conn, st.settings, st.clock.now(), keys)
    out = []
    for o in overview:
        p, att, s = o["provider"], o["attestation"], o["state"]
        out.append(
            {
                "provider": p.key,
                "name": p.name,
                "status": o["status"],
                "reasons": o["reasons"],
                "free_basis": p.free_basis,
                "reviewed_on": p.reviewed_on,
                "docs_url": p.docs_url,
                "key_configured": o["key_configured"],
                "attestation": None
                if att is None
                else {
                    "attested_at": att.attested_at,
                    "expires_at": att.expires_at,
                    "evidence_url": att.evidence_url,
                    "rpm_limit": att.rpm_limit,
                    "rpd_limit": att.rpd_limit,
                },
                "circuit": None
                if s is None
                else {
                    "state": s.circuit,
                    "opened_until": s.opened_until,
                    "rate_limited_until": s.rate_limited_until,
                    "last_success_at": s.last_success_at,
                    "disabled_reason": s.disabled_reason,
                },
                "models": [
                    {
                        "model_id": m["row"].model_id,
                        "tier": m["row"].tier,
                        "eligibility": m["eligibility"],
                    }
                    for m in o["models"]
                ],
                "usage_today": o["usage"],
                "reported_cost_today": o["reported_cost_today"],
            }
        )
    return ok({"policy": st.settings.ai_policy, "providers": out})


@router.get("/missions")
async def missions(request: Request) -> JSONResponse:
    await authorize(request, Capability.VIEW)
    async with state(request).db.connect() as conn:
        rows = await research_queries.mission_list(conn)
        board = await research_queries.task_board(conn)
    return ok({"missions": rows, "task_board": board})


@router.get("/missions/{mission_id}")
async def mission(request: Request, mission_id: str) -> JSONResponse:
    await authorize(request, Capability.VIEW)
    async with state(request).db.connect() as conn:
        d = await research_queries.mission_detail(conn, _uuid(mission_id))
    if d is None:
        raise NotFoundError("unknown mission")
    report = next((a.content for a in d["artifacts"] if a.kind == "report"), None)
    return ok(
        {
            "mission": _row(d["mission"]),
            "tasks": [
                {**dict(t._mapping), "review": _row(d["reviews"].get(t.id))} for t in d["tasks"]
            ],
            "claims": [dict(c._mapping) for c in d["claims"]],
            "opportunity": _row(d["opportunity"]),
            "transitions": [dict(t._mapping) for t in d["transitions"]],
            "report": report,
        }
    )


@router.get("/audit")
async def audit(request: Request) -> JSONResponse:
    """Audit events, newest first. Payloads were redacted before they were written."""
    await authorize(request, Capability.READ_ACCOUNT)
    q = select(audit_events).order_by(desc(audit_events.c.seq))
    entity_id = request.query_params.get("entity_id")
    entity_type = request.query_params.get("entity_type")
    if entity_id:
        q = q.where(audit_events.c.entity_id == entity_id[:64])
    if entity_type:
        q = q.where(audit_events.c.entity_type == entity_type[:64])
    try:
        limit = min(max(int(request.query_params.get("limit", "100")), 1), 500)
    except ValueError:
        raise ValidationFailedError("limit must be an integer") from None
    async with state(request).db.connect() as conn:
        rows = list(await conn.execute(q.limit(limit)))
    return ok([dict(r._mapping) for r in rows])


# ---- privileged actions that previously existed only as portal forms ---------------------


@router.post("/authority/mode")
async def authority_mode(request: Request) -> JSONResponse:
    """Change the authority mode. Capability, fresh MFA and preconditions are enforced by
    ``authority.change_mode`` exactly as for the portal; refusals answer 409 with the reason."""
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    await verify_csrf(request, principal)
    body = await _json(request)
    try:
        target = Mode(str(body.get("mode", "")))
    except ValueError:
        raise ValidationFailedError("unknown mode") from None
    async with st.db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=principal,
            target=target,
            reason=str(body.get("reason", "")),
            now=st.clock.now(),
            settings=st.settings,
        )
        banner = await queries.authority_banner(conn)
    return ok({"authority": banner})


@router.post("/approvals/{approval_id}/revoke")
async def approval_revoke(request: Request, approval_id: str) -> JSONResponse:
    st = state(request)
    principal = await authorize(request, Capability.APPROVE_TRADE)
    await verify_csrf(request, principal)
    body = await _json(request)
    async with st.db.transaction() as conn:
        await approvals_svc.revoke(
            conn,
            principal=principal,
            approval_id=_uuid(approval_id),
            reason=str(body.get("reason", "revoked by operator"))[:300] or "revoked",
            now=st.clock.now(),
        )
    return ok({"approval_id": approval_id, "status": "REVOKED"})


@router.get("/search")
async def search_api(request: Request) -> JSONResponse:
    principal = await authorize(request, Capability.VIEW)
    q = request.query_params.get("q", "")
    async with state(request).db.connect() as conn:
        hits = await research_search(
            conn, q, include_proposals=principal.has(Capability.READ_ACCOUNT)
        )
    return ok({"query": q[:200], "results": [h.__dict__ for h in hits]})
