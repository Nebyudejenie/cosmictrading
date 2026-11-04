"""The internal gold simulation (S62, ADR-037). SIMULATED: nothing is ever sent to HFM.

The owner (2026-11-04): "I choose the amount. I choose my risk. The system calculates the
position size and handles the trading logic. Internal simulation must support fractional XAUUSD
positions and automatically execute simulated BUY and SELL trades using HFM market prices."

- **The account:** the owner's own starting balance; its balance is the sum of its ledger
  (deposit, results, swap); equity adds the open positions at HFM's current price.
- **The size:** from the owner's amount and risk per trade: at most ``risk`` % of the amount
  (never more than the equity left) may be lost at the stop; ounces in steps of 0.01 from 0.01.
  Too small: WAIT, saying so and from which amount this opportunity would trade at this risk.
  The risk is the owner's: never raised to make a trade.
- **The prices:** HFM's own bid and ask from the bridge's report, at most 15 seconds old; no
  price, nothing happens. A buy opens at the ask and closes at the bid, a sell the other way, so
  the spread is paid as at HFM.
- **Stops and targets:** checked every second; a position closes at the price that crossed them,
  so a jump past a stop loses more than planned, as at a broker.
- **Swap:** HFM's own overnight terms for gold, per night held at 17:00 New York, three nights on
  HFM's triple day. Commission: none (HFM charged none on the owner's demo deals, 2026-11-04).
- **Decisions:** once per 1-minute candle (``analysis.read``), recorded with the reasons; a
  restart never decides, or opens, twice (the decision's unique key).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_gold import (
    gold_sim_accounts,
    gold_sim_decisions,
    gold_sim_ledger,
    gold_sim_positions,
    gold_sim_settings,
)
from cosmic.db.schema_trading import bridge_state
from cosmic.gold import analysis
from cosmic.market import hfm_candles
from cosmic.trading import venue_terms

STEP = Decimal("0.01")  # ounces
SMALLEST = Decimal("0.01")
CENT = Decimal("0.01")
FRESH = timedelta(seconds=15)  # HFM's report
MARKET_MOVING = timedelta(minutes=5)  # an older tick: the market is closed or stalled
LEVERAGE_UNKNOWN = Decimal(100)  # when HFM has not said its leverage: a cautious margin
BRIDGE = "hfm-demo"
CANDLES = {"1m": 240, "5m": 120, "15m": 120, "1h": 240, "1d": 120}
OPEN, CLOSED = "OPEN", "CLOSED"


# ---- the owner's settings ---------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    amount: Decimal
    risk_pct: Decimal
    daily_loss: Decimal
    max_open_risk_pct: Decimal = Decimal(5)
    max_drawdown_pct: Decimal = Decimal(20)
    max_positions: int = 1
    take_profit_r: Decimal | None = Decimal(2)
    allow_sell: bool = True


def problems(s: Settings, balance: Decimal) -> list[str]:
    """Each setting that cannot be used, and why, in the owner's words; nothing is changed."""
    out: list[str] = []
    if not s.amount > 0:
        out.append("Trading amount: it must be more than 0.")
    elif s.amount > balance:
        out.append(
            f"Trading amount: {s.amount:,.2f} is more than the account's balance "
            f"({balance:,.2f}); the system trades only money the account has."
        )
    if not Decimal("0.1") <= s.risk_pct <= 10:
        out.append(f"Risk per trade: {s.risk_pct:f} % is outside 0.1 % to 10 %.")
    if not s.daily_loss > 0:
        out.append("Most a day may lose: it must be more than 0.")
    if not Decimal("0.1") <= s.max_open_risk_pct <= 100:
        out.append(
            f"Most at risk at once: {s.max_open_risk_pct:f} % of the amount is outside 0.1 % to "
            "100 %."
        )
    elif s.max_open_risk_pct < s.risk_pct:
        out.append(
            f"Most at risk at once: {s.max_open_risk_pct:f} % is less than one trade's risk "
            f"({s.risk_pct:f} %), so no trade could ever open; make it at least that."
        )
    if not Decimal(1) <= s.max_drawdown_pct <= 100:
        out.append(f"Largest drop: {s.max_drawdown_pct:f} % is outside 1 % to 100 %.")
    if not 1 <= s.max_positions <= 10:
        out.append(f"Trades open at once: {s.max_positions} is outside 1 to 10.")
    if s.take_profit_r is not None and not Decimal("0.5") <= s.take_profit_r <= 10:
        out.append(
            f"Take-profit: {s.take_profit_r:f} × the stop distance is outside 0.5 to 10 "
            "(or choose none: the rule closes it)."
        )
    return out


