"""Start with $100 (the $100 Gold Plan, S37).

- ``GET /rehearsal``: where $100 can trade gold, what history says, what a year could bring,
  how money grows, what must be ready first, and the rehearsal once it runs.
- ``POST /rehearsal/start``: open the $100 rehearsal account (the first time) and start its
  daily auto-trader on Binance gold, with the owner's code; or renew it from its money now.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.core.errors import ValidationFailedError
from cosmic.identity.capabilities import Capability
from cosmic.reporting import rehearsal
from cosmic.trading import rehearsal as trading_rehearsal
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
        alias=trading_rehearsal.ALIAS,
        floor=trading_rehearsal.FLOOR,
        start_money=trading_rehearsal.START,
        days=trading_rehearsal.DAYS,
    )


@router.post("/rehearsal/start", include_in_schema=False)
async def rehearsal_start(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.APPROVE_TRADE)
    rule = str(form.get("rule", rehearsal.DEFAULT_RULE))
    now = st.clock.now()
    async with st.db.transaction() as conn:
        account_id, opened = await trading_rehearsal.start(
            conn,
            principal=principal,
            broker=request.app.state.trading.paper,
            rule=rule,
            now=now,
            settings=st.settings,
        )
        if opened:  # reconciled at once, before its first signal
            await queue.enqueue(
                conn,
                kind="trading.reconcile",
                now=now,
                payload={"account_id": str(account_id), "trigger": "account_opened"},
                priority=queue.PRIORITY_SAFETY,
            )
    return RedirectResponse(
        f"/trader?{urlencode({'account': trading_rehearsal.ALIAS})}", status_code=303
    )
