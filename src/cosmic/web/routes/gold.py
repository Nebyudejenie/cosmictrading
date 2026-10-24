"""The Gold page (ADR-032): price, trend, what a trade needs, why the rule lost or won,
gold in the rule competition, and the long-run benchmark — read-only."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from markupsafe import Markup

from cosmic.identity.capabilities import Capability
from cosmic.market.indicators import sma
from cosmic.reporting import autotrader, gold
from cosmic.research.library import compete
from cosmic.trading import mandates
from cosmic.web.charts import Guide, bar_chart, price_chart, spark
from cosmic.web.deps import authorize, state
from cosmic.web.routes.autotrader import radar_chart
from cosmic.web.views import render

router = APIRouter()
YEAR = 365


def year_chart(g: gold.GoldBoard) -> Markup | None:
    """The last year of daily closes with the 50- and 200-day averages."""
    if g.daily is None or len(g.daily) < 60:
        return None
    m = g.daily
    k = min(YEAR, len(m))
    closes = list(m.close)
    fast = sma(closes, 50)[-k:]
    slow = sma(closes, 200)[-k:]
    guides = []
    if fast[-1] is not None:
        guides.append(
            Guide(fast, fast[-1], "fast", f"50-day {fast[-1]:,.0f}", "50-day average", step=False)
        )
    if slow[-1] is not None:
        guides.append(
            Guide(slow, slow[-1], "slow", f"200-day {slow[-1]:,.0f}", "200-day average", step=False)
        )
    times = list(m.dates[-k:])
    tips = [f"{t:%d %b %Y} · close {c:,.2f}" for t, c in zip(times, closes[-k:], strict=True)]
    return price_chart(
        times,  # type: ignore[arg-type]
        closes[-k:],
        guides,
        series=f"{autotrader.series_name(g.signal or '')}, daily close",
        tips=tips,
        label="Gold over the last year with its 50- and 200-day averages",
        width=720,
        height=260,
        time_format="%d %b %Y",
    )


def benchmark_chart(b: gold.Benchmark) -> Markup:
    acc = b.acc
    times = [t for t, _, _ in acc.curve]
    put_in = [p for _, p, _ in acc.curve]
    values = [v for _, _, v in acc.curve]
    guide = Guide(put_in, put_in[-1], "ref", f"put in {put_in[-1]:,.0f}", "money put in")
    return price_chart(
        times,
        values,
        [guide],
        series=f"value of the gold bought (worth {values[-1]:,.0f} at the end)",
        label=f"Buying {gold.MONTHLY:,.0f} of gold every month for {b.years} year(s)",
        width=720,
        height=240,
        time_format="%b %Y",
    )


@router.get("/gold", include_in_schema=False)
async def gold_page(request: Request) -> Response:
    principal = await authorize(request, Capability.READ_ACCOUNT)
    st = state(request)
    now = st.clock.now()
    async with st.db.connect() as conn:
        g = await gold.build(conn, now)
        board = await autotrader.board_for(conn, now, g.traded or "")
    watch = autotrader.Watch(
        "Gold",
        g.traded or "",
        g.price,
        None,
        None,
        None,
        0.0,
        "WATCHING",
        None,
        None,
        None,
        g.chart,
    )
    d = g.diagnosis
    sessions = (
        bar_chart(
            [b.total_r for b in d.sessions],
            [f"{b.name} session, {b.trades} trades" for b in d.sessions],
            label="The rule's result in R by the session a trade was opened in",
            height=160,
        )
        if d is not None and d.trades
        else None
    )
    curves = {
        (e["rule"], e["timeframe"]): spark(
            e["curve"], split=e["split_trade"], label=f"{e['name']}: R after each trade"
        )
        for e in g.competition
        if e["curve"]
    }
    longest = g.benchmarks[-1] if g.benchmarks else None
    return await render(
        request,
        "gold.html",
        principal,
        g=g,
        b=board,
        year=year_chart(g),
        radar=radar_chart(watch) if g.chart is not None else None,
        sessions=sessions,
        curves=curves,
        benchmark=benchmark_chart(longest) if longest is not None else None,
        longest=longest,
        care_levels=mandates.CARE_LEVELS,
        forward_from=compete.FORWARD_FROM,
        r_ref=gold.R,
    )