# ---- the account ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    id: uuid.UUID
    side: str
    ounces: Decimal
    entry_price: Decimal
    entry_at: datetime
    stop_price: Decimal
    take_profit_price: Decimal | None
    spread: Decimal
    margin: Decimal
    status: str
    exit_price: Decimal | None = None
    exit_at: datetime | None = None
    exit_reason: str | None = None
    result: Decimal | None = None
    swap: Decimal = ZERO

    def floating(self, bid: Decimal, ask: Decimal) -> Decimal:
        """Its result if closed now: a buy at the bid, a sell at the ask (before swap)."""
        if self.side == "BUY":
            return (bid - self.entry_price) * self.ounces
        return (self.entry_price - ask) * self.ounces

    @property
    def risk(self) -> Decimal:
        """What it loses if its stop is hit at the stop's price."""
        return abs(self.entry_price - self.stop_price) * self.ounces


@dataclass(frozen=True)
class Price:
    bid: Decimal
    ask: Decimal
    at: datetime  # the tick's time (UTC)
    leverage: Decimal
    swap_terms: dict[str, Any] = field(default_factory=dict)  # HFM's symbol rules for gold

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class State:
    """An account's money now."""

    balance: Decimal
    floating: Decimal
    equity: Decimal
    margin_used: Decimal
    free_margin: Decimal
    peak: Decimal  # the highest balance it has had
    today: Decimal  # closed results and swap today (HFM's trading day), plus the floating
    positions: list[Position]

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak <= 0:
            return ZERO
        return max(ZERO, (self.peak - self.equity) / self.peak * 100)


async def price_now(conn: AsyncConnection, now: datetime) -> Price | None:
    """HFM's gold price from the bridge's latest report, or None when there is no current one:
    the bridge silent for 15 s, or the market closed or stalled (no new tick for 5 minutes)."""
    row = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == BRIDGE
            )
        )
    ).first()
    if row is None or now - row.received_at > FRESH:
        return None
    snap = row.snapshot or {}
    account = snap.get("account") or {}
    if account.get("trade_mode") not in ("DEMO",):
        return None  # prices are read only from the demo account the server trusts
    tick = (snap.get("ticks") or {}).get(hfm_candles.SYMBOL) or {}
    offset = snap.get("server_utc_offset_ms")
    if tick.get("bid") is None or tick.get("ask") is None or offset is None:
        return None
    at = datetime.fromtimestamp((int(tick["time_msc"]) - int(offset)) / 1000, UTC)
    if now - at > MARKET_MOVING:
        return None
    leverage = Decimal(str(account.get("leverage") or 0)) or LEVERAGE_UNKNOWN
    return Price(
        Decimal(str(tick["bid"])),
        Decimal(str(tick["ask"])),
        at,
        leverage,
        dict((snap.get("symbols") or {}).get(hfm_candles.SYMBOL) or {}),
    )


def _position(r: Any, swap: Decimal = ZERO) -> Position:
    return Position(
        r.id,
        r.side,
        r.ounces,
        r.entry_price,
        r.entry_at,
        r.stop_price,
        r.take_profit_price,
        r.spread,
        r.margin,
        r.status,
        r.exit_price,
        r.exit_at,
        r.exit_reason,
        r.result,
        swap,
    )


