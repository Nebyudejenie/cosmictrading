"""Wiring of the trading services (one place, shared by worker, web and bot)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_trading import accounts
from cosmic.identity.service import Principal
from cosmic.trading import killswitch
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.base import BrokerAdapter
from cosmic.trading.brokers.binance_testnet import SandboxBroker
from cosmic.trading.brokers.paper import PaperBroker, PaperConfig
from cosmic.trading.brokers.registry import BrokerRegistry
from cosmic.trading.execution import ExecutionService
from cosmic.trading.reconcile import Reconciler, ReconResult
from cosmic.worker.queue import PRIORITY_SAFETY, enqueue


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
        cls,
        db: Database,
        clock: Clock,
        settings: Settings,
        paper_config: PaperConfig | None = None,
        sandbox: SandboxBroker | None = None,
        hfm: BrokerAdapter | None = None,
    ) -> TradingServices:
        """``sandbox`` and ``hfm`` are passed only by the worker, the one process that reaches
        the Binance test network and the HFM bridge."""
        paper = PaperBroker(db, clock, settings, paper_config)
        brokers = BrokerRegistry(paper, sandbox, hfm)
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
            if await self._unreachable_accounts(conn):
                # Some account's venue is reachable only from the worker: it does the cancels
                # and reconciliation, queued in the same transaction as the stop itself.
                await enqueue(
                    conn,
                    kind="trading.emergency_followup",
                    now=now,
                    priority=PRIORITY_SAFETY,
                    payload={
                        "entry_orders": [str(i) for i in activation.entry_orders_to_cancel],
                        "user_id": str(principal.user_id) if principal else None,
                    },
                )
                return activation
        for order_id in activation.entry_orders_to_cancel:
            await self.execution.request_cancel(
                order_id, reason="emergency stop: cancel unfilled entry", actor=actor
            )
        await self.reconcile_all(trigger="emergency_stop")
        return activation

    async def _unreachable_accounts(self, conn: AsyncConnection) -> list[uuid.UUID]:
        ids = [
            r.id
            for r in await conn.execute(select(accounts.c.id).where(accounts.c.status == "ACTIVE"))
        ]
        return [
            i
            for i in ids
            if not self.brokers.serves(await get_account(conn, account_id=uuid.UUID(str(i))))
        ]

    async def reconcile_all(self, *, trigger: str) -> list[ReconResult]:
        """Every active account this process can reach (the worker reaches all of them)."""
        async with self.db.connect() as conn:
            ids = [
                r.id
                for r in await conn.execute(
                    select(accounts.c.id).where(accounts.c.status == "ACTIVE")
                )
            ]
            skip = set(await self._unreachable_accounts(conn))
        return [await self.reconciler.reconcile(i, trigger=trigger) for i in ids if i not in skip]
