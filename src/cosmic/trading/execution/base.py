"""State and cross-part methods shared by the execution authority's mixins.

The concrete implementations live in the mixins; :class:`ExecutionService` combines them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.trading.accounts import Account
from cosmic.trading.brokers.registry import BrokerRegistry
from cosmic.trading.execution.common import (
    Outcome,
)
from cosmic.trading.modes import Operation
from cosmic.trading.orders import OrderRow, Source
from cosmic.trading.proposals import entry_held


class ExecutionBase:
    db: Database
    clock: Clock
    settings: Settings
    brokers: BrokerRegistry
    # When this process first heard "no such order" for an order the venue had acknowledged;
    # cleared when the venue knows it again. Kept in memory: a restart only waits longer.
    missing_since: dict[uuid.UUID, datetime]

    async def execute_intent(self, intent_id: uuid.UUID) -> Outcome:
        raise NotImplementedError

    async def poll_active(self) -> int:
        raise NotImplementedError

    async def manage_positions(self) -> int:
        raise NotImplementedError

    async def release_exits(self, exit_intent_id: uuid.UUID | None = None) -> int:
        raise NotImplementedError

    async def _exit_settled(self, conn: AsyncConnection, o: OrderRow, now: datetime) -> None:
        raise NotImplementedError

    async def _intent_held(self, conn: AsyncConnection, intent_id: uuid.UUID) -> Decimal:
        """What an intent's own orders hold: buys minus sells (stops, take-profits, exits)."""
        return await entry_held(conn, intent_id)

    async def sync_order(self, order_id: uuid.UUID, *, source: Source = Source.BROKER) -> OrderRow:
        raise NotImplementedError

    async def _transmit(
        self, order_id: uuid.UUID, account: Account, *, intent_operation: Operation
    ) -> Outcome:
        raise NotImplementedError

    async def request_cancel(
        self, order_id: uuid.UUID, *, reason: str, actor: Actor | None = None
    ) -> Outcome:
        raise NotImplementedError

    async def _protection_failed(
        self, conn: AsyncConnection, o: OrderRow, reason: str, now: datetime
    ) -> None:
        raise NotImplementedError
