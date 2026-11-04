"""The gold simulation's worker jobs (S62)."""

from __future__ import annotations

import asyncio
from typing import Any

from cosmic.core.logs import get_logger
from cosmic.gold import sim
from cosmic.market import hfm_candles
from cosmic.trading.brokers.base import BrokerUnavailableError
from cosmic.trading.brokers.mt5_bridge import HfmBridgeAdapter, hfm_symbols
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_BACKGROUND, PRIORITY_TRADING
from cosmic.worker.registry import Periodic, Registry

log = get_logger(__name__)
CHECKS = 5  # one a second while the scheduler's next run (5 s) comes
# HFM's own candles asked of MT5 each hour: (timeframe, how many)
HISTORY = (("1d", 1000), ("1h", 2000), ("15m", 1000), ("5m", 1000), ("1m", 1500))


def register(registry: Registry, ctx: WorkerContext) -> None:
    @registry.handler("gold.sim_tick")
    async def sim_tick(ctx: WorkerContext, payload: dict[str, Any]) -> None:
        """Every second: stops, targets and swap; a decision on each new 1-minute candle."""
        for i in range(CHECKS):
            now = ctx.clock.now()
            closed = await sim.watch(ctx.db, now)
            decided = await sim.decide(ctx.db, now)
            if closed or any(not d.endswith(":WAIT") for d in decided):
                log.info("gold_sim", closed=closed, decided=decided)
            if i < CHECKS - 1:
                await asyncio.sleep(1)

    @registry.handler("gold.candles_backfill")
    async def candles_backfill(ctx: WorkerContext, payload: dict[str, Any]) -> None:
        """HFM's candles rebuilt from its stored prices where none is stored (S61)."""
        async with ctx.db.transaction() as conn:
            added = await hfm_candles.backfill(conn, ctx.clock.now())
        log.info("hfm_candles_backfill", added=added)

    @registry.handler("gold.hfm_history")
    async def hfm_history(ctx: WorkerContext, payload: dict[str, Any]) -> None:
        """HFM's own candles from MT5 (bridge version 4), replacing the rebuilt ones."""
        adapter = HfmBridgeAdapter(ctx.db, ctx.clock, hfm_symbols())
        stored: dict[str, int] = {}
        try:
            offset = (await adapter.report()).get("server_utc_offset_ms")
            expected = hfm_candles.server_offset_ms(ctx.clock.now())
            if offset is None or abs(int(offset) - expected) > 60_000:
                # Not New York + 7 hours: MT5's times would be placed wrongly; store nothing.
                log.warning("hfm_history_clock", reported=offset, expected=expected)
                return
            for tf, count in HISTORY:
                asked = ctx.clock.now()  # a candle still forming then is not complete
                rows = await adapter.rates(
                    hfm_candles.SYMBOL, hfm_candles.MT5_TIMEFRAMES[tf], count
                )
                async with ctx.db.transaction() as conn:
                    stored[tf] = await hfm_candles.store_rates(conn, tf, rows, asked)
        except BrokerUnavailableError as exc:
            log.info("hfm_history_unavailable", reason=str(exc)[:200])
        log.info("hfm_history", stored=stored)

    registry.every(Periodic("gold.sim_tick", 5, PRIORITY_TRADING))
    registry.every(Periodic("gold.hfm_history", 3600, PRIORITY_BACKGROUND))
    registry.every(Periodic("gold.candles_backfill", 24 * 3600, PRIORITY_BACKGROUND))
