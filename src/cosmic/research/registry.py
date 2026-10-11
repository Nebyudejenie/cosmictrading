"""Strategy registry and stored, reproducible backtests (with data vintage)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import canonical_hash_lenient, to_jsonable_lenient
from cosmic.db.schema_market import bars as bars_t
from cosmic.db.schema_research import backtest_runs, strategies, strategy_versions
from cosmic.market import data as md
from cosmic.research.backtest import CostModel, run_backtest, walk_forward
from cosmic.research.strategy import Strategy, TrendSmaAtr, version_hash

DESCRIPTIONS = {
    "trend_sma_atr": (
        "Long when SMA(fast) crosses above SMA(slow) outside high-volatility regimes; "
        "ATR stop and target. Transparent pipeline exerciser, not a claim of edge."
    ),
}


async def ensure_strategy(conn: AsyncConnection, strategy: Strategy, now: datetime) -> uuid.UUID:
    """Register the strategy and this exact parameter version; returns the version ID."""
    await conn.execute(
        pg_insert(strategies)
        .values(
            id=uuid7(),
            key=strategy.key,
            name=strategy.key.replace("_", " "),
            description=DESCRIPTIONS.get(strategy.key, strategy.key),
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[strategies.c.key])
    )
    sid = (
        await conn.execute(select(strategies.c.id).where(strategies.c.key == strategy.key))
    ).scalar_one()
    digest = version_hash(strategy)
    existing = (
        await conn.execute(select(strategy_versions.c.id).where(strategy_versions.c.hash == digest))
    ).first()
    if existing is not None:
        return uuid.UUID(str(existing.id))
    n = int(
        await conn.scalar(
            select(func.count())
            .select_from(strategy_versions)
            .where(strategy_versions.c.strategy_id == sid)
        )
        or 0
    )
    vid = uuid7()
    await conn.execute(
        strategy_versions.insert().values(
            id=vid,
            strategy_id=sid,
            version=n + 1,
            params=to_jsonable_lenient(strategy.params),
            code_ref=f"cosmic.research.strategy:{type(strategy).__name__}",
            hash=digest,
            status="RESEARCH",
            created_at=now,
        )
    )
    return vid


async def run_and_store(
    conn: AsyncConnection,
    *,
    strategy: Strategy,
    instrument_id: uuid.UUID,
    now: datetime,
    limit: int = 2000,
    costs: CostModel | None = None,
    walk: tuple[int, int] | None = (720, 168),
) -> dict[str, Any]:
    version_id = await ensure_strategy(conn, strategy, now)
    history = await md.complete_bars(conn, instrument_id, "1h", limit=limit)
    if len(history) < strategy.warmup() + 2:
        return {"status": "INSUFFICIENT_DATA", "bars": len(history)}
    vintage = (
        await conn.execute(
            select(
                func.count(func.distinct(bars_t.c.fetch_run_id)).label("runs"),
                func.max(bars_t.c.ingested_at).label("latest"),
            ).where(bars_t.c.instrument_id == instrument_id, bars_t.c.timeframe == "1h")
        )
    ).one()
    costs = costs or CostModel()
    result = run_backtest(history, strategy, costs=costs)
    summary = result.summary()
    out: dict[str, Any] = {"backtest": summary}
    trials = 1
    if walk is not None:
        grid = [
            {"fast": f, "slow": s, "atr_n": 14, "stop_k": 2.0, "tp_m": 3.0, "skip_high_vol": True}
            for f, s in ((10, 30), (20, 50), (30, 90))
        ]
        wf = walk_forward(
            history, lambda g: TrendSmaAtr(params=g), grid, train=walk[0], test=walk[1], costs=costs
        )
        trials += wf.trials
        out["walk_forward"] = {
            "windows": wf.windows,
            "trials": wf.trials,
            "out_of_sample_return": wf.out_of_sample_return,
            "notes": wf.notes,
        }
    data_vintage = {
        "fetch_runs": vintage.runs,
        "latest_ingested_at": vintage.latest,
        "source": "coinbase 1h",
        "complete_bars_only": True,
    }
    run_id = uuid7()
    await conn.execute(
        backtest_runs.insert().values(
            id=run_id,
            strategy_version_id=version_id,
            instrument_id=instrument_id,
            kind="WALK_FORWARD" if walk is not None else "BACKTEST",
            timeframe="1h",
            data_from=history[0].open_time,
            data_to=history[-1].open_time,
            bars=len(history),
            data_vintage=to_jsonable_lenient(data_vintage),
            assumptions=summary["assumptions"],
            trials=trials,
            result=to_jsonable_lenient(out),
            result_hash=canonical_hash_lenient(out),
            created_at=now,
        )
    )
    out["run_id"] = str(run_id)
    out["strategy_version_id"] = str(version_id)
    return out
