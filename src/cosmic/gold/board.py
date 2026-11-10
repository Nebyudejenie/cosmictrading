"""My Gold Auto-Trader, as data (S62, S63): the account, its money, the reading of gold now, its
open and closed trades, its recent decisions, its honest status, and everything the system
calculates from the owner's amount. SIMULATED throughout."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import ZERO
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_gold import gold_hfm_decisions, gold_sim_accounts, gold_sim_decisions
from cosmic.db.schema_trading import accounts, bridge_state
from cosmic.gold import analysis, daily, hfm_costs, hfm_demo, hfm_selftest, sim
from cosmic.market import data as md
from cosmic.trading import mandates
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.mt5_bridge import ADAPTER_2, BRIDGE_2
from cosmic.trading.policy import active_policy

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
    hfm_unit = (
        stop + price.ask * hfm_demo.MARKUP if stop is not None and price is not None else stop
    )
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
        # HFM's 1 ounce: its stop distance and the 0.1 % limit margin a buy is sent with
        hfm_risk=(HFM_SMALLEST * hfm_unit).quantize(CENT) if hfm_unit is not None else None,
        hfm_from=(HFM_SMALLEST * hfm_unit * 100 / s.risk_pct).quantize(CENT, ROUND_CEILING)
        if hfm_unit is not None
        else None,
    )


async def start_view(
    conn: AsyncConnection, reading: analysis.Reading, price: sim.Price | None
) -> tuple[analysis.Reading, sim.Price | None]:
    """What START sizes HFM's smallest trade with: the reading and price now, or, while the
    market is closed (the weekend), HFM's last price and the last hourly stop distance. Too
    small an amount is then refused with the reason on a Saturday as on a weekday (S72: a
    START with 100 went through while gold was closed). An estimate only: nothing trades on it."""
    if price is not None and reading.stop_distance is not None:
        return reading, price
    return (
        replace(reading, stop_distance=analysis.last_stop(reading)),
        price or await sim.last_price(conn),
    )


@dataclass(frozen=True)
class HfmPreview:
    """A trade now on the HFM demo, by HFM's rules (whole ounces, the fee both ways)."""

    ounces: Decimal
    why: str | None
    entry: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    fee_bps: Decimal | None = None
    fee_source: str = ""
    costs: hfm_costs.HfmCosts | None = None


async def hfm_preview(
    conn: AsyncConnection, s: sim.Settings, reading: analysis.Reading, price: sim.Price | None
) -> HfmPreview:
    costs = await hfm_costs.measure(conn)
    _, policy = await active_policy(conn)
    fee, source = hfm_costs.fee_bps(policy, costs)
    if price is None or reading.stop_distance is None:
        return HfmPreview(
            ZERO,
            "no current HFM price, or no stop distance from the hourly candles",
            fee_bps=fee,
            fee_source=source,
            costs=costs,
        )
    inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
    tick = inst.tick_size or CENT
    entry = hfm_demo._tick(price.ask * (1 + hfm_demo.MARKUP), tick, ROUND_CEILING)
    stop = hfm_demo._tick(price.ask - reading.stop_distance, tick)
    max_loss = (s.amount * s.risk_pct / 100).quantize(CENT, ROUND_DOWN)
    qty, why = hfm_demo.size(
        max_loss,
        s.risk_pct,
        s.amount,
        entry,
        stop,
        fee,
        inst.lot_size or Decimal(1),
    )
    target = hfm_demo._tick(price.ask + 2 * reading.stop_distance, tick)
    return HfmPreview(qty, why, entry, stop, target, fee, source, costs)


@dataclass
class HfmTrader:
    """My Gold Auto-Trader in HFM Demo mode: its mandate, its recent decisions, and its status."""

    mandate: Any
    decisions: list[Any]
    held: int
    waiting: Any | None  # a proposal of its own waiting for the owner's approval
    status: tuple[str, str]
    selftest: Any | None = None  # the latest self-test run (S67)


