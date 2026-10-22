"""Worker jobs for the strategy library."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from cosmic.db.schema_market import bars, instruments
from cosmic.research.library import auto, compete_store, signals, store
from cosmic.sources.registry import source_retired
from cosmic.trading import mandates
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_BACKGROUND, PRIORITY_DEFAULT
from cosmic.worker.registry import Periodic, Registry

# Research and signal series → (source, days of history to keep). Bitstamp BTC/USD starts
# 2011-08-18, Binance BTC/USDT 2017-08-17 and Binance PAXG/USDT 2020-08-28; asking for more days
# than exist is harmless. Series of retired venues are kept as stored, never fetched (ADR-027).
HISTORY = {
    "SPOT:BTC/USD@BITSTAMP": ("bitstamp_public", 5_700),
    "SPOT:BTC/USDT@BINANCE": ("binance_public", 3_400),
    "SPOT:PAXG/USDT@BINANCE": ("binance_public", 2_400),
}
STALE_AFTER = timedelta(days=6)
# The auto-trader's signal series (ADR-030) keep about a year of hourly bars, so "What to
# expect" (expect.py) can replay its rule over the last 12 months. Fetched once when short;
# the hourly bar ingestion keeps them current. Hourly bars older than 400 days are refused
# on ingestion (MAX_BAR_AGE), hence 390.
HOURLY_DAYS = 390
HOURLY_ENOUGH = 24 * 370


async def stored_bars(ctx: WorkerContext, key: str, timeframe: str = "1d") -> int:
    async with ctx.db.connect() as conn:
        return int(
            await conn.scalar(
                select(func.count())
                .select_from(bars.join(instruments, instruments.c.id == bars.c.instrument_id))
                .where(instruments.c.canonical_key == key, bars.c.timeframe == timeframe)
            )
            or 0
        )


async def ensure_history(ctx: WorkerContext) -> dict[str, int]:
    """Backfill a research series once when it is far shorter than its available history."""
    from cosmic.market.jobs import _ingest

    out: dict[str, int] = {}
    retired = frozenset(ctx.settings.retired_venues)
    for key, (source, days) in HISTORY.items():
        have = await stored_bars(ctx, key)
        if have < store.MIN_BARS * 2 and not source_retired(source, retired):
            await _ingest(ctx).backfill_bars(source, "1d", days=days)
            have = await stored_bars(ctx, key)
        out[key] = have
    return out


async def ensure_hourly_history(ctx: WorkerContext) -> dict[str, int]:
    """Backfill the auto-trader's signal series to about a year of hourly bars, once."""
    from cosmic.market.jobs import _ingest

    out: dict[str, int] = {}
    retired = frozenset(ctx.settings.retired_venues)
    short: set[str] = set()
    for key in sorted(set(mandates.AUTO_SIGNAL_SERIES.values())):
        out[key] = await stored_bars(ctx, key, "1h")
        source = HISTORY[key][0] if key in HISTORY else None
        if out[key] < HOURLY_ENOUGH and source and not source_retired(source, retired):
            short.add(source)
    for source in sorted(short):  # one backfill per source covers all its series
        await _ingest(ctx).backfill_bars(source, "1h", days=HOURLY_DAYS, page_bars=1000)
    for key in out:
        out[key] = await stored_bars(ctx, key, "1h")
    return out


async def evaluate_if_due(ctx: WorkerContext, costs_name: str) -> dict[str, Any]:
    """Re-evaluate weekly, or at once when the library code changed."""
    now = ctx.clock.now()
    scenario = next(c for c in store.SCENARIOS if c.name == costs_name)
    async with ctx.db.connect() as conn:
        last = await store.latest_run(conn, costs_name)
    if (
        last is not None
        and last.code_version == store.CODE_VERSION
        and now - last.created_at < STALE_AFTER
    ):
        return {"status": "CURRENT", "run": str(last.id)}
    return await store.run_library(ctx.db, now, (scenario,))


def register(registry: Registry, ctx: WorkerContext) -> None:
    @registry.handler("research.library_history")
    async def history(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await ensure_history(ctx)

    @registry.handler("research.auto_history")
    async def hourly_history(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await ensure_hourly_history(ctx)

    @registry.handler("research.auto_competition")
    async def competition(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await compete_store.run_if_due(ctx.db, ctx.clock.now())

    @registry.handler("research.library_evaluate")
    async def evaluate(ctx: WorkerContext, p: dict[str, Any]) -> None:
        await evaluate_if_due(ctx, str(p["costs"]))

    @registry.handler("research.library_signals")
    async def apply(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await signals.run(ctx.db, ctx.clock)

    @registry.handler("research.auto_trader")
    async def auto_trade(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await auto.run(ctx.db, ctx.clock)

    def market_on(c: WorkerContext) -> bool:
        return c.settings.market_data_enabled

    registry.every(
        Periodic("research.library_history", 6 * 3600, PRIORITY_BACKGROUND, {}, market_on)
    )
    registry.every(Periodic("research.auto_history", 6 * 3600, PRIORITY_BACKGROUND, {}, market_on))
    # The rule competition (ADR-031): re-run once a day, or at once after a code change.
    registry.every(
        Periodic("research.auto_competition", 6 * 3600, PRIORITY_BACKGROUND, {}, market_on)
    )
    for scenario in store.SCENARIOS:
        registry.every(
            Periodic(
                "research.library_evaluate",
                6 * 3600,
                PRIORITY_BACKGROUND,
                {"costs": scenario.name},
                market_on,
            )
        )
    registry.every(Periodic("research.library_signals", 3600, PRIORITY_DEFAULT, {}, market_on))
    # The demo auto-trader (ADR-030): hourly bars complete on the hour; a 5-minute check acts
    # within minutes of each close. It does nothing without an active mandate.
    registry.every(Periodic("research.auto_trader", 300, PRIORITY_DEFAULT, {}, market_on))
