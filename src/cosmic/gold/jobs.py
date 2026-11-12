"""The gold simulation's worker jobs (S62)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cosmic.core.logs import get_logger
from cosmic.gold import daily, hfm_demo, hfm_selftest, predictions, sim
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
# Between those checks (S87, the owner's rule of 2026-11-12: an order leaves within a second of
# its prediction): how often a newly complete 1-minute candle is looked for. A run keeps looking
# until the next run has started (they are started every 5 s, and picked up within a second),
# so no moment passes unwatched; at most HAND_OVER seconds, when no next run comes.
LOOK_EVERY = 0.1
HAND_OVER = 3.0
NEWEST_RUN = "gold_tick_started"  # in the worker's services: when the newest run started
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


async def _newest(ctx: WorkerContext) -> datetime | None:
    async with ctx.db.connect() as conn:
        return await hfm_candles.newest_complete(conn, "1m")


async def hfm_now(ctx: WorkerContext, now: datetime, *, selftest: bool = True) -> list[str]:
    """The HFM demo robot at this moment: the owner's self-test (S67), the decision on a new
    candle, and, when that decision opened a trade, its order at once (S87). A trade the
    owner's rules approved in the decision itself does not wait for the trading cycle's next
    run (up to 5 s): the order is created, checked and handed to the bridge here. Never able
    to hold back the simulation's stops (S64)."""
    started = time.monotonic()
    try:
        if selftest:
            tested = await hfm_selftest.advance(ctx.db, now)  # the owner's self-test (S67)
            if tested:
                log.info("gold_hfm_selftest", steps=tested)
        hfm = await hfm_demo.decide(ctx.db, now)
    except Exception as exc:
        log.error("gold_hfm_failed", error=type(exc).__name__, detail=str(exc)[:200])
        return []
    decided = time.monotonic()
    if any(not d.endswith(":WAIT") for d in hfm):
        log.info("gold_hfm", decided=hfm)
    trading = ctx.services.get("trading")
    sent = False
    if trading is not None and any(d.endswith(":PROPOSED") for d in hfm):
        try:
            await trading.execution.execute_approved()
            sent = True
        except Exception as exc:  # the trading cycle takes it at its next run
            log.error("gold_hfm_send_failed", error=type(exc).__name__, detail=str(exc)[:200])
    if hfm:  # once a candle: how long the decision took, and the order's step when there was one
        log.info(
            "gold_hfm_step",
            decide_ms=round((decided - started) * 1000),
            order_ms=round((time.monotonic() - decided) * 1000) if sent else None,
        )
    return hfm


async def watch_candles(
    ctx: WorkerContext,
    seen: datetime | None,
    until: float,
    done: Callable[[], bool] | None = None,
) -> datetime | None:
    """Until ``until`` (``time.monotonic()``), or until ``done`` says so: look for a newly
    complete 1-minute candle, and let the HFM robot decide the moment there is one. Returns the
    newest candle seen."""
    said = False
    while time.monotonic() < until and not (done is not None and done()):
        await asyncio.sleep(LOOK_EVERY)
        try:
            newest = await _newest(ctx)
        except Exception as exc:  # the database for a moment: the every-second check goes on
            if not said:  # once a run, not ten times a second
                log.warning("gold_candle_watch_failed", error=type(exc).__name__)
                said = True
            continue
        if newest != seen:
            seen = newest
            await hfm_now(ctx, ctx.clock.now(), selftest=False)
    return seen


def register(registry: Registry, ctx: WorkerContext) -> None:
    @registry.handler("gold.sim_tick")
    async def sim_tick(ctx: WorkerContext, payload: dict[str, Any]) -> None:
        """Every second: stops, targets and swap; a decision on each new 1-minute candle. In
        between, the candles are watched ten times a second, and the HFM robot decides the
        moment one completes (S87). A run hands over to the next: it watches until that one
        has started."""
        started = time.monotonic()
        ctx.services[NEWEST_RUN] = started
        seen = await _newest(ctx)
        for i in range(CHECKS):
            now = ctx.clock.now()
            closed = await sim.watch(ctx.db, now)
            decided = await sim.decide(ctx.db, now)
            if closed or any(not d.endswith(":WAIT") for d in decided):
                log.info("gold_sim", closed=closed, decided=decided)
            await hfm_now(ctx, now)
            try:  # the 1-minute rule's forward test: a prediction a candle, and its outcomes
                await predictions.tick(ctx.db, now)
            except Exception as exc:
                log.error(
                    "gold_predictions_failed", error=type(exc).__name__, detail=str(exc)[:200]
                )
            try:  # the daily robot on the second HFM demo (S75), apart from the others
                days = await daily.decide(ctx.db, now)
            except Exception as exc:
                log.error("gold_daily_failed", error=type(exc).__name__, detail=str(exc)[:200])
            else:
                if days:
                    log.info("gold_daily", decided=days)
            if i < CHECKS - 1:
                seen = await watch_candles(ctx, seen, time.monotonic() + 1)
        await watch_candles(
            ctx,
            seen,
            time.monotonic() + HAND_OVER,
            done=lambda: ctx.services.get(NEWEST_RUN) != started,
        )

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
