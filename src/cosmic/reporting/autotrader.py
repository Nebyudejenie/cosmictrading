"""The auto-trader page (ADR-030): what it does with the owner's money, in plain numbers.

One read model for the portal page and the Telegram /trader answer:
- the mandate in force (money, care level, mode, limits) and today's use of it;
- every trade on its account: result after fees, how it ended, auto or by hand;
- open trades with their result now and the loss if their stop is hit;
- trades waiting for the owner (ASK mode);
- the watch list: how far each market is from the rule's next breakout, with the last two
  days of the price the rule watches and the levels it acts on (the radar);
- the money planner: what each market needs at each care level, from today's volatility;
- what to expect: the rule replayed on the last 12 months with the owner's limits
  (``research/library/expect.py``), a historical simulation and labelled so.

Read-only. Every amount is in the account's currency and labelled SIMULATED with the account's
environment: the auto-trader only trades paper or demo money.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import NotFoundError
from cosmic.core.money import BPS, ZERO
from cosmic.db.schema_execution import orders, post_trade_reviews, trade_intents
from cosmic.db.schema_market import quotes
from cosmic.market import data as md
from cosmic.research.library import auto, expect, ta
from cosmic.research.library.catalog import BY_KEY, Spec
from cosmic.research.library.engine import Market
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account, list_accounts
from cosmic.trading.context_builder import account_state
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, account_ref, friendly
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import entry_held

CENT = Decimal("0.01")
LIVE_STATES = ("EXECUTING", "EXECUTED")
WAITING_STATES = ("PROPOSED", "APPROVED")


@dataclass(frozen=True)
class ClosedTrade:
    code: str
    intent_id: uuid.UUID
    market: str
    auto: bool
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    entry: Decimal | None
    exit: Decimal | None
    pnl: Decimal
    how: str  # stop-loss | rule exit | take-profit | exit

    @property
    def outcome(self) -> str:
        return "WIN" if self.pnl > 0 else ("LOSS" if self.pnl < 0 else "EVEN")


@dataclass(frozen=True)
class OpenTrade:
    code: str
    intent_id: uuid.UUID
    market: str
    auto: bool
    opened_at: datetime
    quantity: Decimal
    entry: Decimal | None
    price_now: Decimal | None
    stop: Decimal | None
    result_now: Decimal | None  # before fees
    loss_if_stopped: Decimal | None


@dataclass(frozen=True)
class Waiting:
    code: str
    intent_id: uuid.UUID
    market: str
    status: str
    summary: str
    expires_at: datetime


@dataclass(frozen=True)
class Watch:
    market: str
    key: str
    price: Decimal | None
    level: Decimal | None  # the rule buys when an hourly close goes above this
    distance: Decimal | None
    distance_pct: Decimal | None
    closeness: float  # 0 = far, 1 = at the level (for the meter)
    state: str  # IN A TRADE | BREAKOUT SOON | WATCHING | NO DATA
    smallest_value: Decimal | None  # money one lot ties up (fully paid)
    smallest_risk: Decimal | None  # loss at the stop of one lot, fees on both sides
    fits: bool | None
    chart: Radar | None = None


RADAR_HOURS = 48


@dataclass(frozen=True)
class Radar:
    """The last two days of the price the rule watches, with the levels it acts on."""

    series: str  # the signal series, e.g. "Binance BTC/USDT"
    times: list[datetime]
    closes: list[float]
    buy: list[float | None]  # each hour, the high its close had to beat to buy
    sell: list[float | None]  # each hour, the low its close had to stay above while held
    next_buy: float
    next_sell: float
    in_trade: bool
    stop: float | None  # the trade's stop, as far below this price as it is on the venue
    stop_price: Decimal | None  # the stop on the venue's own price
    entry_index: int | None  # the hour the trade was bought, when within the chart
    entry_price: Decimal | None


def series_name(key: str) -> str:
    """'SPOT:BTC/USDT@BINANCE' → 'Binance BTC/USDT'."""
    pair, _, venue = key.partition(":")[2].partition("@")
    return f"{venue.title()} {pair}".strip()


def radar(
    spec: Spec, market: Market | None, series: str, trade: OpenTrade | None, price: Decimal | None
) -> Radar | None:
    n_in, n_out = int(spec.params.get("entry", 20)), int(spec.params.get("exit", 10))
    if market is None or len(market) < n_in + 2:
        return None
    k = min(RADAR_HOURS, len(market))
    times = [t for t in market.dates[-k:] if isinstance(t, datetime)]
    closes = list(market.close[-k:])
    stop, entry_index = None, None
    if trade is not None:
        if trade.stop is not None and price is not None:
            stop = closes[-1] - float(price - trade.stop)
        before = [i for i, t in enumerate(times) if t <= trade.opened_at]
        entry_index = before[-1] if before else None
    return Radar(
        series=series_name(series),
        times=times,
        closes=closes,
        buy=ta.prior_highest(market.high, n_in)[-k:],
        sell=ta.prior_lowest(market.low, n_out)[-k:],
        next_buy=max(market.high[-n_in:]),
        next_sell=min(market.low[-n_out:]),
        in_trade=trade is not None,
        stop=stop,
        stop_price=trade.stop if trade is not None else None,
        entry_index=entry_index,
        entry_price=trade.entry if trade is not None else None,
    )


@dataclass(frozen=True)
class Totals:
    closed: int = 0
    wins: int = 0
    losses: int = 0
    total: Decimal = ZERO
    today: Decimal = ZERO
    best: Decimal | None = None
    worst: Decimal | None = None
    avg_win: Decimal | None = None
    avg_loss: Decimal | None = None
    open_result: Decimal = ZERO

    @property
    def win_rate(self) -> int | None:
        return round(100 * self.wins / self.closed) if self.closed else None


@dataclass
class Board:
    account: Account | None
    account_ref: str
    currency: str
    equity: Decimal | None
    mandate: mandates.Mandate | None
    usage: mandates.Usage | None
    rule: str
    care: str | None
    ended: mandates.Mandate | None = None  # the latest mandate, when none is active
    closed: list[ClosedTrade] = field(default_factory=list)
    open: list[OpenTrade] = field(default_factory=list)
    waiting: list[Waiting] = field(default_factory=list)
    watch: list[Watch] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    curve_times: list[datetime] = field(default_factory=list)
    curve_values: list[float] = field(default_factory=list)

    @property
    def lost_total(self) -> Decimal:
        """What the auto-trader has lost under the mandate in force (zero while ahead)."""
        return max(-self.usage.realized_total, ZERO) if self.usage is not None else ZERO


def care_of(risk_pct: Decimal | None) -> str | None:
    for name, (pct, _) in mandates.CARE_LEVELS.items():
        if risk_pct is not None and pct == risk_pct:
            return name
    return None


async def _account_for_board(
    conn: AsyncConnection, now: datetime
) -> tuple[Account | None, mandates.Mandate | None]:
    """The active mandate's account; else the latest mandate's; else the first demo/paper
    account that has auto-trader instruments (where a mandate would go)."""
    active = await mandates.active_mandates(conn, now)
    if active:
        m = active[0]
        return await get_account(conn, account_id=m.account_id), m
    recent = await mandates.recent_mandates(conn, limit=1)
    if recent:
        return await get_account(conn, account_id=recent[0].account_id), None
    for a in await list_accounts(conn):
        if a.kind in SIMULATED_ACCOUNT_KINDS and a.status == "ACTIVE":
            for key in mandates.AUTO_SIGNAL_SERIES:
                try:
                    inst = await md.instrument_by_key(conn, key)
                except NotFoundError:  # an instrument not configured here does not qualify
                    continue
                if inst.venue_key == a.venue_key and inst.tradable:
                    return a, None
    return None, None


def _how(actual: dict[str, Any]) -> str:
    """How the trade ended, from its filled exit orders."""
    filled = {
        e.get("purpose")
        for e in (actual.get("exits") or {}).values()
        if Decimal(str(e.get("filled") or 0)) > 0
    }
    if "STOP_LOSS" in filled:
        return "stop-loss"
    if "TAKE_PROFIT" in filled:
        return "take-profit"
    if "EXIT" in filled:
        return "exit"
    return "closed"


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except ArithmeticError:
        return None


async def build(conn: AsyncConnection, now: datetime) -> Board:
    account, m = await _account_for_board(conn, now)
    if account is None:
        return Board(None, "", "", None, None, None, "", None)
    ref = account_ref(account.alias, account.kind, account.broker_adapter)
    state = await account_state(conn, account, now)
    usage = await mandates.usage(conn, m, now) if m is not None else None
    rule_key = m.strategy_key if m is not None else next(iter(mandates.AUTO_RULES))
    board = Board(
        account=account,
        account_ref=ref,
        currency=account.base_currency,
        equity=state.equity,
        mandate=m,
        usage=usage,
        rule=mandates.AUTO_RULES.get(rule_key, rule_key),
        care=care_of(m.risk_pct) if m is not None else None,
    )
    await _trades(conn, board, account, now)
    if m is not None:
        board.watch = await watch_list(conn, m, account, now, board.open)
    else:
        latest = await mandates.recent_mandates(conn, limit=1)
        board.ended = latest[0] if latest and latest[0].account_id == account.id else None
    return board


async def _trades(conn: AsyncConnection, board: Board, account: Account, now: datetime) -> None:
    rows = list(
        await conn.execute(
            select(trade_intents, post_trade_reviews.c.realized_pnl, post_trade_reviews.c.actual)
            .add_columns(post_trade_reviews.c.created_at.label("closed_at"))
            .outerjoin(post_trade_reviews, post_trade_reviews.c.intent_id == trade_intents.c.id)
            .where(trade_intents.c.account_id == account.id, trade_intents.c.side == "BUY")
            .order_by(trade_intents.c.created_at)
        )
    )
    keys: dict[uuid.UUID, Any] = {}
    start = mandates.day_start(now)
    cumulative = ZERO
    for r in rows:
        inst = keys.get(r.instrument_id) or await md.instrument_by_id(conn, r.instrument_id)
        keys[r.instrument_id] = inst
        is_auto = "mandate_id" in (r.details or {})
        market = friendly(inst.canonical_key)
        if r.closed_at is not None:
            actual = r.actual or {}
            pnl = (_dec(r.realized_pnl) or ZERO).quantize(CENT)
            board.closed.append(
                ClosedTrade(
                    code=r.trade_code,
                    intent_id=r.id,
                    market=market,
                    auto=is_auto,
                    opened_at=r.created_at,
                    closed_at=r.closed_at,
                    quantity=_dec(actual.get("entry_filled")) or r.quantity,
                    entry=_dec(actual.get("entry_avg_price")),
                    exit=_dec(actual.get("exit_avg_price")),
                    pnl=pnl,
                    how=_how(actual),
                )
            )
            cumulative += pnl
            board.curve_times.append(r.closed_at)
            board.curve_values.append(float(cumulative))
            continue
        if r.status in WAITING_STATES:
            board.waiting.append(
                Waiting(r.trade_code, r.id, market, r.status, r.plain_summary, r.intent_expires_at)
            )
            continue
        if r.status not in LIVE_STATES:
            continue
        held = await entry_held(conn, r.id)
        if held <= ZERO:
            continue
        entry_price = await conn.scalar(
            select(orders.c.avg_fill_price).where(
                orders.c.intent_id == r.id, orders.c.purpose == "ENTRY"
            )
        )
        stop = _dec(((r.management_plan or {}).get("stop_loss") or {}).get("stop_price"))
        q = await md.latest_quote(conn, inst.id, now)
        result = (held * (q.bid - entry_price)).quantize(CENT) if q.bid and entry_price else None
        risk = (held * (entry_price - stop)).quantize(CENT) if stop and entry_price else None
        board.open.append(
            OpenTrade(
                r.trade_code,
                r.id,
                market,
                is_auto,
                r.created_at,
                held,
                entry_price,
                q.bid,
                stop,
                result,
                risk,
            )
        )
    pnls = [t.pnl for t in board.closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    board.totals = Totals(
        closed=len(pnls),
        wins=len(wins),
        losses=len(losses),
        total=sum(pnls, ZERO),
        today=sum((t.pnl for t in board.closed if t.closed_at >= start), ZERO),
        best=max(pnls) if pnls else None,
        worst=min(pnls) if pnls else None,
        avg_win=(sum(wins, ZERO) / len(wins)).quantize(CENT) if wins else None,
        avg_loss=(sum(losses, ZERO) / len(losses)).quantize(CENT) if losses else None,
        open_result=sum((t.result_now or ZERO for t in board.open), ZERO),
    )
    board.closed.reverse()  # newest first on the page


async def instrument_needs(
    conn: AsyncConnection, key: str, now: datetime, strategy_key: str = "turtle_20_10"
) -> tuple[Decimal | None, Decimal | None, Any, Any]:
    """(money one lot ties up, loss at the stop of one lot, hourly market, instrument) at
    today's prices and volatility, as the auto-trader would size it."""
    inst = await md.instrument_by_key(conn, key)
    market = await auto.load_hourly(conn, mandates.AUTO_SIGNAL_SERIES[key])
    q = await md.latest_quote(conn, inst.id, now)
    lot = inst.min_qty or inst.lot_size
    spec = BY_KEY.get(strategy_key)
    if market is None or q.ask is None or lot is None or spec is None:
        return None, None, market, inst
    w = auto.watch(spec, market)
    if w is None:
        return None, None, market, inst
    _, policy = await active_policy(conn)
    entry = q.ask * Decimal("1.001")
    stop_distance = auto.STOP_ATR * Decimal(str(round(w[2], 8)))
    fees = 2 * entry * policy.fee_bps_for(inst.venue_key) / BPS
    return (lot * entry).quantize(CENT), (lot * (stop_distance + fees)).quantize(CENT), market, inst


