"""Manual Trading (S113): the owner's own workspace for the gold trades they place by hand on
their real HFM account. Read-only towards HFM: nothing here sends an order; the portal's own
BUY/SELL is built locked (``manual.execution``)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import require
from cosmic.manual import challenge, demo, execution, journal, live, page, risk
from cosmic.trading import exness_demo_trade
from cosmic.trading.brokers.mt5_bridge import ADAPTER_EXNESS
from cosmic.trading.policy import (
    EXNESS_LEVERAGE,
    EXNESS_OPEN_RISK_PCT,
    active_policy,
    change_policy,
    exness_demo_changes,
)
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.views import render
from cosmic.worker import queue

router = APIRouter()


def _preview_equity(params: Any) -> Decimal | None:
    value = page._num(params.get("preview_equity"), "Preview equity")
    if value is not None and not Decimal(1) <= value <= Decimal(10_000_000):
        return None
    return value


@router.get("/manual", include_in_schema=False)
async def manual(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    async with st.db.connect() as conn:
        w = await page.build(
            conn,
            st.clock.now(),
            st.settings,
            request.query_params,
            preview_equity=_preview_equity(request.query_params),
        )
    return await render(request, "manual.html", principal, w=w, refused=None)


async def _demo_trades(conn: Any, now: datetime, w: page.Workspace) -> dict[str, Any]:
    """The Exness demo and the plan's trade on it for each side (S119), worked out here on the
    server, never taken from the browser."""
    account = await demo.read(conn, now)
    rules, idea = w.rules, w.idea
    trades: dict[str, demo.DemoTrade] = {}
    if account is not None:
        for side in ("BUY", "SELL"):
            t = demo.work_out(
                account,
                side,
                rule_side=idea.rule_side,
                rule_entry=idea.entry,
                rule_stop=idea.stop,
                atr=idea.atr,
                rr=w.plan.rr,
                risk_pct=rules.risk_pct_default,
                max_risk_pct=rules.risk_pct_max,
            )
            if t is not None:
                trades[side] = t
    return {"demo": account, "trades": trades}


@router.get("/manual/demo", include_in_schema=False)
async def manual_demo(request: Request) -> Response:
    """Test the portal's BUY and SELL on the owner's Exness demo (S119, ADR-042)."""
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    now = st.clock.now()
    async with st.db.connect() as conn:
        w = await page.build(conn, now, st.settings, {}, light=True)
        found = await _demo_trades(conn, now, w)
        refusal = await exness_demo_trade.refusal(conn, now)
        alias = await exness_demo_trade.account_alias(conn)
        missing: dict[str, Any] = {}
        if alias is not None:
            _, current = await active_policy(conn)
            missing = exness_demo_changes(current, alias)
    return await render(
        request,
        "manual_demo.html",
        principal,
        w=w,
        refusal=refusal,
        now=now,
        alias=alias,
        policy_missing=sorted(missing),
        **found,
    )


@router.post("/manual/demo/trade", include_in_schema=False)
async def manual_demo_trade(request: Request) -> Response:
    """The plan's trade on the Exness demo, proposed for the owner's approval with their code;
    worked out again here from the side alone."""
    principal, form = await authorize_form(request, Capability.PROPOSE_TRADE)
    st = state(request)
    now = st.clock.now()
    side = str(form.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        raise ValidationFailedError("choose BUY or SELL")
    async with st.db.transaction() as conn:
        w = await page.build(conn, now, st.settings, {}, light=True)
        trade = (await _demo_trades(conn, now, w))["trades"].get(side)
        if trade is None or trade.blocked or trade.sizing is None:
            why = (
                "; ".join(x.text for x in trade.lines if x.level == "BLOCK")
                if trade is not None
                else "the demo is not reporting a fresh price and contract"
            )
            raise ValidationFailedError(f"no demo trade now: {why or 'it cannot be sized'}")
        s, t = trade.sizing, trade.ticket
        assert t.target is not None
        result = await exness_demo_trade.propose(
            conn,
            exness_demo_trade.DemoTicket(
                side=side,
                ounces=s.lots * trade.contract,
                lots=s.lots,
                stop=t.stop,
                target=t.target,
                risk_cash=s.risk_cash,
                why=trade.why,
            ),
            actor=principal.actor,
            now=now,
        )
    return RedirectResponse(f"/approvals/{result.intent_id}", status_code=303)


DEMO_ALIAS = "exness-demo"


@router.post("/manual/demo/open", include_in_schema=False)
async def manual_demo_open(request: Request) -> Response:
    """The owner's Exness demo becomes an account here (S119): the worker reads the demo's
    balance through its bridge and opens it once its broker contract has passed; a refusal
    becomes an incident with the reason. All of the demo's balance is managed: it is a test."""
    principal, _ = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    st = state(request)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        if await exness_demo_trade.account_alias(conn) is not None:
            raise ConflictError("your Exness demo is already set up here")
        account = await demo.read(conn, now)
        if account is None or not account.fresh or account.trade_mode != "DEMO":
            raise ConflictError("your Exness demo is not reporting: sign it in on terminal 3")
        if account.balance is None or account.balance <= 0 or account.positions:
            raise ConflictError(
                "your Exness demo needs a balance and no open trade to be set up here"
            )
        await queue.enqueue(
            conn,
            kind="trading.open_sandbox_account",
            now=now,
            payload={
                "alias": DEMO_ALIAS,
                "managed_capital": str(account.balance),
                "user_id": str(principal.user_id),
                "adapter": ADAPTER_EXNESS,
            },
            priority=queue.PRIORITY_SAFETY,
            idempotency_key=f"exness-demo-open:{now:%Y%m%d%H%M}",
        )
    return RedirectResponse("/manual/demo?opening=1", status_code=303)


