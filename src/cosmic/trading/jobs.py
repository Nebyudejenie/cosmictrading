"""Trading jobs: execution cycle, paper venue matching, expiry, reconciliation, equity."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from cosmic.core.ids import uuid7
from cosmic.db.schema_trading import accounts, equity_snapshots
from cosmic.trading import authority
from cosmic.trading.accounts import get_account
from cosmic.trading.approvals import expire_sweep
from cosmic.trading.context_builder import account_state
from cosmic.trading.policy import ensure_default_policy
from cosmic.trading.services import TradingServices
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_SAFETY, PRIORITY_TRADING
from cosmic.worker.registry import Periodic, Registry


def services(ctx: WorkerContext) -> TradingServices:
    svc: TradingServices = ctx.services["trading"]
    return svc


async def startup(ctx: WorkerContext) -> None:
    now = ctx.clock.now()
    async with ctx.db.transaction() as conn:
        await ensure_default_policy(conn, now)
        await authority.enforce_cap(conn, settings=ctx.settings, now=now)
    if "trading" not in ctx.services:
        ctx.services["trading"] = TradingServices.build(ctx.db, ctx.clock, ctx.settings)
    await services(ctx).execution.startup_recovery()


async def record_equity(ctx: WorkerContext) -> int:
    now = ctx.clock.now()
    written = 0
    async with ctx.db.transaction() as conn:
        ids = [
            r.id
            for r in await conn.execute(select(accounts.c.id).where(accounts.c.status == "ACTIVE"))
        ]
        for account_id in ids:
            acct = await get_account(conn, account_id=account_id)
            state = await account_state(conn, acct, now)
            if state.equity is None or state.cash_available is None:
                continue  # unknown valuation is not recorded as a number
            await conn.execute(
                equity_snapshots.insert().values(
                    id=uuid7(),
                    account_id=account_id,
                    taken_at=now,
                    equity=state.equity,
                    cash=state.cash_available,
                    positions_value=state.equity - state.cash_available,
                    price_label="DELAYED",
                    is_simulated=acct.kind == "PAPER",
                )
            )
            written += 1
    return written


def register(registry: Registry, ctx: WorkerContext) -> None:
    ctx.services.setdefault("startup_hooks", []).append(startup)

    @registry.handler("trading.cycle")
    async def cycle(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await services(ctx).execution.run_cycle()

    @registry.handler("paper.tick")
    async def tick(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await services(ctx).paper.tick()

    @registry.handler("trading.expire")
    async def expire(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        async with ctx.db.transaction() as conn:
            await expire_sweep(conn, now=ctx.clock.now())

    @registry.handler("trading.reconcile")
    async def reconcile(ctx: WorkerContext, p: dict[str, Any]) -> None:
        svc = services(ctx)
        if p.get("account_id"):
            await svc.reconciler.reconcile(
                uuid.UUID(str(p["account_id"])), trigger=str(p.get("trigger", "manual"))
            )
        else:
            await svc.reconcile_all(trigger="scheduled")

    @registry.handler("trading.equity_snapshot")
    async def equity(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await record_equity(ctx)

    registry.every(Periodic("trading.cycle", 5, PRIORITY_TRADING))
    registry.every(Periodic("paper.tick", 10, PRIORITY_TRADING))
    registry.every(Periodic("trading.expire", 30, PRIORITY_SAFETY))
    registry.every(Periodic("trading.reconcile", 300, PRIORITY_SAFETY))
    registry.every(Periodic("trading.equity_snapshot", 300, PRIORITY_TRADING))