async def watch_list(
    conn: AsyncConnection,
    m: mandates.Mandate,
    account: Account,
    now: datetime,
    open_trades: list[OpenTrade],
) -> list[Watch]:
    out: list[Watch] = []
    spec = BY_KEY.get(m.strategy_key)
    held = {t.market: t for t in open_trades}
    for key in m.instruments:
        value, risk, market, inst = await instrument_needs(conn, key, now, m.strategy_key)
        q = await md.latest_quote(conn, inst.id, now)
        w = auto.watch(spec, market) if spec is not None and market is not None else None
        if w is None or q.bid is None:
            out.append(
                Watch(
                    friendly(key), key, q.bid, None, None, None, 0.0, "NO DATA", value, risk, None
                )
            )
            continue
        level, _, atr = w
        price = (q.bid + q.ask) / 2 if q.ask is not None else q.bid
        assert spec is not None  # watch() needs it
        series = mandates.AUTO_SIGNAL_SERIES[key]
        chart = radar(spec, market, series, held.get(friendly(key)), price)
        lvl = Decimal(str(round(level, 8)))
        distance = (lvl - price).quantize(CENT)
        pct = (distance / price * 100).quantize(CENT) if price else None
        # 1 at the level, 0 at three ATRs (or more) below it
        closeness = max(0.0, min(1.0, 1 - float(distance) / (3 * atr))) if atr > 0 else 0.0
        if friendly(key) in held:
            status = "IN A TRADE"
        elif float(distance) <= auto.HEADS_UP_ATR * atr:
            status = "BREAKOUT SOON"
        else:
            status = "WATCHING"
        fits = None
        if risk is not None and value is not None:
            fits = risk <= m.max_loss_per_trade and (m.capital is None or value <= m.capital)
        out.append(
            Watch(
                friendly(key),
                key,
                price,
                lvl,
                distance,
                pct,
                closeness,
                status,
                value,
                risk,
                fits,
                chart,
            )
        )
    return out