@router.post("/manual/demo/allow", include_in_schema=False)
async def manual_demo_allow(request: Request) -> Response:
    """The owner's one button (S119): the risk policy lets their Exness demo trade on the PDF
    plan. A new policy version, with their code; only what is missing is added."""
    principal, _ = await authorize_form(request, Capability.CHANGE_RISK_LIMITS)
    st = state(request)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        alias = await exness_demo_trade.account_alias(conn)
        if alias is None:
            raise ConflictError("your Exness demo is not set up here yet")
        version, current = await active_policy(conn)
        changes = exness_demo_changes(current, alias)
        if not changes:
            raise ConflictError("the risk policy already allows your Exness demo")
        params = current.model_dump()
        params.update(changes)
        await change_policy(
            conn,
            principal=principal,
            new_params=params,
            reason=(
                "the owner's Exness demo (their choice, 2026-11-25): the portal's BUY and SELL "
                f"tested on it with their PDF plan; Exness and its gold allowed, a short allowed "
                f"there, held on margin at most 1:{EXNESS_LEVERAGE}, and {alias}'s own limits "
                f"({EXNESS_OPEN_RISK_PCT} % open at once); applied with the owner's code"
            )[:500],
            now=now,
            settings=st.settings,
            expected_version=version,
        )
    return RedirectResponse("/manual/demo?allowed=1", status_code=303)


@router.post("/manual/journal", include_in_schema=False)
async def manual_journal(request: Request) -> Response:
    """The owner records what they did with a preview: PLACED in MT5, SKIPPED (why) or a note.
    The ticket is recalculated here, never taken from the browser's figures."""
    principal, form = await authorize_form(request, Capability.MANAGE_APPROVED_POSITION)
    st = state(request)
    now = st.clock.now()
    kind = str(form.get("kind") or "").upper()
    async with st.db.transaction() as conn:
        w = await page.build(conn, now, st.settings, form)
        await journal.add(
            conn,
            principal=principal,
            kind=kind,
            ticket=w.ticket,
            sizing=w.sizing,
            checks=w.checks,
            insight={
                "direction": w.idea.direction,
                "rule_side": w.idea.rule_side,
                "why": w.idea.why,
                "probability": None
                if w.idea.probability is None
                else {
                    "verified": w.idea.probability.verified,
                    "p": str(w.idea.probability.p),
                    "n": w.idea.probability.n,
                },
                "quality": w.idea.quality_score,
                "strength": w.idea.strength,
                "preview": w.preview,
            },
            note=str(form.get("note") or ""),
            request_key=str(form.get("request_key") or ""),
            currency=w.account.currency,
            fingerprint=w.account.fingerprint,
            now=now,
        )
    return RedirectResponse(f"/manual?recorded={kind.lower()}", status_code=303)


