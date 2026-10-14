"""Strategy library pages: the evaluated rules and the owner's paper-proposal switches."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.core.errors import NotFoundError, ValidationFailedError
from cosmic.identity.capabilities import Capability
from cosmic.market import data as md
from cosmic.research.library import store
from cosmic.research.library.catalog import BY_KEY, SPECS
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.views import render

router = APIRouter()

SCENARIO_LABELS = {
    "binance_like": "Binance-like costs (0.10% fee + 0.06% spread and slippage, one way)",
    "coinbase_like": "Coinbase-like costs (0.60% fee + 0.06% spread and slippage, one way)",
}
SERIES_LABELS = {
    "SPOT:BTC/USD@BITSTAMP": "BTC — Bitstamp BTC/USD daily",
    "SPOT:PAXG/USDT@BINANCE": "Gold — Binance PAXG/USDT daily",
    store.PANEL_SERIES: "BTC and gold rotation",
}


async def _overview(conn: Any, costs_name: str) -> dict[str, Any]:
    run = await store.latest_run(conn, costs_name)
    if run is None:
        return {"run": None, "series": []}
    rows = await store.run_evaluations(conn, run.id)
    meta = {m["series"]: m for m in run.series}
    series = []
    for key in (*[a.research_key for a in store.ASSETS], store.PANEL_SERIES):
        evs = [r for r in rows if r.series == key]
        if not evs:
            continue
        tiers: dict[str, int] = {}
        for r in evs:
            tiers[r.tier] = tiers.get(r.tier, 0) + 1
        series.append(
            {
                "key": key,
                "label": SERIES_LABELS.get(key, key),
                "meta": meta.get(key, {}),
                "tiers": tiers,
                "rows": [
                    {"row": r, "spec": BY_KEY.get(r.strategy_key), "r": r.result} for r in evs
                ],
            }
        )
    return {"run": run, "series": series}


@router.get("/strategies/library", include_in_schema=False)
async def library_page(request: Request) -> Response:
    principal = await authorize(request, Capability.VIEW)
    costs = request.query_params.get("costs", "coinbase_like")
    if costs not in SCENARIO_LABELS:
        costs = "coinbase_like"
    async with state(request).db.connect() as conn:
        view = await _overview(conn, costs)
        on = await store.active(conn)
        instruments = {i.id: i.canonical_key for i in await md.list_instruments(conn)}
    return await render(
        request,
        "library.html",
        principal,
        view=view,
        costs=costs,
        scenarios=SCENARIO_LABELS,
        active=[
            {
                "key": r.strategy_key,
                "spec": BY_KEY.get(r.strategy_key),
                "instrument": instruments.get(r.instrument_id, "?"),
                "since": r.occurred_at,
                "reason": r.reason,
            }
            for r in on
        ],
        total=len(SPECS),
        max_active=store.MAX_ACTIVE,
    )


@router.get("/strategies/library/{key}", include_in_schema=False)
async def rule_page(request: Request, key: str) -> Response:
    principal = await authorize(request, Capability.VIEW)
    spec = BY_KEY.get(key)
    if spec is None:
        raise NotFoundError("unknown strategy")
    async with state(request).db.connect() as conn:
        results = []
        for costs in SCENARIO_LABELS:
            run = await store.latest_run(conn, costs)
            if run is None:
                continue
            for r in await store.run_evaluations(conn, run.id):
                if r.strategy_key == key:
                    results.append(
                        {
                            "costs": costs,
                            "label": SERIES_LABELS.get(r.series, r.series),
                            "row": r,
                            "r": r.result,
                            "run": run,
                        }
                    )
        switches = []
        latest = await store.activations(conn)
        for asset in store.ASSETS:
            for trade_key in asset.trade_keys:
                inst = await md.instrument_by_key(conn, trade_key)
                evaluation = await store.evaluation_for(conn, spec, trade_key)
                current = latest.get((spec.key, inst.id))
                switches.append(
                    {
                        "asset": asset.asset,
                        "instrument": trade_key,
                        "tier": evaluation.tier if evaluation is not None else None,
                        "on": current is not None and current.action == "ACTIVATE",
                        "since": current.occurred_at if current is not None else None,
                        "reason": current.reason if current is not None else None,
                    }
                )
    return await render(
        request,
        "library_rule.html",
        principal,
        spec=spec,
        results=results,
        switches=switches,
        activatable=store.ACTIVATABLE,
    )


@router.post("/strategies/library/{key}/switch", include_in_schema=False)
async def switch(request: Request, key: str) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    instrument = str(form.get("instrument", ""))
    on = str(form.get("on", "")) == "1"
    if instrument not in {k for a in store.ASSETS for k in a.trade_keys}:
        raise ValidationFailedError("choose one of the listed instruments")
    async with st.db.transaction() as conn:
        await store.set_active(
            conn,
            principal=principal,
            strategy_key=key,
            instrument_key=instrument,
            on=on,
            reason=str(form.get("reason", "")),
            acknowledged=str(form.get("acknowledge", "")) == "yes",
            settings=st.settings,
            now=st.clock.now(),
        )
    return RedirectResponse(f"/strategies/library/{key}", status_code=303)
