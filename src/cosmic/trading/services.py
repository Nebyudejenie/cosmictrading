"""Wiring of the trading services (one place, shared by worker, web and bot)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_trading import accounts
from cosmic.identity.service import Principal
from cosmic.trading import killswitch
from cosmic.trading.brokers.paper import PaperBroker, PaperConfig
from cosmic.trading.brokers.registry import BrokerRegistry
from cosmic.trading.execution import ExecutionService
from cosmic.trading.reconcile import Reconciler, ReconResult


@dataclass
class TradingServices:
    db: Database
    clock: Clock
    settings: Settings
    paper: PaperBroker
    brokers: BrokerRegistry
    execution: ExecutionService
    reconciler: Reconciler

    @classmethod
    def build(
        cls, db: Database, clock: Clock, settings: Settings, paper_config: PaperConfig | None = None
    ) -> TradingServices:
        paper = PaperBroker(db, clock, settings, paper_config)
        brokers = BrokerRegistry(paper)
        execution = ExecutionService(db, clock, settings, brokers)
        return cls(
            db,
            clock,
            settings,
            paper,
            brokers,
            execution,
            Reconciler(db, clock, brokers, execution),
        )

    async def emergency_stop(
        self, *, actor: Actor, reason: str, principal: Principal | None = None
    ) -> killswitch.Activation:
        now = self.clock.now()
        async with self.db.transaction() as conn:
            activation = await killswitch.activate(
                conn, actor=actor, reason=reason, now=now, principal=principal
            )
        for order_id in activation.entry_orders_to_cancel:
            await self.execution.request_cancel(
                order_id, reason="emergency stop: cancel unfilled entry", actor=actor
            )
        await self.reconcile_all(trigger="emergency_stop")
        return activation

    async def reconcile_all(self, *, trigger: str) -> list[ReconResult]:
        async with self.db.connect() as conn:
            ids = [
                r.id
                for r in await conn.execute(
                    select(accounts.c.id).where(accounts.c.status == "ACTIVE")
                )
            ]
        return [await self.reconciler.reconcile(i, trigger=trigger) for i in ids]
