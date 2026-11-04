"""My Gold Auto-Trader, as data (S62, S63): the account, its money, the reading of gold now, its
open and closed trades, its recent decisions, its honest status, and everything the system
calculates from the owner's amount. SIMULATED throughout."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import ZERO
from cosmic.db.schema_gold import gold_sim_accounts, gold_sim_decisions
from cosmic.gold import analysis, sim

# Statuses, in the owner's words (the directive of 2026-11-04).
READY, RUNNING, WAITING = "READY", "RUNNING", "WAITING"
ATTENTION, PAUSED, STOPPED = "NEEDS ATTENTION", "PAUSED", "STOPPED"
DECIDES_EVERY = timedelta(minutes=3)  # longer without a decision, with the market open: say so
HFM_SMALLEST = Decimal(1)  # ounces: HFM's smallest gold trade, 0.01 lot of 100 ounces
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Calc:
    """What the system works out from the owner's amount and settings, at HFM's price now."""

    risk_per_trade: Decimal
    daily_loss: Decimal
    at_risk_at_once: Decimal
    stop_distance: Decimal | None  # an ounce, from the hourly volatility now
    size: sim.Size | None  # a trade now, or why it waits
    target_distance: Decimal | None
    spread: Decimal | None
    swap_buy: Decimal | None  # an ounce a night
    swap_sell: Decimal | None
    hfm_risk: Decimal | None  # HFM's smallest trade (1 ounce) at the stop
    hfm_from: Decimal | None  # the amount from which HFM's smallest trade fits the risk


def calculate(
    s: sim.Settings, st: sim.State, reading: analysis.Reading, price: sim.Price | None
) -> Calc:
    stop = reading.stop_distance
    sized = None
    if stop is not None and price is not None:
        sized = sim.size(s, st, stop, price.ask, price.leverage)
    return Calc(
        risk_per_trade=(s.amount * s.risk_pct / 100).quantize(CENT, ROUND_DOWN),
        daily_loss=s.daily_loss,
        at_risk_at_once=(s.amount * s.max_open_risk_pct / 100).quantize(CENT, ROUND_DOWN),
        stop_distance=stop,
        size=sized,
        target_distance=stop * s.take_profit_r
        if stop is not None and s.take_profit_r is not None
        else None,
        spread=price.spread if price else None,
        swap_buy=sim.swap_per_ounce(price.swap_terms, "BUY", price.bid) if price else None,
        swap_sell=sim.swap_per_ounce(price.swap_terms, "SELL", price.ask) if price else None,
        hfm_risk=(HFM_SMALLEST * stop).quantize(CENT) if stop is not None else None,
        hfm_from=(HFM_SMALLEST * stop * 100 / s.risk_pct).quantize(CENT, ROUND_CEILING)
        if stop is not None
        else None,
    )


@dataclass
class Board:
    reading: analysis.Reading
    price: sim.Price | None
    feed: str = sim.FEED_OK
    now: datetime | None = None
    account: Any | None = None
    settings: sim.Settings | None = None
    state: sim.State | None = None
    open: list[sim.Position] = field(default_factory=list)
    closed: list[sim.Position] = field(default_factory=list)
    decisions: list[Any] = field(default_factory=list)
    earlier: list[Any] = field(default_factory=list)  # earlier simulated accounts, kept
    pending: Any | None = None  # a trade it asked about, waiting for the owner's answer
    calc: Calc | None = None

    @property
    def running(self) -> bool:
        return self.account is not None and self.account.status == "RUNNING"

    @property
    def active(self) -> bool:
        return self.account is not None and self.account.status in sim.ACTIVE

    @property
    def status(self) -> tuple[str, str]:
        """One status and what it means, from the evidence only."""
        no_price = sim.FEED_WORDS.get(self.feed)
        if not self.active:
            if self.feed in (sim.BRIDGE_SILENT, sim.NOT_DEMO, sim.NO_PRICE):
                return ATTENTION, f"{no_price}: nothing can start trading until it does"
            if self.account is not None:
                return STOPPED, (
                    f"stopped {self.account.status_changed_at:%d %b %H:%M} UTC "
                    f"({self.account.status_reason}); its records are kept below"
                )
            closed = "; the market is closed now, it trades when it reopens" if no_price else ""
            return READY, f"choose your amount and press START{closed}"
        if self.pending is not None:
            return ATTENTION, "a trade waits for your answer (below)"
        if self.feed in (sim.BRIDGE_SILENT, sim.NOT_DEMO, sim.NO_PRICE):
            return ATTENTION, (
                f"{no_price}: no new trade opens; open trades are checked at HFM's next price"
            )
        if not self.running:
            return PAUSED, (
                "Automation is OFF: no new trade opens; open trades keep their stop-loss and "
                "take-profit, watched every second"
            )
        account = self.account
        assert account is not None
        last = self.decisions[0].at if self.decisions else None
        since = account.status_changed_at if last is None else max(last, account.status_changed_at)
        quiet = self.now is not None and self.now - since > DECIDES_EVERY
        if self.feed == sim.FEED_OK and quiet:
            return ATTENTION, (
                "no decision for over 3 minutes while HFM's market is open: the background "
                "worker may have stopped"
            )
        if self.open:
            n = len(self.open)
            return RUNNING, (
                f"{n} trade{'s' if n > 1 else ''} open; stop-loss and take-profit checked every "
                "second at HFM's price"
            )
        if self.feed == sim.MARKET_CLOSED:
            return WAITING, "HFM's gold market is closed; it decides again when it reopens"
        why = self.decisions[0].reasons[-1] if self.decisions and self.decisions[0].reasons else ""
        return WAITING, f"waiting for an opportunity{': ' + why if why else ''}"

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


async def build(conn: AsyncConnection, now: datetime, amount: Decimal | None = None) -> Board:
    """``amount``: the owner's amount for a new start, for the calculations before it starts."""
    reading = await sim.reading_now(conn, now)
    price, why = await sim.feed(conn, now)
    board = Board(reading, price, why, now)
    rows = list(
        await conn.execute(select(gold_sim_accounts).order_by(desc(gold_sim_accounts.c.created_at)))
    )
    if not rows or amount is not None:
        start = amount if amount is not None and amount > 0 else Decimal(50)
        board.calc = calculate(sim.defaults(start), sim.fresh_state(start), reading, price)
        if not rows:
            return board
    account = next((r for r in rows if r.status in sim.ACTIVE), rows[0])
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
    board.pending = await sim.pending_ask(conn, account.id, now)
    if board.calc is None and board.settings is not None and board.active:
        board.calc = calculate(board.settings, board.state, reading, price)
    elif board.calc is None:
        board.calc = calculate(
            sim.defaults(Decimal(50)), sim.fresh_state(Decimal(50)), reading, price
        )
    return board
