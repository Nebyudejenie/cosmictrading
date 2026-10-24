"""The auto-trader page (ADR-030): what it does with the owner's money, in plain numbers.

One read model for the portal page and the Telegram /trader answer, per account (each
account may have its own auto-trader, hourly or daily):
- the mandate in force (money, care level, mode, limits) and today's use of it;
- every trade on its account: result after fees, how it ended, auto or by hand;
- open trades with their result now and the loss if their stop is hit;
- trades waiting for the owner (ASK mode);
- the watch list: how far each market is from the rule's next breakout, with the last two
  days of the price the rule watches and the levels it acts on (the radar);
- the money planner: what each market needs at each care level, from today's volatility;
- what to expect: the rule replayed with the owner's limits on the last 12 months of hourly
  bars, or on up to five years of daily bars (``research/library/expect.py``), a historical
  simulation and labelled so.

Read-only. Every amount is in the account's currency and labelled SIMULATED with the account's
environment: the auto-trader only trades paper or demo money.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, NotFoundError
from cosmic.core.money import BPS, ZERO
from cosmic.db.schema_execution import orders, post_trade_reviews, trade_intents
from cosmic.market import data as md
from cosmic.research.library import auto, expect, ta
from cosmic.research.library.catalog import BY_KEY, Spec
from cosmic.research.library.engine import Market
from cosmic.trading import mandates, venue_terms
from cosmic.trading.accounts import Account, get_account, list_accounts
from cosmic.trading.context_builder import account_state
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, account_ref, friendly
from cosmic.trading.policy import RiskPolicyParams, active_policy
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
    level: Decimal | None  # a breakout rule buys when a close goes above this
    distance: Decimal | None
    distance_pct: Decimal | None
    closeness: float  # 0 = far, 1 = at the level (for the meter)
    state: str  # IN A TRADE | BREAKOUT SOON | WATCHING | WAITING FOR THE RULE | NO DATA
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
    entry_index: int | None  # the bar the trade was bought on, when within the chart
    entry_price: Decimal | None
    entry_bars: int = 20  # the breakout: a close above the highest high of this many bars
    exit_bars: int = 10  # the exit: a close below the lowest low of this many bars


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
        entry_bars=n_in,
        exit_bars=n_out,
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
    sizing: mandates.Sizing | None = None  # full size, or half after a deep drop
    timeframe: str = mandates.HOURLY  # the bars its rule runs on
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

    @property
    def bars(self) -> str:
        """The bars its rule runs on, in words."""
        return mandates.TIMEFRAMES[self.timeframe]


def care_of(risk_pct: Decimal | None) -> str | None:
    for name, (pct, _) in mandates.CARE_LEVELS.items():
        if risk_pct is not None and pct == risk_pct:
            return name
    return None


async def _account_for_board(
    conn: AsyncConnection, now: datetime, alias: str | None = None
) -> tuple[Account | None, mandates.Mandate | None]:
    """The account a page is about: ``alias`` when it names a demo or paper account (with its
    active mandate, if any); else the oldest active mandate's account; else the latest
    mandate's; else the first demo/paper account that has auto-trader instruments (where a
    mandate would go)."""
    active = await mandates.active_mandates(conn, now)
    if alias:
        try:
            chosen = await get_account(conn, alias=alias)
        except NotFoundError:
            chosen = None
        if chosen is not None and chosen.kind in SIMULATED_ACCOUNT_KINDS:
            return chosen, next((m for m in active if m.account_id == chosen.id), None)
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


async def build(conn: AsyncConnection, now: datetime, account: str | None = None) -> Board:
    """The board of one account (``account``: its alias), or of the first auto-trader."""
    acct, m = await _account_for_board(conn, now, account)
    if acct is None:
        return Board(None, "", "", None, None, None, "", None)
    ref = account_ref(acct.alias, acct.kind, acct.broker_adapter)
    state = await account_state(conn, acct, now)
    usage = await mandates.usage(conn, m, now) if m is not None else None
    latest = await mandates.recent_mandates(conn, limit=1, account_id=acct.id)
    shown = m or (latest[0] if latest else None)  # an OFF board shows what it last ran
    rule_key = shown.strategy_key if shown is not None else next(iter(mandates.AUTO_RULES))
    board = Board(
        account=acct,
        account_ref=ref,
        currency=acct.base_currency,
        equity=state.equity,
        mandate=m,
        usage=usage,
        rule=mandates.AUTO_RULES.get(rule_key, rule_key),
        care=care_of(m.risk_pct) if m is not None else None,
        timeframe=shown.timeframe if shown is not None else mandates.HOURLY,
    )
    await _trades(conn, board, acct, now)
    if m is not None:
        board.sizing = await mandates.sizing_now(conn, m)
        board.watch = await watch_list(conn, m, acct, now, board.open)
    else:
        board.ended = latest[0] if latest else None
    return board


async def running(conn: AsyncConnection, now: datetime) -> list[dict[str, str]]:
    """Each running auto-trader, oldest first: its account, rule and bars (for a switcher)."""
    out = []
    for m in await mandates.active_mandates(conn, now):
        a = await get_account(conn, account_id=m.account_id)
        out.append(
            {
                "alias": a.alias,
                "account_ref": account_ref(a.alias, a.kind, a.broker_adapter),
                "rule": mandates.AUTO_RULES.get(m.strategy_key, m.strategy_key),
                "bars": m.bars,
            }
        )
    return out


async def board_for(conn: AsyncConnection, now: datetime, instrument: str) -> Board:
    """The board of the auto-trader that trades ``instrument``, else the first board."""
    for m in await mandates.active_mandates(conn, now):
        if instrument in m.instruments:
            a = await get_account(conn, account_id=m.account_id)
            return await build(conn, now, a.alias)
    return await build(conn, now)


async def boards(conn: AsyncConnection, now: datetime) -> list[Board]:
    """One board per running auto-trader, oldest first; else the one board ``build`` picks."""
    active = await mandates.active_mandates(conn, now)
    if not active:
        return [await build(conn, now)]
    out = []
    for m in active:
        a = await get_account(conn, account_id=m.account_id)
        out.append(await build(conn, now, a.alias))
    return out


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
    conn: AsyncConnection,
    key: str,
    now: datetime,
    timeframe: str = mandates.HOURLY,
    *,
    market_stop: bool = True,
) -> tuple[Decimal | None, Decimal | None, Any, Any]:
    """(money one lot ties up, loss at the stop of one lot, the signal's bars, instrument) at
    today's prices and volatility, as the auto-trader would size it on ``timeframe``: a stop
    3×ATR(20) of those bars below an entry at the ask + 0.1 %, its stop-limit 0.5 % lower where
    the venue does not fill stops at market, and fees on both sides."""
    inst = await md.instrument_by_key(conn, key)
    market = await auto.load_bars(conn, mandates.AUTO_SIGNAL_SERIES[key], timeframe)
    q = await md.latest_quote(conn, inst.id, now)
    lot = inst.min_qty or inst.lot_size
    if market is None or q.ask is None or lot is None:
        return None, None, market, inst
    atr = ta.atr(market.high, market.low, market.close, 20)[-1]
    if atr is None or atr <= 0:
        return None, None, market, inst
    _, policy = await active_policy(conn)
    entry = q.ask * Decimal("1.001")
    stop = entry - auto.STOP_ATR * Decimal(str(round(atr, 8)))
    stop_limit = stop if market_stop else stop * Decimal("0.995")
    fees = 2 * entry * policy.fee_bps_for(inst.venue_key) / BPS
    per_unit = entry - stop_limit + fees
    return (lot * entry).quantize(CENT), (lot * per_unit).quantize(CENT), market, inst


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
    market_stop = account.broker_adapter in auto.MARKET_STOP_ADAPTERS
    for key in m.instruments:
        value, risk, market, inst = await instrument_needs(
            conn, key, now, m.timeframe, market_stop=market_stop
        )
        q = await md.latest_quote(conn, inst.id, now)
        fits = None
        if risk is not None and value is not None:
            fits = risk <= m.max_loss_per_trade and (m.capital is None or value <= m.capital)
        if q.bid is None or market is None or spec is None:
            out.append(
                Watch(
                    friendly(key), key, q.bid, None, None, None, 0.0, "NO DATA", value, risk, None
                )
            )
            continue
        price = (q.bid + q.ask) / 2 if q.ask is not None else q.bid
        w = auto.watch(spec, market)
        if w is None:  # a trend rule: no level to watch, only whether it holds
            state = "IN A TRADE" if friendly(key) in held else "WAITING FOR THE RULE"
            out.append(
                Watch(friendly(key), key, price, None, None, None, 0.0, state, value, risk, fits)
            )
            continue
        level, _, atr = w
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
    timeframe: str = mandates.HOURLY
    rule: str = "turtle_20_10"
    policy_notes: list[str] = field(default_factory=list)  # where the risk policy binds first

    @property
    def bars(self) -> str:
        return mandates.TIMEFRAMES[self.timeframe]


def _pct_up(value: Decimal) -> Decimal:
    """A percentage rounded up to the next half."""
    return (value * 2).to_integral_value(ROUND_CEILING) / 2


def losses_to_own_stop() -> int:
    """Full losses in a row before the auto-trader's own stop for good: full size until it is
    ``HALVE_AFTER`` trades' worth below its best, half size after, until one more full loss
    would pass ``TOTAL_STOP_TRADES`` trades' worth (``mandates.stop_due``)."""
    lost, n = Decimal(0), 0
    while lost + 1 <= mandates.TOTAL_STOP_TRADES:
        lost += Decimal("0.5") if lost >= mandates.HALVE_AFTER else Decimal(1)
        n += 1
    return n


def policy_notes(
    p: RiskPolicyParams, equity: Decimal | None, money: Decimal | None, per_trade: Decimal | None
) -> list[str]:
    """Where the account's risk policy stops the auto-trader before its own limits would, at
    this money, in plain words, with the value under Risk policy that lets its own limits decide
    (the owner's decision, with a code). The policy applies to the whole account."""
    if equity is None or equity <= ZERO or money is None or per_trade is None or per_trade <= 0:
        return []
    notes: list[str] = []
    in_one = (equity * p.max_concentration_pct / 100).quantize(CENT)
    if in_one < money:
        notes.append(
            f"One market may hold at most {in_one:,.2f} ({p.max_concentration_pct} % of the "
            f"account, max_concentration_pct), so a trade can be smaller than your plan."
        )
    together = mandates.MONEY_OPEN_POSITIONS
    at_risk = (equity * p.max_open_risk_pct / 100).quantize(CENT)
    if at_risk < together * per_trade:
        fit = int(at_risk // per_trade)
        need = _pct_up(together * per_trade / equity * 100)
        notes.append(
            f"The loss if every stop is hit may be at most {at_risk:,.2f} "
            f"(max_open_risk_pct {p.max_open_risk_pct} %), so "
            + (f"only {fit} full trade at a time" if fit else "every trade is made smaller")
            + f"; {need} % would allow {together} while the first is near its entry (an open "
            "trade's risk grows as its price rises above its stop)."
        )
    own_losses = losses_to_own_stop()
    if p.max_loss_streak < own_losses:
        notes.append(
            f"After {p.max_loss_streak} losing trades in a row, the risk policy refuses every new "
            "trade on this account until you change it (max_loss_streak). The auto-trader's own "
            f"stop for good needs at least {own_losses} losing trades in a row (more when losses "
            f"are smaller than full, and half size comes first), so {own_losses} or more lets its "
            "own stop decide in most cases."
        )
    own = per_trade * mandates.TOTAL_STOP_TRADES / equity * 100
    if p.max_drawdown_pct < own:
        notes.append(
            f"A drop of {p.max_drawdown_pct} % from the account's best (max_drawdown_pct) stops "
            f"new trades before the auto-trader's own stop for good ({own:.1f} % of the account); "
            f"{_pct_up(own + 5)} % lets its own stop decide near the start. After gains, a drop "
            "from the account's best can still stop new trades first."
        )
    return notes


async def plan(
    conn: AsyncConnection,
    now: datetime,
    money: Decimal | None,
    care: str,
    mode: str,
    *,
    account: str | None = None,
    timeframe: str = mandates.HOURLY,
    rule: str | None = None,
) -> Plan:
    """What a mandate from ``money`` and ``care`` would allow, market by market, on the
    ``account`` (its alias; else the first auto-trader's) with ``rule`` on ``timeframe``."""
    if timeframe not in mandates.TIMEFRAMES:
        timeframe = mandates.HOURLY
    allowed = mandates.RULES_BY_TIMEFRAME[timeframe]
    rule = rule if rule in allowed else allowed[0]
    acct, _ = await _account_for_board(conn, now, account)
    rows: list[PlanRow] = []
    keys: list[str] = []
    if acct is not None:
        for key in mandates.AUTO_SIGNAL_SERIES:
            try:
                inst = await md.instrument_by_key(conn, key)
            except NotFoundError:  # not configured here
                continue
            if inst.venue_key == acct.venue_key and inst.tradable:
                keys.append(key)
    market_stop = acct is None or acct.broker_adapter in auto.MARKET_STOP_ADAPTERS
    pct = mandates.CARE_LEVELS.get(care, mandates.CARE_LEVELS["NORMAL"])[0]
    per_trade = (money * pct / 100).quantize(CENT) if money else None
    for key in keys:
        value, risk, _, _ = await instrument_needs(
            conn, key, now, timeframe, market_stop=market_stop
        )
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
    equity = (await account_state(conn, acct, now)).equity if acct is not None else None
    try:
        _, policy = await active_policy(conn)
        notes = policy_notes(policy, equity, money, per_trade)
    except ConflictError:  # no valid policy: every check fails closed, nothing to compare
        notes = []
    return Plan(
        money=money,
        care=care,
        mode=mode,
        per_trade=per_trade,
        daily_stop=per_trade * mandates.DAILY_STOP_TRADES if per_trade is not None else None,
        total_stop=per_trade * mandates.TOTAL_STOP_TRADES if per_trade is not None else None,
        rows=rows,
        account=acct,
        account_ref=account_ref(acct.alias, acct.kind, acct.broker_adapter)
        if acct is not None
        else "",
        equity=equity,
        timeframe=timeframe,
        rule=rule,
        policy_notes=notes,
    )


# ---- what to expect: the rule replayed on the last 12 months ----------------------------------

EXPECT_DAYS = 365
EXPECT_BARS = 24 * (EXPECT_DAYS + 25)  # the year and the rule's warm-up before it
MIN_EXPECT_DAYS = 30
WARM_UP = timedelta(days=2)
# Daily rules: up to five years, after a warm-up long enough for 12-month momentum.
EXPECT_DAILY_DAYS = 5 * 365
DAILY_WARM_UP = timedelta(days=400)
SPREAD_WINDOW = timedelta(hours=24)
ASSUMED_SPREAD = Decimal("0.001")  # 0.1 %: ASSUMPTION where no venue quotes are stored


@dataclass(frozen=True)
class Cost:
    market: str
    fee_pct: Decimal  # per fill: the risk policy's assumption for the venue
    spread_pct: Decimal  # the whole spread
    spread_measured: bool  # from the venue's own quotes; else ASSUMED_SPREAD
    swap: venue_terms.Swap | None = None  # the broker's overnight swap, when reported


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
    timeframe: str = mandates.HOURLY
    rule: str = ""  # the rule's name

    @property
    def daily(self) -> bool:
        return self.timeframe == mandates.DAILY

    @property
    def bar_word(self) -> str:
        """'hourly' or 'daily', for "… hourly prices"."""
        return "daily" if self.daily else "hourly"

    @property
    def unit(self) -> str:
        return "days" if self.daily else "hours"

    @property
    def total_pct(self) -> Decimal | None:
        s = self.summary
        if s is None or not self.money:
            return None
        return (Decimal(str(s.total)) / self.money * 100).quantize(CENT)

    @property
    def period(self) -> str:
        """The span replayed, in words: "the last 12 months" once a year of hourly bars is
        stored; the years replayed for a daily rule."""
        s = self.summary
        if self.daily:
            return f"the years {s.start:%b %Y} to {s.end:%b %Y}" if s else "the stored years"
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


async def expectation(
    conn: AsyncConnection,
    now: datetime,
    account: Account | None,
    keys: tuple[str, ...] | list[str],
    *,
    per_trade: Decimal,
    daily_stop: Decimal,
    money: Decimal | None,
    trades_per_day: int = mandates.MONEY_TRADES_PER_DAY,
    open_positions: int = mandates.MONEY_OPEN_POSITIONS,
    total_stop: Decimal | None = None,
    strategy_key: str = "turtle_20_10",
    timeframe: str = mandates.HOURLY,
    equity: Decimal | None = None,
    market_stop: bool | None = None,
) -> Expectation:
    """The auto-trader's rule replayed on its signal series with these limits and the venue's
    costs (a historical simulation: ``expect.py``): the last 12 months of hourly bars, or up
    to five years of daily bars."""
    spec = BY_KEY[strategy_key]
    hourly = timeframe == mandates.HOURLY
    # Without an account (an account still to open, S37), ``equity`` and ``market_stop`` say
    # what it would be.
    if market_stop is None:
        market_stop = account is not None and account.broker_adapter in auto.MARKET_STOP_ADAPTERS
    if equity is None and account is not None:
        equity = (await account_state(conn, account, now)).equity
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
        market = (
            await auto.load_bars(conn, signal, timeframe, EXPECT_BARS if hourly else None)
            if signal
            else None
        )
        if market is None or inst.lot_size is None:
            continue
        fee = policy.fee_bps_for(inst.venue_key) / BPS
        measured = await md.median_spread(conn, inst.id, now, SPREAD_WINDOW)
        spread = measured if measured is not None else ASSUMED_SPREAD
        swap = swap_for(inst, market.close[-1])
        venues.append(
            expect.Venue(
                key=key,
                bars=market,
                lot=float(inst.lot_size),
                min_qty=float(inst.min_qty or inst.lot_size),
                fee=float(fee),
                half_spread=float(spread) / 2,
                market_stop=market_stop,
                swap=swap.long if swap else 0.0,
                swap_triple=swap_triple(inst, swap),
                weekend_closed=inst.base in venue_terms.METALS,
            )
        )
        costs.append(Cost(friendly(key), fee * 100, spread * 100, measured is not None, swap))
        names.append(friendly(key))
        series.append(series_name(signal or key))
    out = Expectation(
        None,
        money,
        per_trade,
        names,
        series,
        costs,
        total_stop,
        timeframe=timeframe,
        rule=mandates.AUTO_RULES.get(strategy_key, spec.name),
    )
    if not venues:
        return replace(out, note=f"No {out.bar_word} prices are stored yet to test the rule on.")
    first = max(v.bars.dates[0] for v in venues)
    assert isinstance(first, datetime)
    if hourly:
        start = max(now - timedelta(days=EXPECT_DAYS), first + WARM_UP)
    else:
        start = max(now - timedelta(days=EXPECT_DAILY_DAYS), first + DAILY_WARM_UP)
    if (now - start).days < MIN_EXPECT_DAYS:
        return replace(
            out,
            note=(
                f"Only {(now - first).days} days of hourly prices are stored so far. About a "
                "year is being fetched; this test appears within a few hours."
                if hourly
                else f"Only {(now - first).days} days of daily prices are stored so far; the "
                f"test needs {DAILY_WARM_UP.days + MIN_EXPECT_DAYS}: {DAILY_WARM_UP.days} to "
                f"warm up the rule's averages, then at least {MIN_EXPECT_DAYS} to test."
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
        halve_after=float(mandates.HALVE_AFTER),
        restore_below=float(mandates.RESTORE_BELOW),
        # the account's risk policy, as its checks apply it (S36)
        equity=float(equity) if equity is not None else None,
        concentration=float(policy.max_concentration_pct) / 100,
        open_risk=float(policy.max_open_risk_pct) / 100,
        max_drawdown=float(policy.max_drawdown_pct) / 100,
        loss_streak=policy.max_loss_streak,
        max_daily_loss=float(policy.max_daily_loss),
    )
    result = expect.simulate(venues, limits, spec, start=start)
    return replace(out, summary=expect.summarize(result))


def swap_triple(inst: md.InstrumentView, swap: venue_terms.Swap | None) -> int | None:
    """The triple-swap weekday for a market closed at weekends (metals); None for markets
    that trade every day, which pay every night."""
    return swap.triple_weekday if swap is not None and inst.base in venue_terms.METALS else None


def swap_for(inst: md.InstrumentView, price: float) -> venue_terms.Swap | None:
    """The venue's swap as the replay applies it: a market that trades every day has no
    triple day."""
    swap = venue_terms.swap_per_night(inst.metadata, price)
    if swap is not None and swap_triple(inst, swap) is None:
        swap = replace(swap, triple_day=None)
    return swap


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
        timeframe=m.timeframe,
    )


async def plan_expectation(
    conn: AsyncConnection, now: datetime, p: Plan, keys: list[str] | None = None
) -> Expectation | None:
    """What to expect with a plan, on the chosen markets (``keys``, those of the plan) or on
    every market it lists."""
    if p.account is None or p.money is None or p.per_trade is None or p.daily_stop is None:
        return None
    listed = [r.key for r in p.rows]
    chosen = [k for k in keys or [] if k in listed]
    return await expectation(
        conn,
        now,
        p.account,
        chosen or listed,
        per_trade=p.per_trade,
        daily_stop=p.daily_stop,
        money=p.money,
        total_stop=p.total_stop,
        strategy_key=p.rule,
        timeframe=p.timeframe,
    )


def telegram_lines(b: Board, now: datetime, e: Expectation | None = None) -> list[str]:
    """The /trader answer: a few lines a phone shows without scrolling."""
    if b.account is None:
        return ["No paper or demo account can run the auto-trader yet."]
    m = b.mandate
    head = (
        f"Auto-trader {'ASKS FIRST' if m.mode == 'ASK' else 'ON'} on {b.account_ref}: "
        f"{b.rule} on {b.bars}"
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
        if b.sizing is not None and b.sizing.half:
            lines.append(
                f"Trade size: HALF, {b.sizing.drop:,.2f} below its best result; full again below "
                f"{mandates.RESTORE_BELOW * m.max_loss_per_trade:,.2f}."
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


__all__ = ["Board", "Plan", "boards", "build", "plan", "since", "telegram_lines", "timedelta"]
