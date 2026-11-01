"""Start with $100 (the $100 Gold Plan, S37).

- ``GET /rehearsal``: where $100 can trade gold, what history says, what a year could bring,
  how money grows, what must be ready first, and the rehearsals side by side once they run,
  each against its own replay of the same days (S39).
- ``POST /rehearsal/start``: open the $100 rehearsal account (the first time) and start its
  daily auto-trader on Binance gold, with the owner's code; or renew it from its money now.
- ``POST /rehearsal/ready``: get both rehearsals ready in one risk-policy change, with the
  owner's code (S46): the fees and lists they need, and the advised limits the owner ticks.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.identity.capabilities import Capability
from cosmic.reporting import rehearsal
from cosmic.trading import measured
from cosmic.trading import rehearsal as trading_rehearsal
from cosmic.trading.policy import active_policy, change_policy
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.routes.autotrader import expect_charts
from cosmic.web.views import render
from cosmic.worker import queue

router = APIRouter()


def _monthly(raw: object) -> Decimal:
    text = str(raw or "").replace(",", "").replace("$", "").strip()
    if not text:
        return Decimal(0)
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValidationFailedError("the monthly amount must be a number, like 25") from None
    if not value.is_finite() or value < 0 or value > 100_000:
        raise ValidationFailedError("the monthly amount must be between 0 and 100,000")
    return value.quantize(Decimal("0.01"))


@router.get("/rehearsal", include_in_schema=False)
async def rehearsal_page(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    q = request.query_params
    error = None
    try:
        monthly = _monthly(q.get("monthly"))
    except ValidationFailedError as exc:
        monthly, error = Decimal(0), exc.message
    async with st.db.connect() as conn:
        b = await rehearsal.build(conn, st.clock.now(), monthly=monthly, rule=q.get("rule"))
    e_curve, e_months = expect_charts(b.chosen.e if b.chosen else None)
    return await render(
        request,
        "rehearsal.html",
        principal,
        s=b,
        e_curve=e_curve,
        e_months=e_months,
        error=error,
        floor=trading_rehearsal.FLOOR,
        start_money=trading_rehearsal.START,
        days=trading_rehearsal.DAYS,
        replay_slippage=measured.REPLAY_SLIPPAGE_BPS,
    )


@router.post("/rehearsal/start", include_in_schema=False)
async def rehearsal_start(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.APPROVE_TRADE)
    rule = str(form.get("rule", rehearsal.DEFAULT_RULE))
    venue = str(form.get("venue", "binance"))
    if venue not in trading_rehearsal.REHEARSALS:
        raise ValidationFailedError("choose Binance or HFM Cent")
    now = st.clock.now()
    async with st.db.transaction() as conn:
        account_id, opened = await trading_rehearsal.start(
            conn,
            principal=principal,
            broker=request.app.state.trading.paper,
            rule=rule,
            now=now,
            settings=st.settings,
            venue=venue,
        )
        if opened:  # reconciled at once, before its first signal
            await queue.enqueue(
                conn,
                kind="trading.reconcile",
                now=now,
                payload={"account_id": str(account_id), "trigger": "account_opened"},
                priority=queue.PRIORITY_SAFETY,
            )
    alias = trading_rehearsal.REHEARSALS[venue].alias
    return RedirectResponse(f"/trader?{urlencode({'account': alias})}", status_code=303)


FEE = re.compile(r"\d{1,3}(\.\d{1,2})?")  # bps a side: 10, 7.5


@router.post("/rehearsal/ready", include_in_schema=False)
async def rehearsal_ready(request: Request) -> Response:
    """One risk-policy change, worked out again from the policy the page showed (refused if it
    changed since): the form only says which advised limits the owner ticked and the Binance
    fee chosen."""
    st = state(request)
    principal, form = await authorize_form(request, Capability.CHANGE_RISK_LIMITS)
    raw = str(form.get("binance_fee") or "").strip()
    if raw and not FEE.fullmatch(raw):
        raise ValidationFailedError("the Binance fee must be a number of bps, like 10 or 7.5")
    bps = Decimal(raw) if raw else None
    try:
        shown = int(str(form.get("version", "")))
    except ValueError:
        raise ValidationFailedError("the page is out of date: open it again") from None
    ticked = {str(k) for k in form.getlist("advise")}
    async with st.db.transaction() as conn:
        version, policy = await active_policy(conn)
        if version != shown:
            raise ConflictError("the risk policy changed since this page was shown: look again")
        params, done = rehearsal.ready_params(
            policy,
            binance_bps=bps,
            advised=ticked,
            available=await rehearsal.tradable_rehearsals(conn),
        )
        if not done:
            raise ConflictError("nothing to change: both rehearsals are ready")
        await change_policy(
            conn,
            principal=principal,
            new_params=params,
            reason="Start with $100, getting the rehearsals ready: " + "; ".join(done),
            now=st.clock.now(),
            settings=st.settings,
            expected_version=shown,
        )
    return RedirectResponse("/rehearsal#ready-title", status_code=303)
