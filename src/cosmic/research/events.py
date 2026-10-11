"""Deterministic material-change detection (DATA-08) on completed hourly bars."""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable_lenient
from cosmic.db.schema_market import market_events
from cosmic.market import data as md
from cosmic.market.regime import classify
from cosmic.research.strategy import Strategy

SIGMA = 3.0
VOLUME_MULT = 3.0
DIVERGENCE_BPS = Decimal(50)


def detect(bars: list[md.Bar], strategy: Strategy) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    if len(bars) < 170:
        return out
    closes = [float(b.close) for b in bars]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    trailing = rets[-169:-1]
    sd = statistics.stdev(trailing)
    if sd > 0 and abs(rets[-1]) > SIGMA * sd:
        out.append(("RETURN_SPIKE", {"log_return": rets[-1], "sigma": sd}))
    vols = [float(b.volume) for b in bars[-169:-1]]
    med = statistics.median(vols)
    if med > 0 and float(bars[-1].volume) > VOLUME_MULT * med:
        out.append(("VOLUME_SPIKE", {"volume": float(bars[-1].volume), "median": med}))
    if len(bars) >= 201:
        now_r, prev_r = classify(closes), classify(closes[:-1])
        if now_r.label != prev_r.label:
            out.append(("REGIME_CHANGE", {"from": prev_r.label, "to": now_r.label}))
    sig = strategy.signal(bars)
    if sig.action in ("ENTER_LONG", "EXIT"):
        out.append(
            (
                "SIGNAL",
                {
                    "action": sig.action,
                    "reason": sig.reason,
                    "stop": sig.stop,
                    "take_profit": sig.take_profit,
                    "strategy": strategy.key,
                    **sig.detail,
                },
            )
        )
    return out


async def detect_and_store(
    conn: AsyncConnection, *, instrument: md.InstrumentView, strategy: Strategy, now: datetime
) -> list[str]:
    bars = await md.complete_bars(conn, instrument.id, "1h", limit=800)
    found = detect(bars, strategy)
    dev = await md.cross_source_deviation(conn, instrument.pair_key, now)
    if dev.bps is not None and dev.bps > DIVERGENCE_BPS:
        found.append(("SOURCE_DIVERGENCE", {"bps": dev.bps}))
    stored: list[str] = []
    bar_time = bars[-1].open_time if bars else None
    for kind, detail in found:
        stamp = bar_time.isoformat() if bar_time else now.isoformat()
        key = f"{kind}:{instrument.canonical_key}:{stamp}"
        result = await conn.execute(
            pg_insert(market_events)
            .values(
                id=uuid7(),
                instrument_id=instrument.id,
                kind=kind,
                detected_at=now,
                bar_time=bar_time,
                detail=to_jsonable_lenient(detail),
                dedup_key=key,
                status="NEW",
            )
            .on_conflict_do_nothing(index_elements=[market_events.c.dedup_key])
        )
        if result.rowcount:
            stored.append(kind)
    return stored
