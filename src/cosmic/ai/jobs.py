"""AI worker jobs: gateway lifecycle and live price checks for runtime-checked providers."""

from __future__ import annotations

from typing import Any

from cosmic.ai import admin, connection_test, gold_opinion
from cosmic.ai.adapters.http import make_client
from cosmic.ai.gateway import AIGateway
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_DEFAULT
from cosmic.worker.registry import Periodic, Registry


async def startup(ctx: WorkerContext) -> None:
    if "ai" not in ctx.services:
        ctx.services["ai"] = AIGateway(ctx.db, ctx.clock, ctx.settings)


async def shutdown(ctx: WorkerContext) -> None:
    gw = ctx.services.pop("ai", None)
    if isinstance(gw, AIGateway):
        await gw.close()


def register(registry: Registry, ctx: WorkerContext) -> None:
    ctx.services.setdefault("startup_hooks", []).append(startup)
    ctx.services.setdefault("shutdown_hooks", []).append(shutdown)

    @registry.handler("ai.price_check")
    async def price_check(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        async with make_client() as client, ctx.db.transaction() as conn:
            await admin.run_price_checks(
                conn, settings=ctx.settings, client=client, now=ctx.clock.now()
            )

    registry.every(Periodic("ai.price_check", 6 * 3600, PRIORITY_DEFAULT))

    @registry.handler("ai.gold_opinion")
    async def gold_opinion_job(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        """The AI's second opinion on HFM's gold, every few minutes while the market is open
        (S68); nothing when no free AI route is configured."""
        gw = ctx.services.get("ai")
        if isinstance(gw, AIGateway):
            await gold_opinion.opine(ctx.db, gw, ctx.clock.now())

    registry.every(Periodic("ai.gold_opinion", 60, PRIORITY_DEFAULT))

    @registry.handler("ai.test_provider")
    async def test_provider(ctx: WorkerContext, p: dict[str, Any]) -> None:
        """The owner's connection and inference test of one provider (S81)."""
        await connection_test.run(
            ctx.db, ctx.settings, str(p.get("provider_key") or ""), ctx.clock.now()
        )
