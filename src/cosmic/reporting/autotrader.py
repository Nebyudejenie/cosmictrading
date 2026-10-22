"""The auto-trader page (ADR-030): what it does with the owner's money, in plain numbers.

One read model for the portal page and the Telegram /trader answer:
- the mandate in force (money, care level, mode, limits) and today's use of it;
- every trade on its account: result after fees, how it ended, auto or by hand;
- open trades with their result now and the loss if their stop is hit;
- trades waiting for the owner (ASK mode);
- the watch list: how far each market is from the rule's next breakout;
- the money planner: what each market needs at each care level, from today's volatility.

Read-only. Every amount is in the account's currency and labelled SIMULATED with the account's
environment: the auto-trader only trades paper or demo money.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import NotFoundError
from cosmic.core.money import BPS, ZERO
from cosmic.db.schema_execution import orders, post_trade_reviews, trade_intents
from cosmic.market import data as md
from cosmic.research.library import auto
from cosmic.research.library.catalog import BY_KEY
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
    closed: list[ClosedTrade] = field(default_factory=list)
    open: list[OpenTrade] = field(default_factory=list)
    waiting: list[Waiting] = field(default_factory=list)
    watch: list[Watch] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    curve_times: list[datetime] = field(default_factory=list)
    curve_values: list[float] = field(default_factory=list)


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
    held = {t.market for t in open_trades}
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
                friendly(key), key, price, lvl, distance, pct, closeness, status, value, risk, fits
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
        rows=rows,
        account=account,
        account_ref=account_ref(account.alias, account.kind, account.broker_adapter)
        if account is not None
        else "",
        equity=equity,
    )


def telegram_lines(b: Board, now: datetime) -> list[str]:
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
    if m is not None:
        money = f"{m.capital:,.2f} {b.currency}" if m.capital is not None else "the account"
        lines.append(
            f"Money: {money} · loss per trade ≤ {m.max_loss_per_trade:,.2f} · daily stop "
            f"{m.daily_loss_limit:,.2f} · until {m.expires_at:%Y-%m-%d}"
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