async def positions(
    conn: AsyncConnection, account_id: uuid.UUID, *, status: str | None = OPEN, limit: int = 200
) -> list[Position]:
    q = (
        select(gold_sim_positions)
        .where(gold_sim_positions.c.account_id == account_id)
        .order_by(gold_sim_positions.c.entry_at.desc())
        .limit(limit)
    )
    if status is not None:
        q = q.where(gold_sim_positions.c.status == status)
    rows = list(await conn.execute(q))
    swaps = {
        r.position_id: Decimal(str(r.total))
        for r in await conn.execute(
            select(gold_sim_ledger.c.position_id, func.sum(gold_sim_ledger.c.amount).label("total"))
            .where(gold_sim_ledger.c.account_id == account_id, gold_sim_ledger.c.kind == "SWAP")
            .group_by(gold_sim_ledger.c.position_id)
        )
    }
    return [_position(r, swaps.get(r.id, ZERO)) for r in rows]


def trading_day(t: datetime) -> datetime:
    """When HFM's trading day containing ``t`` began (17:00 New York)."""
    return hfm_candles.open_of(t, "1d")


async def state(
    conn: AsyncConnection, account_id: uuid.UUID, price: Price | None, now: datetime
) -> State:
    rows = list(
        await conn.execute(
            select(gold_sim_ledger.c.at, gold_sim_ledger.c.amount, gold_sim_ledger.c.kind)
            .where(gold_sim_ledger.c.account_id == account_id)
            .order_by(gold_sim_ledger.c.at, gold_sim_ledger.c.id)
        )
    )
    balance = peak = today = ZERO
    day = trading_day(now)
    for r in rows:
        balance += r.amount
        peak = max(peak, balance)
        if r.at >= day and r.kind != "DEPOSIT":  # trading results of HFM's trading day
            today += r.amount
    open_now = await positions(conn, account_id)
    floating = sum((p.floating(price.bid, price.ask) for p in open_now), ZERO) if price else ZERO
    used = sum((p.margin for p in open_now), ZERO)
    equity = balance + floating
    return State(balance, floating, equity, used, equity - used, peak, today + floating, open_now)


# ---- sizing -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Size:
    ounces: Decimal
    max_loss: Decimal  # the most this trade may lose at its stop
    per_ounce: Decimal  # one ounce's loss at the stop
    margin: Decimal
    why_not: str | None = None  # WAIT, in the owner's words
    smallest_amount: Decimal | None = None  # from which amount it would trade, at this risk


def size(s: Settings, st: State, stop_distance: Decimal, price: Decimal, leverage: Decimal) -> Size:
    """How many ounces this opportunity buys or sells within every one of the owner's limits."""
    base = min(s.amount, st.equity)  # never more than the money that is left
    max_loss = (base * s.risk_pct / 100).quantize(Decimal("0.0001"), ROUND_DOWN)
    per_ounce = stop_distance
    if per_ounce <= 0 or max_loss <= 0:
        return Size(ZERO, max_loss, per_ounce, ZERO, "no stop distance or no money to risk")
    ounces = (max_loss / per_ounce / STEP).to_integral_value(ROUND_DOWN) * STEP
    if ounces < SMALLEST:
        smallest_amount = (SMALLEST * per_ounce * 100 / s.risk_pct).quantize(CENT, ROUND_CEILING)
        return Size(
            ZERO,
            max_loss,
            per_ounce,
            ZERO,
            "Your selected trading amount is too small for this opportunity at your chosen risk "
            f"level: the smallest trade (0.01 ounce) would lose about "
            f"{(SMALLEST * per_ounce).quantize(CENT)} at its stop, more than the "
            f"{max_loss.quantize(CENT)} you allow ({s.risk_pct.normalize():f} % of "
            f"{base:,.2f}). At {s.risk_pct.normalize():f} %, this opportunity trades from "
            f"an amount of {smallest_amount:,.2f}",
            smallest_amount,
        )
    room = base * s.max_open_risk_pct / 100 - sum((p.risk for p in st.positions), ZERO)
    if ounces * per_ounce > room:
        ounces = (max(room, ZERO) / per_ounce / STEP).to_integral_value(ROUND_DOWN) * STEP
        if ounces < SMALLEST:
            at_risk = sum((p.risk for p in st.positions), ZERO)
            return Size(
                ZERO,
                max_loss,
                per_ounce,
                ZERO,
                f"the open trades already risk {at_risk:,.2f}: another would pass your limit of "
                f"{s.max_open_risk_pct.normalize():f} % of the amount at risk at once"
                if st.positions
                else f"your limit at risk at once ({s.max_open_risk_pct.normalize():f} % of the "
                f"amount) is less than the smallest trade's risk "
                f"({(SMALLEST * per_ounce).quantize(CENT)})",
            )
    margin = (ounces * price / leverage).quantize(CENT, ROUND_CEILING)
    if margin > st.free_margin:
        return Size(
            ZERO,
            max_loss,
            per_ounce,
            margin,
            f"not enough free margin: {margin:,.2f} needed, {st.free_margin:,.2f} free",
        )
    return Size(ounces, max_loss, per_ounce, margin)


