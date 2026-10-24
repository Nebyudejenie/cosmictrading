"""Trading control center: approvals, proposals, portfolio, orders, operations, audit.

Handlers are thin: authorise → validate CSRF → call one domain service → redirect.
Broker-touching follow-ups are queued for the worker (single execution authority).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import desc, select

from cosmic.audit.service import Actor
from cosmic.core.errors import NotFoundError, StepUpRequiredError, ValidationFailedError
from cosmic.db.schema import audit_events
from cosmic.identity import service as identity
from cosmic.identity.capabilities import Capability
from cosmic.market import data as md
from cosmic.ops.incidents import acknowledge
from cosmic.ops.integrations import integration_view
from cosmic.reporting import decision
from cosmic.trading import approvals as approvals_svc
from cosmic.trading import authority, killswitch, mandates, queries
from cosmic.trading.accounts import list_accounts
from cosmic.trading.exits import propose_close
from cosmic.trading.modes import DESCRIPTIONS, Mode, selectable_targets
from cosmic.trading.paper_accounts import open_paper_account, paper_venues
from cosmic.trading.proposals import Candidate, create_candidate, repropose
from cosmic.trading.sandbox import ADAPTER as SANDBOX_DEFAULT
from cosmic.trading.sandbox import ALIAS as SANDBOX_ALIAS
from cosmic.trading.sandbox import SANDBOXES, sandbox_status
from cosmic.trading.services import TradingServices
from cosmic.web.charts import line_chart
from cosmic.web.deps import authorize, authorize_form, state, verify_csrf
from cosmic.web.views import render
from cosmic.worker import queue

router = APIRouter()
EQUITY_WINDOW = timedelta(days=7)  # the Portfolio chart

# What the Operations page says about each test venue (ADR-026, ADR-028).
SANDBOX_TEXT = {
    "binance_spot_testnet": {
        "note": "Virtual funds on Binance's Spot Test Network; not real money. Binance resets "
        "it about once a month.",
        "quote": "USDT",
        "example": "binance-test",
        "fingerprint_label": "key",
    },
    "hfm_mt5_demo": {
        "note": "HFM demo account through MetaTrader 5 on the owner's computer; virtual funds. "
        "Buy only, no leverage; stops sit on the position.",
        "quote": "USD",
        "example": "hfm-demo",
        "fingerprint_label": "account",
    },
}


def _services(request: Request) -> TradingServices:
    svc: TradingServices = request.app.state.trading
    return svc


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise NotFoundError("not found") from None


def _dec(form: Any, name: str, *, required: bool = True) -> Decimal | None:
    raw = str(form.get(name, "")).strip().replace(",", "")
    if not raw:
        if required:
            raise ValidationFailedError(f"{name.replace('_', ' ')} is required")
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValidationFailedError(f"{name.replace('_', ' ')} must be a number") from None


async def _enqueue(
    request: Request, kind: str, payload: dict[str, Any], key: str | None = None
) -> None:
    st = state(request)
    async with st.db.transaction() as conn:
        await queue.enqueue(
            conn,
            kind=kind,
            now=st.clock.now(),
            payload=payload,
            priority=queue.PRIORITY_SAFETY,
            idempotency_key=key,
        )


# ---- approvals ------------------------------------------------------------------------------


@router.get("/approvals", include_in_schema=False)
async def approvals_page(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    async with state(request).db.connect() as conn:
        pending = await queries.intent_list(conn, statuses=queries.QUEUE_STATUSES)
        recent = await queries.intent_list(conn, limit=30)
    return await render(request, "approvals.html", principal, pending=pending, recent=recent)


@router.get("/approvals/{intent_id}", include_in_schema=False)
async def approval_detail(request: Request, intent_id: str) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    async with st.db.connect() as conn:
        detail = await queries.intent_detail(conn, _uuid(intent_id))
        if detail is None:
            raise NotFoundError("unknown proposal")
        packet = detail.packets[0].packet if detail.packets else None
        guide = (
            await decision.guide(conn, packet, st.clock.now(), decision.timeframe_of(detail.intent))
            if packet is not None and detail.intent.status == "PROPOSED"
            else decision.Guide()
        )
    return await render(
        request,
        "approval_detail.html",
        principal,
        d=detail,
        packet=packet,
        guide=guide,
    )


@router.post("/approvals/{intent_id}/review", include_in_schema=False)
async def approval_review(request: Request, intent_id: str) -> Response:
    st = state(request)
    principal = await _approver(request)
    await verify_csrf(request, principal, await request.form())
    async with st.db.transaction() as conn:
        ch = await approvals_svc.issue_challenge(
            conn,
            principal=principal,
            intent_id=_uuid(intent_id),
            channel="PORTAL",
            now=st.clock.now(),
        )
    fresh = principal.step_up_fresh(st.clock.now(), st.settings.step_up_minutes)
    return await render(request, "approval_confirm.html", principal, ch=ch, needs_code=not fresh)


async def _approver(request: Request) -> identity.Principal:
    """Capability check without the step-up gate (the confirm form collects the code)."""
    from cosmic.web.deps import portal_principal

    principal = await portal_principal(request)
    if not principal.has(Capability.APPROVE_TRADE):
        from cosmic.core.errors import PermissionDeniedError

        raise PermissionDeniedError("missing capability APPROVE_TRADE")
    return principal


@router.post("/approvals/{intent_id}/decide", include_in_schema=False)
async def approval_decide(request: Request, intent_id: str) -> Response:
    st = state(request)
    principal = await _approver(request)
    form = await request.form()
    await verify_csrf(request, principal, form)
    now = st.clock.now()
    if not principal.step_up_fresh(now, st.settings.step_up_minutes):
        async with st.db.transaction() as conn:
            ok = await identity.step_up(
                conn,
                principal=principal,
                code=str(form.get("code", "")),
                settings=st.settings,
                now=now,
            )
        if not ok:
            raise StepUpRequiredError(
                "the authenticator code was missing, wrong or already used; review again"
            )
        async with st.db.transaction() as conn:
            refreshed = await identity.load_principal(
                conn,
                token=request.cookies.get(st.settings.cookie_name, ""),
                now=now,
                settings=st.settings,
            )
        if refreshed is None:  # session ended between the step-up and the decision
            raise StepUpRequiredError("your session has ended; sign in and review again")
        principal = refreshed
    decision = str(form.get("decision", ""))
    async with st.db.transaction() as conn:
        await approvals_svc.decide(
            conn,
            principal=principal,
            token=str(form.get("challenge", "")),
            packet_hash=str(form.get("packet_hash", "")),
            decision=decision,  # type: ignore[arg-type]
            now=now,
            settings=st.settings,
            note=str(form.get("note", ""))[:1000] or None,
        )
    return RedirectResponse(f"/approvals/{intent_id}", status_code=303)


@router.post("/approvals/{intent_id}/repropose", include_in_schema=False)
async def approval_repropose(request: Request, intent_id: str) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.PROPOSE_TRADE)
    await verify_csrf(request, principal, await request.form())
    async with st.db.transaction() as conn:
        await repropose(conn, intent_id=_uuid(intent_id), actor=principal.actor, now=st.clock.now())
    return RedirectResponse(f"/approvals/{intent_id}", status_code=303)


@router.post("/approvals/revoke/{approval_id}", include_in_schema=False)
async def approval_revoke(request: Request, approval_id: str) -> Response:
    st = state(request)
    principal = await _approver(request)
    form = await request.form()
    await verify_csrf(request, principal, form)
    async with st.db.transaction() as conn:
        await approvals_svc.revoke(
            conn,
            principal=principal,
            approval_id=_uuid(approval_id),
            reason=str(form.get("reason", "revoked by operator"))[:300] or "revoked",
            now=st.clock.now(),
        )
    return RedirectResponse("/approvals", status_code=303)


# ---- manual proposals -------------------------------------------------------------------------


@router.get("/proposals/new", include_in_schema=False)
async def proposal_form(request: Request) -> Response:
    principal = await authorize(request, Capability.PROPOSE_TRADE)
    st = state(request)
    async with st.db.connect() as conn:
        tradable = [i for i in await md.list_instruments(conn) if i.tradable]
        quotes = {
            i.canonical_key: await md.latest_quote(conn, i.id, st.clock.now()) for i in tradable
        }
        active = [a for a in await list_accounts(conn) if a.status == "ACTIVE"]
    return await render(
        request,
        "proposal_new.html",
        principal,
        instruments=tradable,
        quotes=quotes,
        accounts=active,  # chosen by name and environment, never typed (DIR-02)
        error=None,
    )


@router.post("/proposals", include_in_schema=False)
async def proposal_create(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.PROPOSE_TRADE)
    now = st.clock.now()
    key = str(form.get("instrument_key", ""))
    async with st.db.connect() as conn:
        inst = await md.instrument_by_key(conn, key)
        quote = await md.latest_quote(conn, inst.id, now)
    plan: dict[str, Any] = {}
    stop, stop_limit = (
        _dec(form, "stop_price", required=False),
        _dec(form, "stop_limit_price", required=False),
    )
    if stop is not None:
        plan["stop_loss"] = {"stop_price": stop, "limit_price": stop_limit or stop}
    tp = _dec(form, "take_profit_price", required=False)
    if tp is not None:
        plan["take_profit_price"] = tp
    cancel_after = str(form.get("cancel_unfilled_after_seconds", "")).strip()
    if cancel_after:
        plan["cancel_unfilled_after_seconds"] = int(cancel_after)
    data: dict[str, Any] = {
        "account_alias": str(form.get("account_alias", "")),
        "instrument_key": key,
        "side": "BUY",
        "effect": "OPEN",
        "quantity": _dec(form, "quantity"),
        "limit_price": _dec(form, "limit_price"),
        "price_band_low": _dec(form, "price_band_low"),
        "price_band_high": _dec(form, "price_band_high"),
        "max_slippage_bps": _dec(form, "max_slippage_bps"),
        "time_in_force": str(form.get("time_in_force", "GTC")),
        "horizon": str(form.get("horizon", "")),
        "thesis": str(form.get("thesis", "")),
        "plain_summary": str(form.get("plain_summary", "")),
        "evidence": [
            {
                "kind": "QUOTE",
                "ref": f"{quote.source_key}:{key}",
                "summary": f"latest quote bid {quote.bid} ask {quote.ask} ({quote.label.value})",
                "source_ts": quote.source_ts,
            },
            {"kind": "NOTE", "ref": "operator", "summary": "manual proposal by the operator"},
        ],
        "counterarguments": [str(form.get("counterargument", ""))],
        "red_team": str(form.get("red_team", "")),
        "scenarios": [{"name": "operator", "description": str(form.get("scenario", ""))}],
        "invalidation": [str(form.get("invalidation", ""))],
        "entry_conditions": str(form.get("entry_conditions", "")),
        "management_plan": plan,
    }
    try:
        candidate = Candidate.model_validate(data)
    except ValueError as exc:
        raise ValidationFailedError(f"proposal is incomplete or inconsistent: {exc}") from None
    async with st.db.transaction() as conn:
        result = await create_candidate(conn, candidate=candidate, actor=principal.actor, now=now)
    return RedirectResponse(f"/approvals/{result.intent_id}", status_code=303)


# ---- portfolio and orders ---------------------------------------------------------------------


@router.get("/portfolio", include_in_schema=False)
async def portfolio_page(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.READ_ACCOUNT)
    now = st.clock.now()
    curves = {}
    async with st.db.connect() as conn:
        views = await queries.portfolio(conn, now)
        reviews = await queries.reviews(conn, limit=20)
        for v in views:
            history = await queries.equity_history(conn, v.account.id, now - EQUITY_WINDOW)
            curves[v.account.alias] = line_chart(
                [t for t, _ in history],
                [float(e) for _, e in history],  # drawing only; the figures stay Decimal
                label=f"{v.account.alias} equity in {v.account.base_currency}, last 7 days",
                empty="Not enough equity samples yet (one every 5 minutes).",
            )
    return await render(
        request, "portfolio.html", principal, views=views, reviews=reviews, curves=curves
    )


@router.post("/portfolio/{alias}/reconcile", include_in_schema=False)
async def portfolio_reconcile(request: Request, alias: str) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.RECONCILE)
    await verify_csrf(request, principal, await request.form())
    from cosmic.trading.accounts import get_account

    async with st.db.connect() as conn:
        acct = await get_account(conn, alias=alias)
    await _enqueue(
        request, "trading.reconcile", {"account_id": str(acct.id), "trigger": "operator"}
    )
    return RedirectResponse("/portfolio", status_code=303)


@router.post("/portfolio/{alias}/exit", include_in_schema=False)
async def portfolio_exit(request: Request, alias: str) -> Response:
    """Propose closing a position: an approved exit that replaces its protective stops."""
    st = state(request)
    principal, form = await authorize_form(request, Capability.PROPOSE_TRADE)
    from cosmic.trading.accounts import get_account

    async with st.db.transaction() as conn:
        account = await get_account(conn, alias=alias)
        inst = await md.instrument_by_key(conn, str(form.get("instrument_key", "")))
        result = await propose_close(
            conn,
            account=account,
            inst=inst,
            reason=str(form.get("reason", "")),
            actor=principal.actor,
            now=st.clock.now(),
        )
    return RedirectResponse(f"/approvals/{result.intent_id}", status_code=303)


@router.get("/orders", include_in_schema=False)
async def orders_page(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    async with state(request).db.connect() as conn:
        rows = await queries.order_list(conn)
    return await render(request, "orders.html", principal, orders=rows)


@router.get("/orders/{order_id}", include_in_schema=False)
async def order_page(request: Request, order_id: str) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    async with state(request).db.connect() as conn:
        detail = await queries.order_detail(conn, _uuid(order_id))
    if detail is None:
        raise NotFoundError("unknown order")
    return await render(request, "order_detail.html", principal, d=detail)


@router.post("/orders/{order_id}/cancel", include_in_schema=False)
async def order_cancel(request: Request, order_id: str) -> Response:
    principal = await authorize(request, Capability.CANCEL_ORDER)
    await verify_csrf(request, principal, await request.form())
    await _enqueue(
        request, "trading.cancel_order", {"order_id": order_id, "user_id": str(principal.user_id)}
    )
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


# ---- operations ---------------------------------------------------------------------------------


@router.get("/ops", include_in_schema=False)
async def ops_page(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    now = st.clock.now()
    async with st.db.connect() as conn:
        overview = await queries.ops_overview(conn, now)
        checklist, items = await killswitch.recovery_status(conn)
        live_unmet = await authority.live_preconditions(conn, now)
        integrations = await integration_view(conn, st.settings, now)
        sandboxes = [
            {**await sandbox_status(conn, adapter), **SANDBOX_TEXT[adapter]}
            for adapter in SANDBOXES
        ]
        current = (await authority.get_state(conn)).mode
        auto_trader = await mandates.view(conn, now)
    targets = selectable_targets(
        current, cap=authority.mode_cap(st.settings), live_available=not live_unmet
    )
    return await render(
        request,
        "ops.html",
        principal,
        o=overview,
        checklist=checklist,
        items=items,
        mode_choices=[(m.value, DESCRIPTIONS[m]) for m in targets],
        mode_default=Mode.PAPER_TRADING.value if Mode.PAPER_TRADING in targets else None,
        mode_cap=authority.mode_cap(st.settings).value,
        mode_unavailable=[
            m.value for m in Mode if m not in (*targets, current, Mode.EMERGENCY_STOP)
        ],
        live_unmet=live_unmet,
        integrations=integrations,
        sandboxes=sandboxes,
        auto_trader=auto_trader,
        paper_venues=paper_venues(frozenset(st.settings.retired_venues)),
        job_health=next(  # the freshest worker's record of its scheduled jobs
            (b["info"]["jobs"] for b in overview["heartbeats"] if (b["info"] or {}).get("jobs")),
            None,
        ),
        backups=next(  # the freshest worker's view of the backup directory
            (
                b["info"]["backups"]
                for b in overview["heartbeats"]
                if (b["info"] or {}).get("backups")
            ),
            None,
        ),
    )


@router.post("/ops/emergency-stop", include_in_schema=False)
async def ops_emergency_stop(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.ACTIVATE_EMERGENCY_STOP)
    async with st.db.transaction() as conn:
        act = await killswitch.activate(
            conn,
            actor=principal.actor,
            principal=principal,
            reason=str(form.get("reason", ""))[:300],
            now=st.clock.now(),
        )
    await _enqueue(
        request,
        "trading.emergency_followup",
        {
            "entry_orders": [str(i) for i in act.entry_orders_to_cancel],
            "user_id": str(principal.user_id),
        },
    )
    return RedirectResponse("/ops", status_code=303)


@router.post("/ops/mode", include_in_schema=False)
async def ops_mode(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.VIEW)
    try:
        target = Mode(str(form.get("mode", "")))
    except ValueError:
        raise ValidationFailedError("unknown mode") from None
    async with st.db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=principal,
            target=target,
            reason=str(form.get("reason", "")),
            now=st.clock.now(),
            settings=st.settings,
        )
    return RedirectResponse("/ops", status_code=303)


@router.post("/ops/resume", include_in_schema=False)
async def ops_resume(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    try:
        target = Mode(str(form.get("mode", "")))
    except ValueError:
        raise ValidationFailedError("unknown mode") from None
    async with st.db.transaction() as conn:
        await killswitch.resume(
            conn,
            principal=principal,
            target=target,
            reason=str(form.get("reason", "")),
            positions_reviewed=form.get("positions_reviewed") == "yes",
            now=st.clock.now(),
            settings=st.settings,
        )
    return RedirectResponse("/ops", status_code=303)


@router.post("/ops/incidents/{incident_id}/ack", include_in_schema=False)
async def ops_ack(request: Request, incident_id: str) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    await verify_csrf(request, principal, await request.form())
    async with st.db.transaction() as conn:
        await acknowledge(
            conn, incident_id=_uuid(incident_id), user_id=principal.user_id, now=st.clock.now()
        )
    return RedirectResponse("/ops", status_code=303)


@router.post("/ops/paper-account", include_in_schema=False)
async def ops_paper_account(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    capital = _dec(form, "capital")
    if capital is None:
        raise ValidationFailedError("capital is required")
    choices = paper_venues(frozenset(st.settings.retired_venues))
    venue = str(form.get("venue", "") or next(iter(choices), "")).strip().upper()
    if venue not in choices:
        raise ValidationFailedError(f"paper trading is available on {', '.join(choices)}")
    async with st.db.transaction() as conn:
        account_id = await open_paper_account(
            conn,
            broker=_services(request).paper,
            alias=str(form.get("alias", "")).strip().lower(),
            initial_capital=capital,
            now=st.clock.now(),
            actor=principal.actor,
            venue_key=venue,
            base_currency=choices[venue],
        )
    await _enqueue(
        request, "trading.reconcile", {"account_id": str(account_id), "trigger": "account_opened"}
    )
    return RedirectResponse("/portfolio", status_code=303)


@router.post("/ops/sandbox-account", include_in_schema=False)
async def ops_sandbox_account(request: Request) -> Response:
    """Queue opening the Binance test-network account: the worker holds the keys, checks the
    contract-run gate and reads the venue's balances (ADR-026)."""
    principal, form = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    capital = _dec(form, "capital")
    if capital is None or capital <= 0:
        raise ValidationFailedError("the USDT to manage is required and must be positive")
    alias = str(form.get("alias", "")).strip().lower()
    if not SANDBOX_ALIAS.fullmatch(alias):
        raise ValidationFailedError("alias: lower-case letters, digits and dashes (2–41)")
    adapter = str(form.get("adapter", "") or SANDBOX_DEFAULT)
    if adapter not in SANDBOXES:
        raise ValidationFailedError("unknown test venue")
    await _enqueue(
        request,
        "trading.open_sandbox_account",
        {
            "alias": alias,
            "managed_capital": str(capital),
            "user_id": str(principal.user_id),
            "adapter": adapter,
        },
    )
    return RedirectResponse("/ops", status_code=303)


