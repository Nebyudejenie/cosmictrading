"""Open a paper account: internal ledger capital and paper-venue funds in one transaction."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.trading.accounts import create_paper_account
from cosmic.trading.brokers.paper import PaperBroker


async def open_paper_account(
    conn: AsyncConnection,
    *,
    broker: PaperBroker,
    alias: str,
    initial_capital: Decimal,
    now: datetime,
    actor: Actor,
    venue_key: str = "COINBASE",
    base_currency: str = "USD",
) -> uuid.UUID:
    account_id = await create_paper_account(
        conn,
        alias=alias,
        venue_key=venue_key,
        base_currency=base_currency,
        initial_capital=initial_capital,
        now=now,
        actor=actor,
    )
    await broker.fund(conn, account_id, base_currency, initial_capital)
    return account_id
