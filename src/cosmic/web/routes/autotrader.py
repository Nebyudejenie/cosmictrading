"""The auto-trader page (ADR-030): results in plain numbers, and starting it from money.

- ``GET /trader``: what the auto-trader did and is doing (refreshes itself every minute).
- ``GET /trader/setup``: how much money, how careful, automatic or ask first, with a plan of
  what that allows market by market (no side effects).
- ``POST /trader/start``: grant the mandate from that money (fresh authenticator code);
  replaces the account's current settings in one transaction.
- ``POST /trader/stop``: stop it (no code: stopping only removes authority).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.core.errors import ValidationFailedError
from cosmic.identity.capabilities import Capability
from cosmic.reporting import autotrader
from cosmic.trading import mandates
from cosmic.web.charts import bar_chart, line_chart
from cosmic.web.deps import authorize, authorize_form, state, verify_csrf
from cosmic.web.views import render

router = APIRouter()
MODES = {
    "AUTO": "Automatic — it trades by itself within your limits and tells you on Telegram",
    "ASK": "Ask me first — Telegram says TIME TO TRADE; it trades only when you approve",
}


def _money(raw: object, *, required: bool) -> Decimal | None:
    text = str(raw or "").replace(",", "").replace("$", "").strip()
    if not text:
        if required:
            raise ValidationFailedError("say how much money the auto-trader may use")
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValidationFailedError("the money must be a number, like 5000") from None
    if not value.is_finite() or value <= 0 or value > 1_000_000:
        raise ValidationFailedError("the money must be more than 0 and at most 1,000,000")
    return value.quantize(Decimal("0.01"))


@router.get("/trader", include_in_schema=False)
async def trader_page(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    now = st.clock.now()
    async with st.db.connect() as conn:
        board = await autotrader.build(conn, now)
    oldest_first = list(reversed(board.closed))
    curve = line_chart(
        board.curve_times,
        board.curve_values,
        label="Your result, trade after trade",
        empty="The result line appears after two closed trades.",
        css_class="chart chart-pnl",
    )
    bars = bar_chart(
        [float(t.pnl) for t in oldest_first],
        [f"{t.market} {t.code}" for t in oldest_first],
        label="Each trade's result",
    )
    return await render(
        request,
        "trader.html",
        principal,
        b=board,
        curve=curve,
        bars=bars,
        since=autotrader.since,
        modes=MODES,
        care_levels=mandates.CARE_LEVELS,
    )


@router.get("/trader/setup", include_in_schema=False)
async def trader_setup(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    q = request.query_params
    care = q.get("care", "NORMAL") if q.get("care") in mandates.CARE_LEVELS else "NORMAL"
    mode = q.get("mode", "AUTO") if q.get("mode") in MODES else "AUTO"
    error = None
    try:
        money = _money(q.get("money"), required=False)
    except ValidationFailedError as exc:
        money, error = None, exc.message
    async with st.db.connect() as conn:
        plan = await autotrader.plan(conn, st.clock.now(), money, care, mode)
        board = await autotrader.build(conn, st.clock.now())
    return await render(
        request,
        "trader_setup.html",
        principal,
        plan=plan,
        b=board,
        error=error,
        modes=MODES,
        care_levels=mandates.CARE_LEVELS,
    )


@router.post("/trader/start", include_in_schema=False)
async def trader_start(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.APPROVE_TRADE)
    money = _money(form.get("money"), required=True)
    assert money is not None
    care = str(form.get("care", "NORMAL"))
    mode = str(form.get("mode", "AUTO"))
    if mode not in MODES:
        raise ValidationFailedError("choose automatic or ask me first")
    now = st.clock.now()
    async with st.db.transaction() as conn:
        plan = await autotrader.plan(conn, now, money, care, mode)
        if plan.account is None:
            raise ValidationFailedError("no paper or demo account can run the auto-trader")
        chosen = tuple(str(k) for k in form.getlist("instruments"))
        allowed = {r.key for r in plan.rows}
        if not chosen or not set(chosen) <= allowed:
            raise ValidationFailedError("choose at least one market for the auto-trader")
        terms = mandates.terms_from_money(
            account_alias=plan.account.alias,
            money=money,
            care=care,
            mode=mode,  # type: ignore[arg-type]
            instruments=chosen,
        )
        await mandates.grant(
            conn, principal=principal, terms=terms, now=now, settings=st.settings, replace=True
        )
    return RedirectResponse("/trader", status_code=303)


@router.post("/trader/stop", include_in_schema=False)
async def trader_stop(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    form = await request.form()
    await verify_csrf(request, principal, form)
    now = st.clock.now()
    async with st.db.transaction() as conn:
        for m in await mandates.active_mandates(conn, now):
            await mandates.revoke(
                conn, principal=principal, mandate_id=m.id, reason="stopped by the owner", now=now
            )
    return RedirectResponse("/trader", status_code=303)
