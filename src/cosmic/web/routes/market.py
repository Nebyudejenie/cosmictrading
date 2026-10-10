"""Sources/freshness and market pages (read-only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import desc, select

from cosmic.core.labels import DataLabel
from cosmic.db.schema_market import fetch_runs, sources
from cosmic.identity.capabilities import Capability
from cosmic.market import data as md
from cosmic.market import indicators as ind
from cosmic.sources.freshness import age_seconds, classify
from cosmic.web.charts import line_chart
from cosmic.web.deps import authorize, state
from cosmic.web.views import render

router = APIRouter()

COVERAGE_GAPS = (
    ("Equities / ETFs", "No keyed price source configured (capability map, doc 07 §8)"),
    ("FX reference rates, macro data, news", "Not in the first release; requires licence review"),
    ("Kraken, Binance", "Unreachable from the development network (B-09)"),
    ("Live broker account data", "No broker selected (B-06)"),
    ("Real-time licensed streaming", "None: all prices are polled public data, labelled DELAYED"),
)


@router.get("/sources", include_in_schema=False)
async def sources_page(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.READ_MARKET_DATA)
    now = st.clock.now()
    async with st.db.connect() as conn:
        rows = list(await conn.execute(select(sources).order_by(sources.c.key)))
        runs = list(
            await conn.execute(
                select(fetch_runs, sources.c.key.label("source_key"))
                .join(sources, sources.c.id == fetch_runs.c.source_id)
                .order_by(desc(fetch_runs.c.started_at))
                .limit(30)
            )
        )
    items: list[dict[str, Any]] = []
    for s in rows:
        items.append(
            {
                "row": s,
                "label": classify(
                    s.last_success_at,
                    now,
                    s.freshness_limit_seconds,
                    latency_class=s.latency_class,
                    enabled=s.enabled,
                ),
                "age": age_seconds(s.last_success_at, now),
            }
        )
    return await render(
        request, "sources.html", principal, items=items, runs=runs, gaps=COVERAGE_GAPS
    )


@router.get("/market", include_in_schema=False)
async def market_page(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.READ_MARKET_DATA)
    now = st.clock.now()
    async with st.db.connect() as conn:
        instruments = await md.list_instruments(conn)
        quotes = {i.id: await md.latest_quote(conn, i.id, now) for i in instruments}
        pairs = sorted({i.pair_key for i in instruments})
        deviations = [await md.cross_source_deviation(conn, p, now) for p in pairs]
        venues = {
            v: await md.latest_venue_status(conn, v, now)
            for v in sorted({i.venue_key for i in instruments})
        }
    return await render(
        request,
        "market.html",
        principal,
        instruments=instruments,
        quotes=quotes,
        deviations=deviations,
        venues=venues,
    )


@router.get("/market/{canonical_key:path}", include_in_schema=False)
async def instrument_page(request: Request, canonical_key: str) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.READ_MARKET_DATA)
    now = st.clock.now()
    async with st.db.connect() as conn:
        inst = await md.instrument_by_key(conn, canonical_key)
        quote = await md.latest_quote(conn, inst.id, now)
        bars = await md.complete_bars(conn, inst.id, "1h", limit=720)
    closes = [float(b.close) for b in bars]
    stats: dict[str, Any] = {}
    if len(bars) >= 50:
        stats["sma20"] = ind.sma(closes, 20)[-1]
        stats["sma50"] = ind.sma(closes, 50)[-1]
        stats["rsi14"] = ind.rsi(closes, 14)[-1]
        stats["atr14"] = ind.atr(
            [float(b.high) for b in bars], [float(b.low) for b in bars], closes, 14
        )[-1]
        stats["vol24h_annualised"] = ind.realized_volatility(closes[-25:], 8760)
        stats["max_drawdown"] = ind.max_drawdown(closes).max_drawdown
    recent = bars[-168:]
    chart = line_chart(
        [b.open_time for b in recent],
        [float(b.close) for b in recent],
        label=f"{inst.canonical_key} hourly closes (completed bars)",
    )
    return await render(
        request,
        "instrument.html",
        principal,
        inst=inst,
        quote=quote,
        bars=bars,
        stats=stats,
        chart=chart,
        label_stale=DataLabel.STALE,
    )
