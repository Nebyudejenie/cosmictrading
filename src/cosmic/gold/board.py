"""The internal gold simulation's page, as data (S62): the account, its money, the reading of
gold now, its open and closed trades and its recent decisions. SIMULATED throughout."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import ZERO
from cosmic.db.schema_gold import gold_sim_accounts, gold_sim_decisions
from cosmic.gold import analysis, sim


@dataclass
class Board:
    reading: analysis.Reading
    price: sim.Price | None
    account: Any | None = None
    settings: sim.Settings | None = None
    state: sim.State | None = None
    open: list[sim.Position] = field(default_factory=list)
    closed: list[sim.Position] = field(default_factory=list)
    decisions: list[Any] = field(default_factory=list)
    earlier: list[Any] = field(default_factory=list)  # earlier simulated accounts, kept

    @property
    def running(self) -> bool:
        return self.account is not None and self.account.status == "RUNNING"

    @property
    def wins(self) -> int:
        return sum(1 for p in self.closed if (p.result or ZERO) > 0)

    @property
    def win_rate(self) -> Decimal | None:
        if not self.closed:
            return None
        return (Decimal(self.wins) / len(self.closed) * 100).quantize(Decimal("0.1"))

    @property
    def total(self) -> Decimal | None:
        """Equity less the starting balance: everything it has made or lost."""
        if self.state is None or self.account is None:
            return None
        start: Decimal = self.account.starting_balance
        return self.state.equity - start

    def floating(self, p: sim.Position) -> Decimal | None:
        return p.floating(self.price.bid, self.price.ask) if self.price else None

    def now_price(self, p: sim.Position) -> Decimal | None:
        if self.price is None:
            return None
        return self.price.bid if p.side == "BUY" else self.price.ask


async def build(conn: AsyncConnection, now: datetime) -> Board:
    reading = await sim.reading_now(conn, now)
    price = await sim.price_now(conn, now)
    board = Board(reading, price)
    rows = list(
        await conn.execute(select(gold_sim_accounts).order_by(desc(gold_sim_accounts.c.created_at)))
    )
    if not rows:
        return board
    account = next((r for r in rows if r.status == "RUNNING"), rows[0])
    board.account = account
    board.earlier = [r for r in rows if r.id != account.id]
    board.settings = await sim.settings_of(conn, account.id)
    board.state = await sim.state(conn, account.id, price, now)
    board.open = await sim.positions(conn, account.id)
    board.closed = await sim.positions(conn, account.id, status=sim.CLOSED, limit=200)
    board.decisions = list(
        await conn.execute(
            select(gold_sim_decisions)
            .where(gold_sim_decisions.c.account_id == account.id)
            .order_by(desc(gold_sim_decisions.c.bar))
            .limit(30)
        )
    )
    return board