@dataclass(frozen=True)
class PlanRow:
    market: str
    key: str
    smallest_value: Decimal | None
    smallest_risk: Decimal | None
    minimum: dict[str, Decimal | None]  # care level → the least money that trades it
    fits: bool | None  # with the chosen money and care level


@dataclass(frozen=True)
class Plan:
    money: Decimal | None
    care: str
    mode: str
    per_trade: Decimal | None
    daily_stop: Decimal | None
    total_stop: Decimal | None
    rows: list[PlanRow]
    account: Account | None
    account_ref: str
    equity: Decimal | None


async def plan(
    conn: AsyncConnection, now: datetime, money: Decimal | None, care: str, mode: str
) -> Plan:
    """What a mandate from ``money`` and ``care`` would allow, market by market."""
    account, _ = await _account_for_board(conn, now)
    rows: list[PlanRow] = []
    keys: list[str] = []
    if account is not None:
        for key in mandates.AUTO_SIGNAL_SERIES:
            try:
                inst = await md.instrument_by_key(conn, key)
            except NotFoundError:  # not configured here
                continue
            if inst.venue_key == account.venue_key and inst.tradable:
                keys.append(key)
    pct = mandates.CARE_LEVELS.get(care, mandates.CARE_LEVELS["NORMAL"])[0]
    per_trade = (money * pct / 100).quantize(CENT) if money else None
    for key in keys:
        value, risk, _, _ = await instrument_needs(conn, key, now)
        minimum: dict[str, Decimal | None] = {}
        for name, (p, _) in mandates.CARE_LEVELS.items():
            minimum[name] = (
                max(value, (risk * 100 / p).quantize(CENT))
                if value is not None and risk is not None
                else None
            )
        fits = None
        if money is not None and minimum.get(care) is not None:
            fits = money >= minimum[care]  # type: ignore[operator]
        rows.append(PlanRow(friendly(key), key, value, risk, minimum, fits))
    equity = (await account_state(conn, account, now)).equity if account is not None else None
    return Plan(
        money=money,
        care=care,
        mode=mode,
        per_trade=per_trade,
        daily_stop=per_trade * mandates.DAILY_STOP_TRADES if per_trade is not None else None,
        total_stop=per_trade * mandates.TOTAL_STOP_TRADES if per_trade is not None else None,
        rows=rows,
        account=account,
        account_ref=account_ref(account.alias, account.kind, account.broker_adapter)
        if account is not None
        else "",
        equity=equity,
    )


