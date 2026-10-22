"""The auto-trader page (ADR-030): results in plain numbers, and starting it from money.

- ``GET /trader``: what the auto-trader did and is doing (refreshes itself every minute), with
  a price radar per market and what to expect from its settings.
- ``GET /trader/setup``: how much money, how careful, automatic or ask first, with a plan of
  what that allows market by market and the rule replayed on the last 12 months with that
  plan (no side effects).
- ``POST /trader/start``: grant the mandate from that money (fresh authenticator code);
  replaces the account's current settings in one transaction.
- ``POST /trader/stop``: stop it (no code: stopping only removes authority).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from markupsafe import Markup

from cosmic.core.errors import ValidationFailedError
from cosmic.identity.capabilities import Capability
from cosmic.reporting import autotrader
from cosmic.research.library import compete, compete_store
from cosmic.trading import mandates
from cosmic.web.charts import Guide, bar_chart, line_chart, price_chart, spark
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


def radar_chart(w: autotrader.Watch) -> Markup | None:
    """A market's last two days with where the auto-trader buys, or, in a trade, where it
    sells and its stop."""
    r = w.chart
    if r is None:
        return None
    n = len(r.closes)
    if r.in_trade:
        word, levels = "sells below", r.sell
        guides = [
            Guide(
                r.sell,
                r.next_sell,
                "sell",
                f"sells below {r.next_sell:,.2f}",
                "sells: close below the 10-hour low",
            )
        ]
        if r.stop is not None and r.stop_price is not None:
            stops: list[float | None] = [r.stop] * n
            guides.append(Guide(stops, r.stop, "stop", f"stop {r.stop_price:,.2f}", "stop-loss"))
    else:
        word, levels = "buys above", r.buy
        guides = [
            Guide(
                r.buy,
                r.next_buy,
                "buy",
                f"buys above {r.next_buy:,.2f}",
                "buys: close above the 20-hour high",
            )
        ]
    tips = [
        f"{t:%d %b %H:%M} UTC · close {c:,.2f}" + (f" · {word} {v:,.2f}" if v is not None else "")
        for t, c, v in zip(r.times, r.closes, levels, strict=True)
    ]
    bought = f"bought {r.entry_price:,.2f}" if r.entry_price is not None else "bought"
    return price_chart(
        r.times,
        r.closes,
        guides,
        series=f"{r.series}, hourly close",
        marker=r.entry_index if r.in_trade else None,
        marker_text=bought,
        tips=tips,
        label=f"{w.market}: the last {n} hours of {r.series} and where the auto-trader acts",
    )


def expect_charts(e: autotrader.Expectation | None) -> tuple[Markup | None, Markup | None]:
    """The past-year test's result line (trade after trade) and its months."""
    if e is None or e.summary is None:
        return None, None
    s = e.summary
    curve = line_chart(
        [t for t, _ in s.curve],
        [v for _, v in s.curve],
        label="Past-year test: the result, trade after trade",
        empty="Fewer than two trades: no line to draw.",
        css_class="chart chart-pnl",
        baseline=0.0,
        end_label=True,
        time_format="%d %b %Y",
    )
    months = bar_chart(
        [v for _, v in s.months],
        [f"{d:%b %Y}" for d, _ in s.months],
        label="Past-year test: the result of each month",
        empty="No months to show.",
    )
    return curve, months


@router.get("/trader", include_in_schema=False)
async def trader_page(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    now = st.clock.now()
    async with st.db.connect() as conn:
        board = await autotrader.build(conn, now)
        expectation = await autotrader.mandate_expectation(conn, now, board)
        run = await compete_store.latest_run(conn)
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
    e_curve, e_months = expect_charts(expectation)
    m = board.mandate
    more = (
        "/trader/setup?"
        + urlencode({"money": f"{m.capital:.2f}", "care": board.care or "NORMAL", "mode": m.mode})
        if m is not None and m.capital is not None
        else "/trader/setup"
    )
    return await render(
        request,
        "trader.html",
        principal,
        b=board,
        curve=curve,
        bars=bars,
        radar={w.key: radar_chart(w) for w in board.watch},
        e=expectation,
        e_curve=e_curve,
        e_months=e_months,
        more_href=more,
        competition=compete_store.verdict_line(
            run, board.mandate.strategy_key if board.mandate else next(iter(mandates.AUTO_RULES))
        ),
        since=autotrader.since,
        modes=MODES,
        care_levels=mandates.CARE_LEVELS,
    )


@router.get("/trader/competition", include_in_schema=False)
async def trader_competition(request: Request) -> Response:
    """The rule competition (ADR-031): which published rule, if any, has earned the
    auto-trader, judged on data it never saw and then forward."""
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    async with st.db.connect() as conn:
        run = await compete_store.latest_run(conn)
        board = await autotrader.build(conn, st.clock.now())
    rule = board.mandate.strategy_key if board.mandate else next(iter(mandates.AUTO_RULES))
    curves = {}
    if run is not None:
        for e in run.entries:
            if e["tier"] != "N/A" and e["curve"]:
                curves[(e["rule"], e["market"], e["timeframe"])] = spark(
                    e["curve"],
                    split=e["split_trade"],
                    label=f"{e['name']}: R after each trade, development then validation",
                )
    return await render(
        request,
        "trader_competition.html",
        principal,
        run=run,
        b=board,
        rule=rule,
        mine=run.find(rule, "1h") if run is not None else [],
        curves=curves,
        protocol={
            "dev_pct": round(compete.DEV_FRACTION * 100),
            "dev_t": compete.DEV_T,
            "min_trades": compete.MIN_TRADES,
            "forward_trades": compete.FORWARD_TRADES,
            "forward_from": compete.FORWARD_FROM,
            "warm_up": compete.WARM_UP_BARS,
        },
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
        expectation = await autotrader.plan_expectation(conn, st.clock.now(), plan)
    e_curve, e_months = expect_charts(expectation)
    return await render(
        request,
        "trader_setup.html",
        principal,
        plan=plan,
        b=board,
        e=expectation,
        e_curve=e_curve,
        e_months=e_months,
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
