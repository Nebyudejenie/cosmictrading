"""The auto-trader page (ADR-030): results in plain numbers, and starting it from money.

- ``GET /trader?account=…``: what one account's auto-trader did and is doing (refreshes itself
  every minute), with a price radar per market and what to expect from its settings; a
  switcher when several accounts run one.
- ``GET /trader/setup``: which account, which rule on which bars (hourly or daily), how much
  money, how careful, automatic or ask first, with a plan of what that allows market by market
  and the rule replayed with that plan (no side effects).
- ``POST /trader/start``: grant the mandate from that money on that account (fresh
  authenticator code); replaces only that account's current settings, in one transaction.
- ``POST /trader/stop``: stop one account's auto-trader, or all of them (no code: stopping
  only removes authority).
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
from cosmic.research.library import auto, compete, compete_store
from cosmic.research.library.catalog import BY_KEY
from cosmic.trading import mandates
from cosmic.trading.accounts import get_account
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


def radar_chart(w: autotrader.Watch, timeframe: str = mandates.HOURLY) -> Markup | None:
    """A market's last bars with where the auto-trader buys, or, in a trade, where it sells and
    its stop (breakout rules)."""
    r = w.chart
    if r is None:
        return None
    n = len(r.closes)
    unit = "hour" if timeframe == mandates.HOURLY else "day"
    when = "%d %b %H:%M UTC" if timeframe == mandates.HOURLY else "%d %b %Y"
    if r.in_trade:
        word, levels = "sells below", r.sell
        guides = [
            Guide(
                r.sell,
                r.next_sell,
                "sell",
                f"sells below {r.next_sell:,.2f}",
                f"sells: close below the {r.exit_bars}-{unit} low",
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
                f"buys: close above the {r.entry_bars}-{unit} high",
            )
        ]
    tips = [
        f"{t:{when}} · close {c:,.2f}" + (f" · {word} {v:,.2f}" if v is not None else "")
        for t, c, v in zip(r.times, r.closes, levels, strict=True)
    ]
    bought = f"bought {r.entry_price:,.2f}" if r.entry_price is not None else "bought"
    return price_chart(
        r.times,
        r.closes,
        guides,
        series=f"{r.series}, {'hourly' if unit == 'hour' else 'daily'} close",
        marker=r.entry_index if r.in_trade else None,
        marker_text=bought,
        tips=tips,
        label=f"{w.market}: the last {n} {unit}s of {r.series} and where the auto-trader acts",
    )


def expect_charts(e: autotrader.Expectation | None) -> tuple[Markup | None, Markup | None]:
    """The past test's result line (trade after trade) and its months."""
    if e is None or e.summary is None:
        return None, None
    s = e.summary
    curve = line_chart(
        [t for t, _ in s.curve],
        [v for _, v in s.curve],
        label="Past test: the result, trade after trade",
        empty="Fewer than two trades: no line to draw.",
        css_class="chart chart-pnl",
        baseline=0.0,
        end_label=True,
        time_format="%d %b %Y",
    )
    months = bar_chart(
        [v for _, v in s.months],
        [f"{d:%b %Y}" for d, _ in s.months],
        label="Past test: the result of each month",
        empty="No months to show.",
    )
    return curve, months


def setup_href(board: autotrader.Board) -> str:
    """The setup page for this board's account, prefilled with its mandate when it has one."""
    if board.account is not None and board.account.alias.startswith(mandates.REHEARSAL_PREFIX):
        return "/rehearsal"  # renewed only there, keeping its floor
    m = board.mandate or board.ended
    query: dict[str, object] = {"account": board.account.alias} if board.account else {}
    if m is not None and m.capital is not None:
        query |= {
            "money": f"{m.capital:.2f}",
            "care": autotrader.care_of(m.risk_pct) or "NORMAL",
            "mode": m.mode,
        }
    if m is not None:
        query |= {
            "timeframe": m.timeframe,
            "rule": m.strategy_key,
            "instruments": list(m.instruments),  # the test of exactly these markets
        }
    return "/trader/setup" + ("?" + urlencode(query, doseq=True) if query else "")


