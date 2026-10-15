"""Accounts. PAPER accounts are created here; SANDBOX accounts (virtual funds on a venue's test
network) in ``cosmic.trading.sandbox``. No LIVE account can be created (no live adapter exists)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.schema_market import venues
from cosmic.db.schema_trading import accounts
from cosmic.trading import ledger


@dataclass(frozen=True)
class Account:
    id: uuid.UUID
    alias: str
    kind: str
    venue_key: str
    broker_adapter: str
    base_currency: str
    status: str
    reconciliation_status: str
    last_reconciled_at: datetime | None
    created_at: datetime


async def get_account(
    conn: AsyncConnection,
    *,
    alias: str | None = None,
    account_id: uuid.UUID | None = None,
    lock: bool = False,
) -> Account:
    q = select(accounts, venues.c.key.label("venue_key")).join(
        venues, venues.c.id == accounts.c.venue_id
    )
    if alias is not None:
        q = q.where(accounts.c.alias == alias)
    elif account_id is not None:
        q = q.where(accounts.c.id == account_id)
    else:
        raise ValueError("alias or account_id required")
    if lock:
        q = q.with_for_update(of=accounts)
    row = (await conn.execute(q)).first()
    if row is None:
        raise NotFoundError("unknown account")
    return Account(
        row.id,
        row.alias,
        row.kind,
        row.venue_key,
        row.broker_adapter,
        row.base_currency,
        row.status,
        row.reconciliation_status,
        row.last_reconciled_at,
        row.created_at,
    )


async def list_accounts(conn: AsyncConnection) -> list[Account]:
    ids = [r.id for r in await conn.execute(select(accounts.c.id).order_by(accounts.c.alias))]
    return [await get_account(conn, account_id=i) for i in ids]


async def create_paper_account(
    conn: AsyncConnection,
    *,
    alias: str,
    venue_key: str,
    base_currency: str,
    initial_capital: Decimal,
    now: datetime,
    actor: Actor,
) -> uuid.UUID:
    """Create a PAPER account funded with clearly simulated capital (ledger CAPITAL → CASH)."""
    if initial_capital <= ZERO:
        raise ValidationFailedError("initial simulated capital must be positive")
    venue_id = (
        await conn.execute(select(venues.c.id).where(venues.c.key == venue_key))
    ).scalar_one_or_none()
    if venue_id is None:
        raise NotFoundError(f"unknown venue {venue_key}")
    exists = (await conn.execute(select(accounts.c.id).where(accounts.c.alias == alias))).first()
    if exists is not None:
        raise ConflictError("account alias already exists")
    account_id = uuid7()
    await conn.execute(
        accounts.insert().values(
            id=account_id,
            alias=alias,
            kind="PAPER",
            venue_id=venue_id,
            broker_adapter="paper",
            base_currency=base_currency,
            status="ACTIVE",
            reconciliation_status="UNKNOWN",
            created_at=now,
            updated_at=now,
        )
    )
    await ledger.post(
        conn,
        account_id=account_id,
        kind="CAPITAL",
        ref_type="account_funding",
        ref_id=str(account_id),
        description="Simulated paper capital (not real money)",
        entries=[
            ledger.Entry("CASH", base_currency, initial_capital),
            ledger.Entry("CAPITAL", base_currency, -initial_capital),
        ],
        now=now,
        is_simulated=True,
    )
    await record(
        conn,
        action="account.created",
        actor=actor,
        occurred_at=now,
        entity_type="account",
        entity_id=account_id,
        payload={
            "alias": alias,
            "kind": "PAPER",
            "simulated_capital": initial_capital,
            "currency": base_currency,
        },
    )
    return account_id


async def set_reconciliation_status(
    conn: AsyncConnection,
    *,
    account_id: uuid.UUID,
    status: str,
    now: datetime,
    reconciled: bool = False,
) -> None:
    values: dict[str, object] = {"reconciliation_status": status, "updated_at": now}
    if reconciled:
        values["last_reconciled_at"] = now
    await conn.execute(update(accounts).where(accounts.c.id == account_id).values(**values))
