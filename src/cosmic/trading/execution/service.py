"""Execution authority (V6 §1809–§1813, doc 08 §6, ADR-012).

The only code path that transmits orders. One leader at a time (Postgres advisory
lock). Every state claim is broker-grounded; ambiguity becomes UNKNOWN_ORDER_STATE and
is reconciled, never resubmitted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents
from cosmic.trading.brokers.registry import BrokerRegistry
from cosmic.trading.execution.common import (
    EXECUTION_LOCK_KEY,
)
from cosmic.trading.execution.entry import EntryExecution
from cosmic.trading.execution.protection import ProtectionManager
from cosmic.trading.execution.recovery import CancelAndRecovery
from cosmic.trading.execution.sync import OrderSync


class ExecutionService(EntryExecution, OrderSync, ProtectionManager, CancelAndRecovery):
    def __init__(
        self, db: Database, clock: Clock, settings: Settings, brokers: BrokerRegistry
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.brokers = brokers

    @asynccontextmanager
    async def leadership(self) -> AsyncIterator[bool]:
        """Session-level advisory lock: exactly one execution authority at a time."""
        async with self.db.engine.connect() as conn:
            got = bool(
                await conn.scalar(text(f"SELECT pg_try_advisory_lock({EXECUTION_LOCK_KEY})"))
            )
            try:
                yield got
            finally:
                if got:
                    await conn.scalar(text(f"SELECT pg_advisory_unlock({EXECUTION_LOCK_KEY})"))
                await conn.commit()

    async def run_cycle(self) -> dict[str, int]:
        stats = {"executed": 0, "synced": 0, "managed": 0}
        async with self.leadership() as leader:
            if not leader:
                return stats
            async with self.db.connect() as conn:
                approved = [
                    r.id
                    for r in await conn.execute(
                        select(trade_intents.c.id)
                        .where(trade_intents.c.status == "APPROVED")
                        .order_by(trade_intents.c.updated_at)
                    )
                ]
            for intent_id in approved:
                await self.execute_intent(intent_id)
                stats["executed"] += 1
            stats["synced"] = await self.poll_active()
            stats["managed"] = await self.manage_positions()
        return stats