# ---- the account's life -----------------------------------------------------------------------


async def create(
    conn: AsyncConnection,
    *,
    name: str,
    starting_balance: Decimal,
    settings: Settings,
    owner: uuid.UUID,
    now: datetime,
) -> uuid.UUID:
    """A new simulated account with the owner's balance and settings; the one running before is
    stopped (one runs at a time), its records kept."""
    if not starting_balance > 0 or starting_balance > 1_000_000:
        raise ValidationFailedError(
            "Starting balance: it must be more than 0 and at most 1,000,000."
        )
    wrong = problems(settings, starting_balance)
    if wrong:
        raise ValidationFailedError(" ".join(wrong))
    await conn.execute(  # one start at a time, so one simulation runs
        text("SELECT pg_advisory_xact_lock(hashtextextended('gold-sim:accounts', 0))")
    )
    name = name.strip()
    if not 2 <= len(name) <= 40:
        raise ValidationFailedError("Name: 2 to 40 characters.")
    taken = await conn.scalar(
        select(gold_sim_accounts.c.id).where(gold_sim_accounts.c.name == name)
    )
    if taken is not None:
        raise ConflictError(f"a simulated account named {name!r} exists; choose another name")
    for running in await running_accounts(conn):
        await close_all(conn, running, now, "a new simulation was started")
        await set_running(conn, running, False, "a new simulation was started", now)
    account_id = uuid7()
    await conn.execute(
        gold_sim_accounts.insert().values(
            id=account_id,
            name=name,
            starting_balance=starting_balance,
            currency="USD",
            created_by=owner,
            created_at=now,
            status="RUNNING",
            status_changed_at=now,
            status_reason="started by the owner",
        )
    )
    await conn.execute(
        gold_sim_ledger.insert().values(
            id=uuid7(),
            account_id=account_id,
            at=now,
            kind="DEPOSIT",
            amount=starting_balance,
            ref=f"deposit:{account_id}",
            note="the owner's starting balance (simulated)",
        )
    )
    await save_settings(conn, account_id, settings, owner, now, balance=starting_balance)
    return account_id


async def save_settings(
    conn: AsyncConnection,
    account_id: uuid.UUID,
    s: Settings,
    owner: uuid.UUID,
    now: datetime,
    *,
    balance: Decimal | None = None,
) -> None:
    if balance is None:
        balance = (await state(conn, account_id, None, now)).balance
    wrong = problems(s, balance)
    if wrong:
        raise ValidationFailedError(" ".join(wrong))
    await conn.execute(
        gold_sim_settings.insert().values(
            id=uuid7(),
            account_id=account_id,
            amount=s.amount,
            risk_pct=s.risk_pct,
            daily_loss=s.daily_loss,
            max_open_risk_pct=s.max_open_risk_pct,
            max_drawdown_pct=s.max_drawdown_pct,
            max_positions=s.max_positions,
            take_profit_r=s.take_profit_r,
            allow_sell=s.allow_sell,
            created_by=owner,
            created_at=now,
        )
    )


async def set_running(
    conn: AsyncConnection, account_id: uuid.UUID, running: bool, reason: str, now: datetime
) -> None:
    await _lock(conn, account_id)
    await conn.execute(
        update(gold_sim_accounts)
        .where(gold_sim_accounts.c.id == account_id)
        .values(
            status="RUNNING" if running else "STOPPED",
            status_changed_at=now,
            status_reason=reason,
        )
    )