@router.post("/ops/auto-trader", include_in_schema=False)
async def ops_auto_trader_grant(request: Request) -> Response:
    """Grant the demo auto-trader its mandate (ADR-030): owner, fresh authenticator code."""
    st = state(request)
    principal, form = await authorize_form(request, Capability.APPROVE_TRADE)
    try:
        terms = mandates.MandateTerms(
            account_alias=str(form.get("account", "")).strip(),
            strategy_key=str(form.get("rule", "")),
            instruments=tuple(str(x) for x in form.getlist("instruments")),
            max_loss_per_trade=_dec(form, "max_loss_per_trade") or Decimal(0),
            max_trades_per_day=int(str(form.get("max_trades_per_day", "0")) or 0),
            max_open_positions=int(str(form.get("max_open_positions", "0")) or 0),
            daily_loss_limit=_dec(form, "daily_loss_limit") or Decimal(0),
            days=int(str(form.get("days", "0")) or 0),
            timeframe=str(form.get("timeframe", mandates.HOURLY)),
        )
    except ValueError as exc:  # pydantic errors are ValueErrors
        raise ValidationFailedError(f"the auto-trader terms are not valid: {exc}") from None
    async with st.db.transaction() as conn:
        await mandates.grant(
            conn, principal=principal, terms=terms, now=st.clock.now(), settings=st.settings
        )
    return RedirectResponse("/ops", status_code=303)