async def hfm_trader(conn: AsyncConnection, now: datetime, feed: str) -> HfmTrader | None:
    found = await hfm_demo.gold_mandates(conn, now)
    if not found:
        return None
    m = found[0]
    decisions = list(
        await conn.execute(
            select(gold_hfm_decisions)
            .where(gold_hfm_decisions.c.mandate_id == m.id)
            .order_by(desc(gold_hfm_decisions.c.bar))
            .limit(30)
        )
    )
    account = await get_account(conn, account_id=m.account_id)
    inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
    held = len(await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)}))
    waiting = (
        await conn.execute(
            select(trade_intents.c.id, trade_intents.c.trade_code)
            .where(
                trade_intents.c.account_id == account.id,
                trade_intents.c.status == "PROPOSED",
                trade_intents.c.details["mandate_id"].astext == str(m.id),
            )
            .limit(1)
        )
    ).first()
    no_price = sim.FEED_WORDS.get(feed)
    last = decisions[0].at if decisions else None
    since = m.granted_at if last is None else max(last, m.granted_at)
    if waiting is not None:
        status = (ATTENTION, f"{waiting.trade_code} waits for your approval in Approvals")
    elif feed in (sim.BRIDGE_SILENT, sim.NOT_DEMO, sim.NO_PRICE):
        status = (ATTENTION, f"{no_price}: no new trade; HFM holds the stops of open ones")
    elif feed == sim.FEED_OK and now - since > DECIDES_EVERY:
        status = (ATTENTION, "no decision for over 3 minutes while HFM's market is open")
    elif held:
        status = (RUNNING, f"{held} trade(s) open at HFM, each with its stop-loss held by HFM")
    elif feed == sim.MARKET_CLOSED:
        status = (WAITING, "HFM's gold market is closed; it decides again when it reopens")
    else:
        why = decisions[0].reasons[-1] if decisions and decisions[0].reasons else ""
        status = (WAITING, f"waiting for an opportunity{': ' + why if why else ''}")
    run = await hfm_selftest.latest(conn)
    if run is not None and run.finished_at is None:
        trade = {"BUY": run.buy_intent, "SELL": run.sell_intent}.get(run.phase)
        status = (
            (
                WAITING,
                "the self-test waits for HFM's gold market to open, then runs by itself: one "
                "buy, then one short, each closed by itself; nothing more to press",
            )
            if feed == sim.MARKET_CLOSED and run.phase in ("BUY", "SELL") and trade is None
            else (
                RUNNING,
                "the self-test is running: one buy, then one short, each closed by itself",
            )
        )
    return HfmTrader(m, decisions, held, waiting, status, run)


DAILY_AMOUNT = Decimal(2000)  # the owner's choice of 2026-11-10, offered at START
BRIDGE_FRESH = timedelta(seconds=60)


@dataclass
class DailyTrader:
    """The daily gold robot on the second HFM demo account (S75): its account, mandate, the
    rule's levels now, what 1 ounce would risk, its decisions and its status."""

    account: Any | None  # the second HFM demo account, once open
    mandate: Any | None
    reading: daily.DailyReading
    decisions: list[Any]
    held: int
    status: tuple[str, str]
    one_ounce: Decimal | None  # the loss of 1 ounce at the stop now, fees included
    suggested_risk: Decimal | None  # the smallest whole % of DAILY_AMOUNT that carries it


async def daily_trader(conn: AsyncConnection, now: datetime) -> DailyTrader:
    reading = await daily.reading_now(conn, now)
    acct = (
        await conn.execute(
            select(accounts.c.id).where(
                accounts.c.broker_adapter == ADAPTER_2,
                accounts.c.kind == "SANDBOX",
                accounts.c.status == "ACTIVE",
            )
        )
    ).first()
    account = None if acct is None else await get_account(conn, account_id=acct.id)
    found = await daily.daily_mandates(conn, now)
    m = found[0] if found else None
    decisions: list[Any] = []
    held = 0
    if m is not None and account is not None:
        decisions = list(
            await conn.execute(
                select(gold_hfm_decisions)
                .where(gold_hfm_decisions.c.mandate_id == m.id)
                .order_by(desc(gold_hfm_decisions.c.bar))
                .limit(20)
            )
        )
        inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
        held = len(await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)}))
    one_ounce = suggested = None
    if reading.stop_distance is not None and reading.close is not None:
        _, policy = await active_policy(conn)
        fee = 2 * reading.close * policy.fee_bps_for("HFM") / 10_000
        one_ounce = (reading.stop_distance + fee).quantize(CENT, ROUND_CEILING)
        suggested = min(
            max((one_ounce * 100 / DAILY_AMOUNT).to_integral_value(ROUND_CEILING), Decimal(1)),
            mandates.GOLD_DAILY_MAX_RISK,
        )
    seen = await conn.scalar(
        select(bridge_state.c.received_at).where(bridge_state.c.bridge == BRIDGE_2)
    )
    if account is None:
        status = (
            WAITING,
            "the second HFM demo account is not open yet: it opens once its MT5 on the Windows "
            "computer is signed in and its check has passed",
        )
    elif m is None:
        status = (READY, "the second HFM demo account is open; START it below with your code")
    elif seen is None or now - seen > BRIDGE_FRESH:
        status = (
            ATTENTION,
            "the second HFM bridge is not reporting: no new trade; HFM holds the stop of an "
            "open one",
        )
    elif held:
        status = (RUNNING, f"{held} trade open at HFM, its stop-loss held by HFM")
    else:
        status = (
            WAITING,
            "waiting for a day to close beyond its 55-day high or low; it decides once a day, "
            "when HFM's gold reopens after its daily close (around 01:00–02:00 at night in "
            "Addis Ababa)",
        )
    return DailyTrader(account, m, reading, decisions, held, status, one_ounce, suggested)


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
    hfm: HfmTrader | None = None  # HFM Demo mode, when it runs (S64)
    daily: DailyTrader | None = None  # the daily robot on the second HFM demo (S75)

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
    board.hfm = await hfm_trader(conn, now, why)
    board.daily = await daily_trader(conn, now)
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