# ---- what to expect: the rule replayed on the last 12 months ----------------------------------

EXPECT_DAYS = 365
EXPECT_BARS = 24 * (EXPECT_DAYS + 25)  # the year and the rule's warm-up before it
MIN_EXPECT_DAYS = 30
WARM_UP = timedelta(days=2)
SPREAD_WINDOW = timedelta(hours=24)
ASSUMED_SPREAD = Decimal("0.001")  # 0.1 %: ASSUMPTION where no venue quotes are stored


@dataclass(frozen=True)
class Cost:
    market: str
    fee_pct: Decimal  # per fill: the risk policy's assumption for the venue
    spread_pct: Decimal  # the whole spread
    spread_measured: bool  # from the venue's own quotes; else ASSUMED_SPREAD


@dataclass(frozen=True)
class Expectation:
    summary: expect.Summary | None
    money: Decimal | None
    per_trade: Decimal
    markets: list[str]
    series: list[str]  # the prices replayed (the auto-trader's signal series)
    costs: list[Cost]
    total_stop: Decimal | None = None
    note: str = ""  # why there is no summary
    slippage_pct: Decimal = Decimal(str(expect.SLIPPAGE * 100))

    @property
    def total_pct(self) -> Decimal | None:
        s = self.summary
        if s is None or not self.money:
            return None
        return (Decimal(str(s.total)) / self.money * 100).quantize(CENT)

    @property
    def period(self) -> str:
        """The span replayed, in words: "the last 12 months" once a year is stored."""
        s = self.summary
        if s is None or s.days >= EXPECT_DAYS - 5:
            return "the last 12 months"
        return f"the last {s.days} days"

    @property
    def verdict(self) -> str:
        s = self.summary
        if s is None:
            return ""
        if s.trades == 0:
            return "With these settings it would not have traded at all."
        if s.stopped_at is not None:
            return (
                f"It reached your total loss limit on {s.stopped_at:%d %b %Y} and switched itself "
                "off there, so it lost nothing more."
            )
        if s.total > 0 and s.first_half > 0 and s.second_half > 0:
            return f"It made money over {self.period}, and in both halves of that time."
        if s.total > 0:
            return (
                f"It made money over {self.period}, but not in both halves: results come in waves."
            )
        return f"It lost money over {self.period}, after costs."


