"""Workforce and research jobs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from cosmic.db.schema_market import instruments
from cosmic.market import data as md
from cosmic.research import opportunities as opp
from cosmic.research.events import detect_and_store
from cosmic.research.registry import run_and_store
from cosmic.research.strategy import TrendSmaAtr
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_BACKGROUND, PRIORITY_DEFAULT
from cosmic.worker.registry import Periodic, Registry
from cosmic.workforce.missions import MissionDeps, MissionRunner


def runner(ctx: WorkerContext) -> MissionRunner:
    if "missions" not in ctx.services:
        ctx.services["missions"] = MissionRunner(
            MissionDeps(ctx.db, ctx.clock, ctx.settings, ctx.services.get("ai"))
        )
    r: MissionRunner = ctx.services["missions"]
    return r


async def detect_all(ctx: WorkerContext) -> list[str]:
    found: list[str] = []
    async with ctx.db.transaction() as conn:
        keys = [
            r.canonical_key
            for r in await conn.execute(
                select(instruments.c.canonical_key).where(instruments.c.tradable.is_(True))
            )
        ]
        for key in keys:
            inst = await md.instrument_by_key(conn, key)
            found += await detect_and_store(
                conn, instrument=inst, strategy=TrendSmaAtr(), now=ctx.clock.now()
            )
    return found


def register(registry: Registry, ctx: WorkerContext) -> None:
    @registry.handler("research.detect")
    async def detect(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await detect_all(ctx)

    @registry.handler("workforce.open")
    async def open_missions(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await runner(ctx).open_from_events()

    @registry.handler("workforce.step")
    async def step(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await runner(ctx).step(ctx.worker_id)

    @registry.handler("workforce.expire")
    async def expire(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await runner(ctx).expire()

    @registry.handler("research.opportunity_sync")
    async def opp_sync(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        async with ctx.db.transaction() as conn:
            await opp.sync_from_intents(conn, ctx.clock.now())

    @registry.handler("research.backtest")
    async def backtest(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        async with ctx.db.transaction() as conn:
            for r in await conn.execute(
                select(instruments.c.id).where(instruments.c.tradable.is_(True))
            ):
                await run_and_store(
                    conn, strategy=TrendSmaAtr(), instrument_id=r.id, now=ctx.clock.now()
                )

    registry.every(Periodic("research.detect", 300, PRIORITY_DEFAULT))
    registry.every(Periodic("workforce.open", 60, PRIORITY_DEFAULT))
    registry.every(Periodic("workforce.step", 30, PRIORITY_BACKGROUND))
    registry.every(Periodic("workforce.expire", 120, PRIORITY_DEFAULT))
    registry.every(Periodic("research.opportunity_sync", 60, PRIORITY_DEFAULT))
    registry.every(Periodic("research.backtest", 7 * 86400, PRIORITY_BACKGROUND))