async def settings_of(conn: AsyncConnection, account_id: uuid.UUID) -> Settings | None:
    r = (
        await conn.execute(
            select(gold_sim_settings)
            .where(gold_sim_settings.c.account_id == account_id)
            .order_by(gold_sim_settings.c.created_at.desc(), gold_sim_settings.c.id.desc())
            .limit(1)
        )
    ).first()
    if r is None:
        return None
    return Settings(
        r.amount,
        r.risk_pct,
        r.daily_loss,
        r.max_open_risk_pct,
        r.max_drawdown_pct,
        r.max_positions,
        r.take_profit_r,
        r.allow_sell,
    )


async def close(
    conn: AsyncConnection, p: Position, exit_price: Decimal, reason: str, now: datetime
) -> Decimal | None:
    """Close it at ``exit_price`` and book its result, once; None when it was already closed."""
    result = (
        (exit_price - p.entry_price) * p.ounces
        if p.side == "BUY"
        else (p.entry_price - exit_price) * p.ounces
    )
    done = await conn.execute(
        update(gold_sim_positions)
        .where(gold_sim_positions.c.id == p.id, gold_sim_positions.c.status == OPEN)
        .values(
            status=CLOSED, exit_price=exit_price, exit_at=now, exit_reason=reason, result=result
        )
    )
    if not done.rowcount:
        return None
    account_id = await conn.scalar(
        select(gold_sim_positions.c.account_id).where(gold_sim_positions.c.id == p.id)
    )
    await conn.execute(
        pg_insert(gold_sim_ledger)
        .values(
            id=uuid7(),
            account_id=account_id,
            at=now,
            kind="RESULT",
            amount=result,
            position_id=p.id,
            ref=f"result:{p.id}",
            note=f"{p.side} {p.ounces.normalize():f} oz closed ({reason}) at {exit_price}",
        )
        .on_conflict_do_nothing(index_elements=[gold_sim_ledger.c.ref])
    )
    return result


def _hit(p: Position, price: Price) -> tuple[Decimal, str] | None:
    """The price and reason it closes at now, or None: a buy watches the bid, a sell the ask."""
    if p.side == "BUY":
        if price.bid <= p.stop_price:
            return price.bid, "STOP_LOSS"
        if p.take_profit_price is not None and price.bid >= p.take_profit_price:
            return price.bid, "TAKE_PROFIT"
        return None
    if price.ask >= p.stop_price:
        return price.ask, "STOP_LOSS"
    if p.take_profit_price is not None and price.ask <= p.take_profit_price:
        return price.ask, "TAKE_PROFIT"
    return None


def swap_per_ounce(terms: dict[str, Any], side: str, price: Decimal) -> Decimal | None:
    """HFM's overnight swap for one ounce held one night (negative: paid), from its own terms,
    in Decimal; None when the terms are not known or not money."""
    try:
        mode = int(terms["swap_mode"])
        raw = Decimal(str(terms["swap_long" if side == "BUY" else "swap_short"]))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    contract = Decimal(str(terms.get("contract_size") or 0))
    point = Decimal(str(terms.get("point") or 0))
    if mode == 0:
        return ZERO
    if mode == 1 and point > 0:  # points per lot: per ounce it is points × point, in price
        return raw * point
    if mode in (3, 4) and contract > 0:  # money per lot per night
        return raw / contract
    if mode in (5, 6):  # a yearly percentage of the price
        return price * raw / 100 / 360
    return None


def _triple(terms: dict[str, Any]) -> int | None:
    """HFM's triple-swap day as a Python weekday (Monday 0)."""
    day = terms.get("swap_rollover3days")
    try:
        return (int(day) + 6) % 7 if day is not None and 0 <= int(day) <= 6 else None
    except (TypeError, ValueError):
        return None