@router.post("/ops/auto-trader/{mandate_id}/revoke", include_in_schema=False)
async def ops_auto_trader_revoke(request: Request, mandate_id: str) -> Response:
    """Stopping the auto-trader only removes authority, so no fresh code is asked."""
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    form = await request.form()
    await verify_csrf(request, principal, form)
    async with st.db.transaction() as conn:
        await mandates.revoke(
            conn,
            principal=principal,
            mandate_id=_uuid(mandate_id),
            reason=str(form.get("reason", ""))[:300] or "stopped by the owner",
            now=st.clock.now(),
        )
    return RedirectResponse("/ops", status_code=303)


@router.get("/audit", include_in_schema=False)
async def audit_page(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    q = select(audit_events).order_by(desc(audit_events.c.seq)).limit(200)
    entity = request.query_params.get("entity_id")
    if entity:
        q = q.where(audit_events.c.entity_id == entity[:64])
    async with state(request).db.connect() as conn:
        rows = list(await conn.execute(q))
    return await render(request, "audit.html", principal, rows=rows, entity=entity)


def actor_for(user_id: str) -> Actor:
    return Actor.user(user_id)


# ---- Telegram linking ---------------------------------------------------------------------------


@router.get("/settings/telegram", include_in_schema=False)
async def telegram_settings(request: Request) -> Response:
    principal = await authorize(request, Capability.VIEW)
    st = state(request)
    async with st.db.connect() as conn:
        bot = (await integration_view(conn, st.settings, st.clock.now()))["telegram"]
    return await render(request, "telegram.html", principal, code=None, bot=bot)


@router.post("/settings/telegram/code", include_in_schema=False)
async def telegram_code(request: Request) -> Response:
    from cosmic.notify.telegram.linking import create_link_code

    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    await verify_csrf(request, principal, await request.form())
    async with st.db.transaction() as conn:
        code = await create_link_code(conn, principal=principal, now=st.clock.now())
        bot = (await integration_view(conn, st.settings, st.clock.now()))["telegram"]
    return await render(request, "telegram.html", principal, code=code, bot=bot)


@router.post("/settings/telegram/unlink", include_in_schema=False)
async def telegram_unlink(request: Request) -> Response:
    from cosmic.notify.telegram.linking import unlink

    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    await verify_csrf(request, principal, await request.form())
    async with st.db.transaction() as conn:
        await unlink(conn, principal=principal, now=st.clock.now())
    return RedirectResponse("/settings/telegram", status_code=303)
