"""An HFM demo account's balance changed outside the system (S99; the owner, 2026-11-17, set their
demo's balance to 100 USD in myHF and chose to keep it): the owner accepts HFM's balance, and the
books follow it.

HFM's balance deals (a deposit, a withdrawal, a demo's balance set by hand) never reach the
system: the bridge reports gold's deals only. So the books still hold the old money, every
reconciliation fails on the balance, and new exposure stays blocked. Accepting it:

- only for an HFM demo account (a SANDBOX on an HFM adapter), and only while nothing is open
  there, at HFM or in the books: positions and orders cannot be mistaken for the difference;
- only when the account's latest reconciliation failed on its money alone;
- the money the system manages becomes HFM's balance when that is lower (a withdrawal from it),
  and stays as it was when HFM holds more (the rest is the owner's, outside the system's money:
  UNMANAGED); either way the books then equal HFM's balance exactly;
- one balanced ledger transaction (``ADJUSTMENT_RECONCILE``), audited, with the owner's code
  (the route); a reconciliation follows at once.
- when the money the system manages changes, R08's high-water mark counts from then (S100):
  the old money's best is not a loss of the new money (``context_builder.loss_history``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.errors import ConflictError
from cosmic.core.money import ZERO
from cosmic.db.schema_execution import reconciliation_items, reconciliation_runs
from cosmic.db.schema_trading import bridge_state
from cosmic.trading import ledger
from cosmic.trading.accounts import Account
from cosmic.trading.brokers.mt5_bridge import BY_ADAPTER

STEP = "BALANCES"


@dataclass(frozen=True)
class Gap:
    """What HFM holds against what the books hold, for one demo account."""

    account_id: uuid.UUID
    alias: str
    currency: str
    hfm: Decimal  # HFM's balance (nothing open)
    managed: Decimal  # the money the system manages (CASH)
    outside: Decimal  # the owner's money outside it (UNMANAGED)

    @property
    def books(self) -> Decimal:
        return self.managed + self.outside

    @property
    def managed_after(self) -> Decimal:
        return min(self.managed, self.hfm)

    @property
    def outside_after(self) -> Decimal:
        return self.hfm - self.managed_after


async def gap(conn: AsyncConnection, account: Account) -> Gap | None:
    """The gap the owner may accept, or None (not an HFM demo account, something open, or the
    latest reconciliation did not fail on the money alone)."""
    bridge = BY_ADAPTER.get(account.broker_adapter or "")
    if bridge is None or account.kind != "SANDBOX" or account.status != "ACTIVE":
        return None
    run = (
        await conn.execute(
            select(reconciliation_runs.c.id, reconciliation_runs.c.status)
            .where(reconciliation_runs.c.account_id == account.id)
            .order_by(desc(reconciliation_runs.c.started_at))
            .limit(1)
        )
    ).first()
    if run is None or run.status != "FAILED":
        return None
    wrong = list(
        await conn.execute(
            select(reconciliation_items.c.step, reconciliation_items.c.subject).where(
                reconciliation_items.c.run_id == run.id,
                reconciliation_items.c.status != "MATCH",
            )
        )
    )
    quote = account.base_currency
    if [(w.step, w.subject) for w in wrong] != [(STEP, quote)]:
        return None
    snap = await conn.scalar(
        select(bridge_state.c.snapshot).where(bridge_state.c.bridge == bridge.name)
    )
    acct = (snap or {}).get("account") or {}
    if acct.get("currency") != quote or acct.get("balance") is None:
        return None
    if acct.get("positions_total") or acct.get("orders_total") or (snap or {}).get("positions"):
        return None  # something open at HFM: its money is not only the balance
    books = await ledger.balances(conn, account.id)
    if any(book == "POSITION" and amount != ZERO for (book, _), amount in books.items()):
        return None  # something open in the books
    hfm = Decimal(str(acct["balance"]))
    if hfm < ZERO:
        return None
    return Gap(
        account.id,
        account.alias,
        quote,
        hfm,
        books.get(("CASH", quote), ZERO),
        books.get(("UNMANAGED", quote), ZERO),
    )


async def accept(conn: AsyncConnection, account: Account, *, actor: Actor, now: datetime) -> Gap:
    """Book HFM's balance for this demo account (see the module's description). Refuses when
    there is nothing the owner may accept."""
    found = await gap(conn, account)
    if found is None:
        raise ConflictError(
            "there is no balance difference to accept: the account is not an HFM demo account, "
            "something is open, or its latest check did not fail on its money alone"
        )
    cash = found.managed_after - found.managed
    outside = found.outside_after - found.outside
    if cash == ZERO and outside == ZERO:
        raise ConflictError("the books already hold HFM's balance")
    entries = [
        e
        for e in (
            ledger.Entry("CASH", found.currency, cash),
            ledger.Entry("UNMANAGED", found.currency, outside),
            ledger.Entry("CAPITAL", found.currency, -(cash + outside)),
        )
        if e.amount != ZERO
    ]
    await ledger.post(
        conn,
        account_id=account.id,
        kind="ADJUSTMENT_RECONCILE",
        ref_type=ledger.BALANCE_ACCEPTED,
        ref_id=f"{account.id}:{now.isoformat()}",
        description=(
            f"HFM's balance {found.hfm} {found.currency}, changed outside the system, accepted by "
            f"the owner: the system manages {found.managed_after}, the rest is outside its money"
        ),
        entries=entries,
        now=now,
        is_simulated=True,  # a demo account's money
    )
    await record(
        conn,
        action="account.venue_balance_accepted",
        actor=actor,
        occurred_at=now,
        entity_type="account",
        entity_id=account.id,
        payload={
            "alias": account.alias,
            "currency": found.currency,
            "hfm_balance": str(found.hfm),
            "books_before": str(found.books),
            "managed_before": str(found.managed),
            "managed_after": str(found.managed_after),
            "outside_after": str(found.outside_after),
        },
    )
    return found
