"""State and cross-part methods shared by the execution authority's mixins.

The concrete implementations live in the mixins; :class:`ExecutionService` combines them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

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


class ExecutionBase:
    db: Database
    clock: Clock
    settings: Settings
    brokers: BrokerRegistry

    async def execute_intent(self, intent_id: uuid.UUID) -> Outcome:
        raise NotImplementedError

    async def poll_active(self) -> int:
        raise NotImplementedError

    async def manage_positions(self) -> int:
        raise NotImplementedError

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