@router.get("/trader", include_in_schema=False)
async def trader_page(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    now = st.clock.now()
    async with st.db.connect() as conn:
        board = await autotrader.build(conn, now, request.query_params.get("account"))
        expectation = await autotrader.mandate_expectation(conn, now, board)
        run = await compete_store.latest_run(conn)
        running = await autotrader.running(conn, now)
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
    rule_key = board.mandate.strategy_key if board.mandate else next(iter(mandates.AUTO_RULES))
    return await render(
        request,
        "trader.html",
        principal,
        b=board,
        curve=curve,
        bars=bars,
        radar={w.key: radar_chart(w, board.timeframe) for w in board.watch},
        e=expectation,
        e_curve=e_curve,
        e_months=e_months,
        more_href=setup_href(board),
        running=running,
        halve_after=mandates.HALVE_AFTER,
        restore_below=mandates.RESTORE_BELOW,
        competition=compete_store.verdict_line(run, rule_key, board.timeframe),
        since=autotrader.since,
        modes=MODES,
        care_levels=mandates.CARE_LEVELS,
        is_breakout=auto.is_breakout(BY_KEY[rule_key]) if rule_key in BY_KEY else False,
    )


@router.get("/trader/competition", include_in_schema=False)
async def trader_competition(request: Request) -> Response:
    """The rule competition (ADR-031): which published rule, if any, has earned the
    auto-trader, judged on data it never saw and then forward."""
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    async with st.db.connect() as conn:
        run = await compete_store.latest_run(conn)
        board = await autotrader.build(conn, st.clock.now(), request.query_params.get("account"))
    rule = board.mandate.strategy_key if board.mandate else next(iter(mandates.AUTO_RULES))
    curves = {}
    forward = {}
    if run is not None:
        for e in run.entries:
            k = (e["rule"], e["market"], e["timeframe"])
            if e["tier"] != "N/A" and e["curve"]:
                curves[k] = spark(
                    e["curve"],
                    split=e["split_trade"],
                    label=f"{e['name']}: R after each trade, development then validation",
                )
            if e.get("forward_curve"):
                forward[k] = spark(
                    e["forward_curve"], label=f"{e['name']}: R after each forward trade"
                )
    return await render(
        request,
        "trader_competition.html",
        principal,
        run=run,
        b=board,
        rule=rule,
        mine=run.find(rule, board.timeframe) if run is not None else [],
        curves=curves,
        forward=forward,
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
    tf = q.get("timeframe", "") if q.get("timeframe") in mandates.TIMEFRAMES else mandates.HOURLY
    error = None
    try:
        money = _money(q.get("money"), required=False)
    except ValidationFailedError as exc:
        money, error = None, exc.message
    chosen = [str(k) for k in q.getlist("instruments")]
    now = st.clock.now()
    async with st.db.connect() as conn:
        plan = await autotrader.plan(
            conn, now, money, care, mode, account=q.get("account"), timeframe=tf, rule=q.get("rule")
        )
        board = await autotrader.build(conn, now, plan.account.alias if plan.account else None)
        chosen = [k for k in chosen if k in {r.key for r in plan.rows}]
        expectation = await autotrader.plan_expectation(conn, now, plan, chosen)
        accounts = (await mandates.view(conn, now))["choices"]
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
        chosen=chosen,
        error=error,
        modes=MODES,
        care_levels=mandates.CARE_LEVELS,
        accounts=accounts,
        timeframes=mandates.TIMEFRAMES,
        rules=[(k, mandates.AUTO_RULES[k]) for k in mandates.RULES_BY_TIMEFRAME[plan.timeframe]],
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
    tf = str(form.get("timeframe", mandates.HOURLY))
    if tf not in mandates.TIMEFRAMES:
        raise ValidationFailedError("choose hourly or daily bars")
    rule = str(form.get("rule", mandates.RULES_BY_TIMEFRAME[tf][0]))
    if rule not in mandates.RULES_BY_TIMEFRAME[tf]:
        raise ValidationFailedError("that rule does not run on those bars")
    alias = str(form.get("account", "")) or None
    now = st.clock.now()
    async with st.db.transaction() as conn:
        plan = await autotrader.plan(
            conn, now, money, care, mode, account=alias, timeframe=tf, rule=rule
        )
        if plan.account is None or (alias is not None and plan.account.alias != alias):
            raise ValidationFailedError("that paper or demo account cannot run the auto-trader")
        if plan.account.alias.startswith(mandates.REHEARSAL_PREFIX):
            raise ValidationFailedError(
                "renew the $100 rehearsal from Start with $100: it keeps its floor at 80.00"
            )
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
            strategy_key=plan.rule,
            timeframe=plan.timeframe,  # type: ignore[arg-type]
        )
        await mandates.grant(
            conn, principal=principal, terms=terms, now=now, settings=st.settings, replace=True
        )
    return RedirectResponse(f"/trader?{urlencode({'account': plan.account.alias})}", 303)


@router.post("/trader/stop", include_in_schema=False)
async def trader_stop(request: Request) -> Response:
    """Stop one account's auto-trader (``account``), or every one (``all``)."""
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    form = await request.form()
    await verify_csrf(request, principal, form)
    now = st.clock.now()
    alias = str(form.get("account", ""))
    everything = form.get("all") == "1"
    async with st.db.transaction() as conn:
        account_id = None
        if not everything:
            if not alias:
                raise ValidationFailedError("say which auto-trader to stop")
            account_id = (await get_account(conn, alias=alias)).id
        for m in await mandates.active_mandates(conn, now):
            if everything or m.account_id == account_id:
                await mandates.revoke(
                    conn,
                    principal=principal,
                    mandate_id=m.id,
                    reason="stopped by the owner",
                    now=now,
                )
    target = "/trader" if everything else f"/trader?{urlencode({'account': alias})}"
    return RedirectResponse(target, status_code=303)
