"""Open a paper account: internal ledger capital and paper-venue funds in one transaction.

The simulator fills against the chosen venue's own prices. Binance (USDT) is the default; a
venue listed in ``COSMIC_RETIRED_VENUES`` cannot be chosen (ADR-027).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.trading.accounts import create_paper_account
from cosmic.trading.brokers.paper import PaperBroker

# Venue → cash currency of a paper account there (first = the portal's default).
PAPER_VENUES = {"BINANCE": "USDT", "COINBASE": "USD"}


def paper_venues(retired: frozenset[str]) -> dict[str, str]:
    return {v: c for v, c in PAPER_VENUES.items() if v not in retired}


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