@router.post("/manual/execute", include_in_schema=False)
async def manual_execute(request: Request) -> Response:
    """The portal's own BUY/SELL: LOCKED until the nine gates pass and the owner activates live
    trading. Refused here, with every lock still closed; nothing is created or sent."""
    principal, form = await authorize_form(request, Capability.APPROVE_TRADE)
    st = state(request)
    now = st.clock.now()
    require(principal, Capability.SUBMIT_ORDER, now=now, settings=st.settings)
    async with st.db.transaction() as conn:
        w = await page.build(conn, now, st.settings, form)
        summary: dict[str, object] = (
            {}
            if w.ticket is None
            else {
                "side": w.ticket.side,
                "entry": str(w.ticket.entry),
                "stop": str(w.ticket.stop),
                "target": None if w.ticket.target is None else str(w.ticket.target),
                "lots": None if w.sizing is None else str(w.sizing.lots),
            }
        )
        refused = await execution.submit(
            conn,
            st.settings,
            now,
            actor=principal.actor,
            summary=summary,
            account_live=w.account.live,
            account_read_only=w.account.read_only,
        )
    return await render(request, "manual.html", principal, w=w, refused=refused, status_code=409)


@router.post("/manual/rules", include_in_schema=False)
async def manual_rules(request: Request) -> Response:
    """A new version of the owner's risk rules (their code)."""
    principal, form = await authorize_form(request, Capability.CHANGE_RISK_LIMITS)
    st = state(request)
    async with st.db.transaction() as conn:
        # A field left empty keeps its current value (S117: it fell back to the default), and
        # the plan, which this form does not show, is kept.
        _, before = await risk.current(conn)
        params: dict[str, Any] = before.model_dump()
        for name in risk.RiskRules.model_fields:
            if name == "weekdays_only":
                params[name] = str(form.get(name) or "") == "on"
            elif name.startswith("plan_"):
                continue
            elif form.get(name) not in (None, ""):
                params[name] = str(form.get(name)).strip()
        await risk.change(
            conn,
            principal=principal,
            params=params,
            reason=str(form.get("reason") or ""),
            now=st.clock.now(),
            settings=st.settings,
        )
    return RedirectResponse("/manual?rules=changed#rules-h", status_code=303)


@router.post("/manual/plan", include_in_schema=False)
async def manual_plan(request: Request) -> Response:
    """The owner's PDF plan ($100 → $25,000) becomes their rules, with their code (S117): 10 %
    of the balance a trade, 2 trades a day, Monday to Friday, no pause; the balance now is its
    start."""
    principal, form = await authorize_form(request, Capability.CHANGE_RISK_LIMITS)
    st = state(request)
    now = st.clock.now()
    rr = page._num(form.get("rr"), "Reward") or challenge.DEFAULT_RR
    async with st.db.transaction() as conn:
        account = await live.read(conn, now)
        await risk.start_plan(
            conn,
            principal=principal,
            rr=rr,
            start_balance=account.balance if account.live else None,
            now=now,
            settings=st.settings,
        )
    return RedirectResponse("/manual?plan=started#plan-h", status_code=303)


@router.get("/manual/now.json", include_in_schema=False)
async def manual_now(request: Request) -> Response:
    """The page's live part (S117), read every few seconds by ``/static/manual.js``: the price,
    the setup, the recommendation and the next trade. Never an account number or a secret."""
    await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    now = st.clock.now()
    # One build serves every open page for a few seconds: it takes about 0.2 s of CPU in the
    # process that also passes the robots' orders to the bridge.
    cached: tuple[datetime, dict[str, Any]] | None = getattr(request.app.state, "manual_now", None)
    if cached is not None and timedelta(0) <= now - cached[0] < NOW_FOR:
        return JSONResponse(cached[1])
    async with st.db.connect() as conn:
        w = await page.build(conn, now, st.settings, {}, light=True)
    view = page.now_view(w)
    request.app.state.manual_now = (now, view)
    return JSONResponse(view)


NOW_FOR = timedelta(seconds=10)