async def measured_spread(
    conn: AsyncConnection, instrument_id: uuid.UUID, now: datetime
) -> Decimal | None:
    """The median spread of the venue's own quotes over the last day, as a fraction of the
    price, or None when there are none."""
    mid = (quotes.c.ask + quotes.c.bid) / 2
    value = await conn.scalar(
        select(func.percentile_cont(0.5).within_group((quotes.c.ask - quotes.c.bid) / mid)).where(
            quotes.c.instrument_id == instrument_id,
            quotes.c.source_ts > now - SPREAD_WINDOW,
            quotes.c.source_ts <= now,
            quotes.c.bid > 0,
            quotes.c.ask >= quotes.c.bid,
        )
    )
    return Decimal(str(round(value, 8))) if value is not None else None


async def expectation(
    conn: AsyncConnection,
    now: datetime,
    account: Account,
    keys: tuple[str, ...] | list[str],
    *,
    per_trade: Decimal,
    daily_stop: Decimal,
    money: Decimal | None,
    trades_per_day: int = mandates.MONEY_TRADES_PER_DAY,
    open_positions: int = mandates.MONEY_OPEN_POSITIONS,
    total_stop: Decimal | None = None,
    strategy_key: str = "turtle_20_10",
) -> Expectation:
    """The auto-trader's rule replayed on the last 12 months of its signal series, with these
    limits and the venue's costs (a historical simulation: ``expect.py``)."""
    spec = BY_KEY[strategy_key]
    _, policy = await active_policy(conn)
    venues: list[expect.Venue] = []
    costs: list[Cost] = []
    names: list[str] = []
    series: list[str] = []
    for key in keys:
        try:
            inst = await md.instrument_by_key(conn, key)
        except NotFoundError:
            continue
        signal = mandates.AUTO_SIGNAL_SERIES.get(key)
        market = await auto.load_hourly(conn, signal, EXPECT_BARS) if signal else None
        if market is None or inst.lot_size is None:
            continue
        fee = policy.fee_bps_for(inst.venue_key) / BPS
        measured = await measured_spread(conn, inst.id, now)
        spread = measured if measured is not None else ASSUMED_SPREAD
        venues.append(
            expect.Venue(
                key=key,
                bars=market,
                lot=float(inst.lot_size),
                min_qty=float(inst.min_qty or inst.lot_size),
                fee=float(fee),
                half_spread=float(spread) / 2,
                market_stop=account.broker_adapter in auto.MARKET_STOP_ADAPTERS,
            )
        )
        costs.append(Cost(friendly(key), fee * 100, spread * 100, measured is not None))
        names.append(friendly(key))
        series.append(series_name(signal or key))
    out = Expectation(None, money, per_trade, names, series, costs, total_stop)
    if not venues:
        return replace(out, note="No hourly prices are stored yet to test the rule on.")
    first = max(v.bars.dates[0] for v in venues)
    assert isinstance(first, datetime)
    start = max(now - timedelta(days=EXPECT_DAYS), first + WARM_UP)
    if (now - start).days < MIN_EXPECT_DAYS:
        return replace(
            out,
            note=(
                f"Only {(now - first).days} days of hourly prices are stored so far. About a "
                "year is being fetched; this test appears within a few hours."
            ),
        )
    limits = expect.Limits(
        per_trade=float(per_trade),
        daily_stop=float(daily_stop),
        trades_per_day=trades_per_day,
        open_positions=open_positions,
        capital=float(money) if money else None,
        max_order_notional=float(policy.max_order_notional),
        total_stop=float(total_stop) if total_stop is not None else None,
    )
    result = expect.simulate(venues, limits, spec, start=start)
    return replace(out, summary=expect.summarize(result))