async def _swap(conn: AsyncConnection, p: Position, price: Price, now: datetime) -> None:
    """HFM's swap for each night it has been held, once a night."""
    per = swap_per_ounce(price.swap_terms, p.side, p.entry_price)
    if per is None:
        return
    held = venue_terms.nights(p.entry_at, now, _triple(price.swap_terms))
    booked = int(
        await conn.scalar(
            select(func.count())
            .select_from(gold_sim_ledger)
            .where(gold_sim_ledger.c.position_id == p.id, gold_sim_ledger.c.kind == "SWAP")
        )
        or 0
    )
    if held <= booked:
        return
    per_night = (per * p.ounces).quantize(Decimal("0.0001"))
    account_id = await conn.scalar(
        select(gold_sim_positions.c.account_id).where(gold_sim_positions.c.id == p.id)
    )
    for n in range(booked, held):  # one row a night, so each is booked once
        await conn.execute(
            pg_insert(gold_sim_ledger)
            .values(
                id=uuid7(),
                account_id=account_id,
                at=now,
                kind="SWAP",
                amount=per_night,
                position_id=p.id,
                ref=f"swap:{p.id}:{n}",
                note="HFM's overnight swap (simulated)",
            )
            .on_conflict_do_nothing(index_elements=[gold_sim_ledger.c.ref])
        )


async def running_accounts(conn: AsyncConnection) -> list[uuid.UUID]:
    return [
        r.id
        for r in await conn.execute(
            select(gold_sim_accounts.c.id).where(gold_sim_accounts.c.status == "RUNNING")
        )
    ]


async def _lock(conn: AsyncConnection, account_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"gold-sim:{account_id}"},
    )


async def watch(db: Database, now: datetime) -> list[str]:
    """Every second, for every open position of every simulated account: the swap due, then its
    stop or target; a stopped account's trades close at HFM's price (they were left open only
    when there was no price at the stop)."""
    done: list[str] = []
    async with db.transaction() as conn:
        price = await price_now(conn, now)
        if price is None:
            return done
        rows = list(
            await conn.execute(
                select(gold_sim_positions.c.account_id, gold_sim_accounts.c.status)
                .join(gold_sim_accounts, gold_sim_accounts.c.id == gold_sim_positions.c.account_id)
                .where(gold_sim_positions.c.status == OPEN)
                .distinct()
            )
        )
        for r in rows:
            await _lock(conn, r.account_id)
            for p in await positions(conn, r.account_id):
                await _swap(conn, p, price, now)  # a night held is paid even when it closes now
                hit = _hit(p, price)
                if hit is None and r.status != "RUNNING":
                    hit = (price.bid if p.side == "BUY" else price.ask, "OWNER")
                if hit is not None and await close(conn, p, hit[0], hit[1], now) is not None:
                    done.append(f"{p.side}:{hit[1]}")
    return done


async def reading_now(conn: AsyncConnection, now: datetime) -> analysis.Reading:
    candles = {tf: await hfm_candles.candles(conn, tf, n) for tf, n in CANDLES.items()}
    return analysis.read(now, candles)


async def decide(db: Database, now: datetime) -> list[str]:
    """Once per new 1-minute candle and running account: the rule's exit, then BUY, SELL or
    WAIT within every limit, recorded."""
    out: list[str] = []
    async with db.transaction() as conn:
        reading = await reading_now(conn, now)
        if reading.bar is None:
            return out
        price = await price_now(conn, now)
        for account_id in await running_accounts(conn):
            await _lock(conn, account_id)
            status = await conn.scalar(
                select(gold_sim_accounts.c.status).where(gold_sim_accounts.c.id == account_id)
            )
            if status != "RUNNING":
                continue  # stopped meanwhile
            seen = await conn.scalar(
                select(gold_sim_decisions.c.id).where(
                    gold_sim_decisions.c.account_id == account_id,
                    gold_sim_decisions.c.bar == reading.bar,
                )
            )
            if seen is not None:
                continue  # decided on this candle already (a restart, a second worker)
            out.append(await _decide_one(conn, account_id, reading, price, now))
    return out


