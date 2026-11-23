"""Manual Trading (S113): the owner's own workspace for the gold trades they place by hand on
their real HFM account. Read-only towards HFM: nothing here sends an order; the portal's own
BUY/SELL is built locked (``manual.execution``)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from cosmic.identity.capabilities import Capability
from cosmic.identity.service import require
from cosmic.manual import challenge, execution, journal, live, page, risk
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.views import render

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