async def mandate_expectation(
    conn: AsyncConnection, now: datetime, board: Board
) -> Expectation | None:
    """What to expect with the mandate in force."""
    m = board.mandate
    if m is None or board.account is None:
        return None
    return await expectation(
        conn,
        now,
        board.account,
        m.instruments,
        per_trade=m.max_loss_per_trade,
        daily_stop=m.daily_loss_limit,
        money=m.capital,
        trades_per_day=m.max_trades_per_day,
        open_positions=m.max_open_positions,
        total_stop=m.total_limit,
        strategy_key=m.strategy_key,
    )


async def plan_expectation(conn: AsyncConnection, now: datetime, p: Plan) -> Expectation | None:
    """What to expect with a plan, on every market it lists."""
    if p.account is None or p.money is None or p.per_trade is None or p.daily_stop is None:
        return None
    return await expectation(
        conn,
        now,
        p.account,
        [r.key for r in p.rows],
        per_trade=p.per_trade,
        daily_stop=p.daily_stop,
        money=p.money,
        total_stop=p.total_stop,
    )


def telegram_lines(b: Board, now: datetime, e: Expectation | None = None) -> list[str]:
    """The /trader answer: a few lines a phone shows without scrolling."""
    if b.account is None:
        return ["No paper or demo account can run the auto-trader yet."]
    m = b.mandate
    head = (
        f"Auto-trader {'ASKS FIRST' if m.mode == 'ASK' else 'ON'} on {b.account_ref}"
        if m is not None
        else f"Auto-trader OFF ({b.account_ref}). Start it in the portal: Auto-trader."
    )
    lines = [head]
    if m is None and b.ended is not None and b.ended.status == "STOPPED":
        lines.append(f"It stopped itself: {b.ended.end_reason}.")
    if m is not None:
        money = f"{m.capital:,.2f} {b.currency}" if m.capital is not None else "the account"
        lost = b.lost_total
        lines.append(
            f"Money: {money} · loss per trade ≤ {m.max_loss_per_trade:,.2f} · daily stop "
            f"{m.daily_loss_limit:,.2f} · stops for good after losing {m.total_limit:,.2f} "
            f"(lost so far {lost:,.2f}) · until {m.expires_at:%Y-%m-%d}"
        )
    t = b.totals
    lines.append(
        f"Result: {t.total:+,.2f} total · {t.today:+,.2f} today · {t.closed} closed "
        f"({t.wins} won, {t.losses} lost)"
        + (f" · win rate {t.win_rate} %" if t.win_rate is not None else "")
    )
    for o in b.open:
        res = f"{o.result_now:+,.2f}" if o.result_now is not None else "?"
        lines.append(f"Open: {o.market} {o.quantity} · now {res} · stop {o.stop}")
    for w in b.watch:
        if w.distance is not None:
            lines.append(
                f"{w.market}: {w.state.lower()}, breakout at {w.level:,.2f} (+{w.distance:,.2f})"
            )
    if b.waiting:
        lines.append(f"{len(b.waiting)} trade(s) wait for you: /approvals")
    if e is not None and e.summary is not None:
        s = e.summary
        won = f", {s.win_rate} % won" if s.win_rate is not None else ""
        lines.append(
            f"Past test of these settings over {e.period}: {s.total:+,.2f} ({s.trades} trades"
            f"{won}, deepest drop {s.max_drawdown:+,.2f}). HISTORICAL SIMULATION, not a promise."
        )
    lines.append("SIMULATED · demo money. Results are not a promise of future results.")
    return lines


def since(when: datetime, now: datetime) -> str:
    s = int((now - when).total_seconds())
    if s < 3600:
        return f"{max(s, 0) // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


__all__ = ["Board", "Plan", "build", "plan", "since", "telegram_lines", "timedelta"]
