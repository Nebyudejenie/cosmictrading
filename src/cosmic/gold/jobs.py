"""The gold simulation's worker jobs (S62)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from cosmic.core.logs import get_logger
from cosmic.gold import hfm_demo, hfm_selftest, sim
from cosmic.market import hfm_candles
from cosmic.trading.brokers.base import BrokerUnavailableError
from cosmic.trading.brokers.mt5_bridge import (
    HISTORY_VERSION,
    HfmBridgeAdapter,
    bridge_version,
    hfm_symbols,
)
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_BACKGROUND, PRIORITY_TRADING
from cosmic.worker.registry import Periodic, Registry

log = get_logger(__name__)
CHECKS = 5  # one a second while the scheduler's next run (5 s) comes
# HFM's own candles asked of MT5 each hour: (timeframe, how many)
HISTORY = (("1d", 1000), ("1h", 2000), ("15m", 1000), ("5m", 1000), ("1m", 1500))
# The test lab's history (S73): HFM's older candles, read from MT5 a page at a time (bridge
# version 6), back to DEEP_FROM; read-only, in the background, one page per timeframe a run.
DEEP_FROM = datetime(2022, 11, 1, tzinfo=UTC)
DEEP_PAGE = 2000
DEEP = ("1d", "1h", "15m", "5m", "1m")
_deep_done: set[str] = set()  # timeframes MT5 holds nothing older of (this worker's run)


async def deep_history(adapter: HfmBridgeAdapter, ctx: WorkerContext) -> dict[str, int]:
    """One page of older candles per timeframe not yet back to ``DEEP_FROM``: the 2,000
    candles before the oldest stored one. A timeframe MT5 holds nothing older of is done."""
    stored: dict[str, int] = {}
    snap = await adapter.report()
    if bridge_version(snap) < HISTORY_VERSION:
        return stored
    offset = snap.get("server_utc_offset_ms")
    if offset is None or abs(int(offset) - hfm_candles.server_offset_ms(ctx.clock.now())) > 60_000:
        return stored  # not New York + 7 hours: MT5's times would be placed wrongly
    for tf in DEEP:
        if tf in _deep_done:
            continue
        async with ctx.db.connect() as conn:
            first = await hfm_candles.earliest(conn, tf)
        if first is None:
            continue  # the hourly job stores the latest page first
        if first <= DEEP_FROM:
            _deep_done.add(tf)
            continue
        asked = ctx.clock.now()
        rows = await adapter.rates(
            hfm_candles.SYMBOL,
            hfm_candles.MT5_TIMEFRAMES[tf],
            DEEP_PAGE,
            before=hfm_candles.utc_to_server(first) - 1,
        )
        async with ctx.db.transaction() as conn:
            stored[tf] = await hfm_candles.store_rates(conn, tf, rows, asked)
            now_first = await hfm_candles.earliest(conn, tf)
        if not rows or now_first is None or now_first >= first:
            _deep_done.add(tf)  # MT5 holds nothing older
    return stored


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
            try:  # the HFM demo's decision never holds back the simulation's stops (S64)
                tested = await hfm_selftest.advance(ctx.db, now)  # the owner's self-test (S67)
                if tested:
                    log.info("gold_hfm_selftest", steps=tested)
                hfm = await hfm_demo.decide(ctx.db, now)
            except Exception as exc:
                log.error("gold_hfm_failed", error=type(exc).__name__, detail=str(exc)[:200])
            else:
                if any(not d.endswith(":WAIT") for d in hfm):
                    log.info("gold_hfm", decided=hfm)
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

    @registry.handler("gold.hfm_deep_history")
    async def hfm_deep_history(ctx: WorkerContext, payload: dict[str, Any]) -> None:
        """HFM's older candles for the test lab (S73), a page per timeframe a run."""
        if len(_deep_done) == len(DEEP):
            return
        adapter = HfmBridgeAdapter(ctx.db, ctx.clock, hfm_symbols())
        try:
            stored = await deep_history(adapter, ctx)
        except BrokerUnavailableError as exc:
            log.info("hfm_deep_history_unavailable", reason=str(exc)[:200])
            return
        if stored:
            log.info("hfm_deep_history", stored=stored, done=sorted(_deep_done))

    registry.every(Periodic("gold.sim_tick", 5, PRIORITY_TRADING))
    registry.every(Periodic("gold.hfm_deep_history", 20, PRIORITY_BACKGROUND))
    registry.every(Periodic("gold.hfm_history", 3600, PRIORITY_BACKGROUND))
    registry.every(Periodic("gold.candles_backfill", 24 * 3600, PRIORITY_BACKGROUND))
