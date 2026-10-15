"""Double-entry ledger (doc 05 §3). Every transaction balances per asset (DB-enforced)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.schema_trading import ledger_entries, ledger_txns

# UNMANAGED: venue holdings an account does not manage (a test account's other virtual funds,
# ADR-026). Reconciliation counts them; equity, positions and risk do not.
BOOKS = frozenset({"CASH", "POSITION", "CLEARING", "FEES", "CAPITAL", "UNMANAGED"})


@dataclass(frozen=True)
class Entry:
    book: str
    asset: str
    amount: Decimal


def check_balanced(entries: list[Entry]) -> None:
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for e in entries:
        if e.book not in BOOKS:
            raise ValidationFailedError(f"unknown ledger book {e.book}")
        if e.amount == ZERO:
            raise ValidationFailedError("zero-amount ledger entry")
        totals[e.asset] += e.amount
    unbalanced = {a: t for a, t in totals.items() if t != ZERO}
    if unbalanced:
        raise ValidationFailedError(f"ledger transaction does not balance: {unbalanced}")


def fill_entries(
    *,
    side: str,
    base: str,
    quote: str,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    fee_asset: str,
) -> list[Entry]:
    """Entries for a spot fill. BUY adds base to POSITION and removes quote from CASH.

    The fee comes out of the asset it is charged in: quote from CASH, base from POSITION (a
    Binance buy pays its commission in the asset received), anything else from UNMANAGED.
    """
    if quantity <= ZERO or price <= ZERO or fee < ZERO:
        raise ValidationFailedError("fill quantity/price must be positive and fee non-negative")
    value = quantity * price
    sign = Decimal(1) if side == "BUY" else Decimal(-1)
    entries = [
        Entry("POSITION", base, sign * quantity),
        Entry("CLEARING", base, -sign * quantity),
        Entry("CLEARING", quote, sign * value),
        Entry("CASH", quote, -sign * value),
    ]
    if fee > ZERO:
        entries += [
            Entry("FEES", fee_asset, fee),
            Entry(
                "CASH" if fee_asset == quote else "POSITION" if fee_asset == base else "UNMANAGED",
                fee_asset,
                -fee,
            ),
        ]
    return entries


async def post(
    conn: AsyncConnection,
    *,
    account_id: uuid.UUID,
    kind: str,
    ref_type: str,
    ref_id: str,
    description: str,
    entries: list[Entry],
    now: datetime,
    is_simulated: bool,
) -> uuid.UUID | None:
    """Post a balanced transaction. Idempotent per (kind, ref_type, ref_id): returns None
    if that reference was already posted."""
    check_balanced(entries)
    txn_id = uuid7()
    inserted = (
        await conn.execute(
            pg_insert(ledger_txns)
            .values(
                id=txn_id,
                account_id=account_id,
                kind=kind,
                ref_type=ref_type,
                ref_id=ref_id,
                description=description[:300],
                is_simulated=is_simulated,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["kind", "ref_type", "ref_id"])
            .returning(ledger_txns.c.id)
        )
    ).first()
    if inserted is None:
        return None
    await conn.execute(
        ledger_entries.insert(),
        [
            {
                "id": uuid7(),
                "txn_id": txn_id,
                "account_id": account_id,
                "book": e.book,
                "asset": e.asset,
                "amount": e.amount,
                "created_at": now,
            }
            for e in entries
        ],
    )
    return txn_id


async def balances(conn: AsyncConnection, account_id: uuid.UUID) -> dict[tuple[str, str], Decimal]:
    rows = await conn.execute(
        select(
            ledger_entries.c.book,
            ledger_entries.c.asset,
            func.sum(ledger_entries.c.amount).label("total"),
        )
        .where(ledger_entries.c.account_id == account_id)
        .group_by(ledger_entries.c.book, ledger_entries.c.asset)
    )
    return {(r.book, r.asset): Decimal(r.total) for r in rows}


async def book_balance(
    conn: AsyncConnection, account_id: uuid.UUID, book: str, asset: str
) -> Decimal:
    value = await conn.scalar(
        select(func.coalesce(func.sum(ledger_entries.c.amount), 0)).where(
            ledger_entries.c.account_id == account_id,
            ledger_entries.c.book == book,
            ledger_entries.c.asset == asset,
        )
    )
    return ZERO if value is None else Decimal(value)