async def _decide_one(
    conn: AsyncConnection,
    account_id: uuid.UUID,
    reading: analysis.Reading,
    price: Price | None,
    now: datetime,
) -> str:
    s = await settings_of(conn, account_id)
    decision_id = uuid7()
    reasons = list(reading.reasons)
    action, ounces, opened = "WAIT", None, None

    async def record() -> None:
        await conn.execute(
            pg_insert(gold_sim_decisions)
            .values(
                id=decision_id,
                account_id=account_id,
                bar=reading.bar,
                at=now,
                decision=reading.decision,
                action=action,
                reasons=reasons,
                bid=price.bid if price else None,
                ask=price.ask if price else None,
                ounces=ounces,
                stop_distance=reading.stop_distance,
                position_id=opened,
            )
            .on_conflict_do_nothing(index_elements=["account_id", "bar"])
        )

    if s is None or price is None:
        reasons.append("no current HFM price" if price is None else "no settings")
        await record()
        return f"{account_id}:WAIT"
    # The rule's exit: a position the hourly trend has turned against closes.
    h1 = reading.frames["1h"].trend
    for p in await positions(conn, account_id):
        against = (p.side == "BUY" and h1 == analysis.DOWN) or (
            p.side == "SELL" and h1 == analysis.UP
        )
        if against:
            exit_price = price.bid if p.side == "BUY" else price.ask
            if await close(conn, p, exit_price, "RULE", now) is not None:
                reasons.append(f"closed the {p.side}: the hourly trend turned {h1}")
    if reading.decision == analysis.WAIT:
        await record()
        return f"{account_id}:WAIT"
    side = reading.decision
    st = await state(conn, account_id, price, now)
    why: str | None = None
    if side == analysis.SELL and not s.allow_sell:
        why = "selling is off in your settings"
    elif st.today <= -s.daily_loss:
        why = f"today's loss ({-st.today:,.2f}) reached your daily limit of {s.daily_loss:,.2f}"
    elif st.drawdown_pct >= s.max_drawdown_pct:
        why = (
            f"the account is {st.drawdown_pct:.2f} % below its best ({st.peak:,.2f}): your "
            f"limit is {s.max_drawdown_pct.normalize():f} %"
        )
    elif len(st.positions) >= s.max_positions:
        why = f"{len(st.positions)} trade(s) open: your limit is {s.max_positions}"
    elif reading.stop_distance is None:
        why = "no stop distance"
    sz: Size | None = None
    if why is None:
        assert reading.stop_distance is not None
        entry = price.ask if side == analysis.BUY else price.bid
        sz = size(s, st, reading.stop_distance, entry, price.leverage)
        why = sz.why_not
    if why is not None or sz is None:
        reasons.append(f"WAIT — {why}")
        action = "WAIT"
        await record()
        return f"{account_id}:WAIT"
    assert reading.stop_distance is not None
    stop_d = reading.stop_distance
    entry = price.ask if side == analysis.BUY else price.bid
    sign = 1 if side == analysis.BUY else -1
    stop = entry - sign * stop_d
    target = entry + sign * stop_d * s.take_profit_r if s.take_profit_r is not None else None
    opened = uuid7()
    ounces = sz.ounces
    action = "OPENED"
    reasons.append(
        f"{side} {sz.ounces.normalize():f} oz at {entry}: stop {stop}"
        + (f", target {target.quantize(CENT)}" if target is not None else ", no fixed target")
        + f"; at most {(sz.ounces * stop_d).quantize(CENT)} lost at the stop"
    )
    await record()  # the decision first: its key is what stops a second opening
    await conn.execute(
        gold_sim_positions.insert().values(
            id=opened,
            account_id=account_id,
            decision_id=decision_id,
            side=side,
            ounces=sz.ounces,
            entry_price=entry,
            entry_at=now,
            stop_price=stop,
            take_profit_price=target.quantize(CENT) if target is not None else None,
            spread=price.spread,
            margin=sz.margin,
            status=OPEN,
        )
    )
    return f"{account_id}:{side}"


async def close_all(conn: AsyncConnection, account_id: uuid.UUID, now: datetime, why: str) -> int:
    """The owner stops it: every open position closes at HFM's price now (if there is one)."""
    price = await price_now(conn, now)
    if price is None:
        return 0
    n = 0
    for p in await positions(conn, account_id):
        exit_price = price.bid if p.side == "BUY" else price.ask
        if await close(conn, p, exit_price, "OWNER", now) is not None:
            n += 1
    return n


async def resume(conn: AsyncConnection, account_id: uuid.UUID, now: datetime) -> None:
    """Run this simulation again; any other running one stops (one runs at a time)."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('gold-sim:accounts', 0))")
    )
    for other in await running_accounts(conn):
        if other != account_id:
            await set_running(conn, other, False, "another simulation was resumed", now)
    await set_running(conn, account_id, True, "resumed by the owner", now)
