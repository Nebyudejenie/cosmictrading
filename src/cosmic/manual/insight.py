"""Trader Insight (S113): what the system's tested gold rule sees now, for the owner's own
decision. It never places, changes or closes anything.

- **The idea** is Robot A's rule on HFM's own candles (4-hour and 1-hour trend, 15-minute
  trigger, stop 2 × ATR, target 1:2; the lab's Run 8): BUY, SELL or NO TRADE, with the entry,
  stop-loss and target from the price now.
- **No invented probability.** The rule's win rate in markets as volatile as this one is its
  in-sample record (2022-11 to 2026-11): it is shown with its sample size, period and 95 %
  range, under "Probability not verified" (no out-of-sample test per kind of market exists).
- **Verified only out of sample (S114):** the probability counts as verified only from the
  rule's forward test (Robot A's simulation, trades it took after the lab closed), and only from
  ``FORWARD_MIN`` closed trades; until then the forward record is shown with its count.
- **Separate measures:** the probability (the record), the signal's strength (how many of its
  conditions hold) and the setup's quality (tested criteria, each explained) are never merged.
- **The market (S114):** trend (4-hour and 1-hour, the rule's own), structure (the last swings of
  the hourly candles), support and resistance (4-hour swing points of the last 30 days, the
  previous day's and the last 24 hours' high and low) and volatility (the daily range against its
  own year), all from HFM's own candles.
- **News:** no economic-calendar source is connected, so no news warning is given and none is
  claimed.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema_gold import gold_sim_accounts, gold_sim_positions
from cosmic.gold import probability, sim, trend_buy, trend_sim, trend_test
from cosmic.manual.live import LiveAccount
from cosmic.market import hfm_candles
from cosmic.market.hfm_candles import Candle

NO_TRADE = "NO TRADE"
NEWS = (
    "News: not checked. No economic-calendar source is connected, so no news warning can be "
    "given; look at the calendar yourself before a trade."
)
LAB = "HFM gold, 2022-11 to 2026-11 (the lab's Run 8, HFM's spread and swap included)"
FORWARD_MIN = 30  # closed forward-test trades before a probability counts as verified
SWING = 2  # a swing high (low) is above (below) the two candles on each side
CALM, NORMAL, HIGH = "calm", "normal", "high"


@dataclass(frozen=True)
class Probability:
    verified: bool
    p: Decimal  # the target reached first, of its trades
    n: int
    low: Decimal  # the 95 % range (Wilson)
    high: Decimal
    period: str
    kind: str  # e.g. "volatile markets (stop 15.26 or more)"


@dataclass(frozen=True)
class Forward:
    """The rule's forward test so far: Robot A's simulation, out of sample."""

    n: int
    wins: int  # the target reached first
    since: datetime | None

    @property
    def p(self) -> Decimal | None:
        return Decimal(self.wins) / self.n if self.n else None


@dataclass(frozen=True)
class Market:
    """Gold now, from HFM's own candles: trend, structure, levels and volatility."""

    structure: str | None = None
    supports: tuple[tuple[str, Decimal], ...] = ()
    resistances: tuple[tuple[str, Decimal], ...] = ()
    volatility: str | None = None  # CALM, NORMAL or HIGH
    volatility_why: str | None = None


@dataclass(frozen=True)
class Criterion:
    name: str
    passed: bool
    why: str


@dataclass(frozen=True)
class Insight:
    as_of: datetime | None
    price_source: str
    direction: str  # BUY, SELL or NO TRADE
    rule_side: str | None  # the rule's direction, even when its record says NO TRADE
    why: str
    entry: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    rr: Decimal | None = None
    probability: Probability | None = None
    expected_r: Decimal | None = None
    strength: int = 0
    strength_of: int = 5
    quality: tuple[Criterion, ...] = ()
    no_trade: tuple[str, ...] = ()
    levels: tuple[tuple[str, Decimal], ...] = ()
    conditions: tuple[str, ...] = ()
    news: str = NEWS
    atr: Decimal | None = None  # the usual 15-minute move (ATR14), in price
    notes: tuple[str, ...] = field(default_factory=tuple)
    trend: str | None = None  # "4-hour UP, 1-hour UP"
    market: Market = field(default_factory=Market)
    forward: Forward | None = None

    @property
    def quality_score(self) -> int:
        if not self.quality:
            return 0
        return round(100 * sum(c.passed for c in self.quality) / len(self.quality))


def wilson(p: Decimal, n: int, z: float = 1.96) -> tuple[Decimal, Decimal]:
    """The 95 % range of a win rate measured on ``n`` trades."""
    if n <= 0:
        return Decimal(0), Decimal(1)
    pf = float(p)
    centre = (pf + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(pf * (1 - pf) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (
        Decimal(str(round(max(centre - half, 0.0), 4))),
        Decimal(str(round(min(centre + half, 1.0), 4))),
    )


def true_ranges(candles: list[Candle]) -> list[Decimal]:
    out = []
    for prev, c in itertools.pairwise(candles):
        out.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    return out


def atr(candles: list[Candle], n: int = 14) -> Decimal | None:
    """The average true range of the last ``n`` candles (a simple mean)."""
    tr = true_ranges(candles)
    return sum(tr[-n:], Decimal(0)) / n if len(tr) >= n else None


def swings(candles: list[Candle]) -> tuple[list[Candle], list[Candle]]:
    """Swing highs and swing lows: a candle whose high (low) is above (below) the ``SWING``
    candles on each side, oldest first."""
    highs, lows = [], []
    for i in range(SWING, len(candles) - SWING):
        around = candles[i - SWING : i] + candles[i + 1 : i + 1 + SWING]
        if all(candles[i].high > c.high for c in around):
            highs.append(candles[i])
        if all(candles[i].low < c.low for c in around):
            lows.append(candles[i])
    return highs, lows


def structure_of(hours: list[Candle]) -> str | None:
    """The last two swing highs and lows of the hourly candles, in plain words."""
    highs, lows = swings(hours)
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh, hl = highs[-1].high > highs[-2].high, lows[-1].low > lows[-2].low
    lh, ll = highs[-1].high < highs[-2].high, lows[-1].low < lows[-2].low
    swing = (
        f"last swing highs {highs[-2].high:.2f} → {highs[-1].high:.2f}, lows "
        f"{lows[-2].low:.2f} → {lows[-1].low:.2f}"
    )
    if hh and hl:
        return f"rising: higher highs and higher lows ({swing})"
    if lh and ll:
        return f"falling: lower highs and lower lows ({swing})"
    return f"mixed: no clear direction, a range ({swing})"


def zones(
    fours: list[Candle], price: Decimal, n: int = 2
) -> tuple[tuple[tuple[str, Decimal], ...], tuple[tuple[str, Decimal], ...]]:
    """The nearest 4-hour swing lows below the price (support) and swing highs above it
    (resistance)."""
    highs, lows = swings(fours)
    above = sorted({c.high for c in highs if c.high > price})[:n]
    below = sorted({c.low for c in lows if c.low < price}, reverse=True)[:n]
    return (
        tuple(("Support (4-hour swing low)", v) for v in below),
        tuple(("Resistance (4-hour swing high)", v) for v in above),
    )


def volatility_of(days: list[Candle]) -> tuple[str, str] | None:
    """Today's daily range (ATR14) against its own last year: calm, normal or high."""
    if len(days) < 60:
        return None
    tr = true_ranges(days)
    series = [sum(tr[i - 14 : i], Decimal(0)) / 14 for i in range(14, len(tr) + 1)]
    now, ordered = series[-1], sorted(series)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return None
    ratio = now / median
    kind = CALM if ratio < Decimal("0.8") else HIGH if ratio > Decimal("1.25") else NORMAL
    return kind, (
        f"the daily range (ATR14) is {now:.2f}, {ratio:.2f} × its median of {median:.2f} "
        f"over the last {len(series)} days"
    )


async def market_now(conn: AsyncConnection, price: Decimal | None) -> Market:
    hourly = await hfm_candles.candles(conn, "1h", 720)  # 30 days
    hours = hourly[-72:]
    fours = trend_buy.four_hours(hourly)
    days = await hfm_candles.candles(conn, "1d", 260)
    ref = price if price is not None else (hours[-1].close if hours else None)
    supports, resistances = zones(fours, ref) if ref is not None else ((), ())
    vol = volatility_of(days)
    return Market(
        structure=structure_of(hours),
        supports=supports,
        resistances=resistances,
        volatility=vol[0] if vol else None,
        volatility_why=vol[1] if vol else None,
    )


async def forward_record(conn: AsyncConnection) -> Forward:
    """The rule's closed forward-test trades (Robot A's simulation)."""
    p = gold_sim_positions
    row = (
        await conn.execute(
            select(
                func.count(),
                func.count().filter(p.c.exit_reason == "TAKE_PROFIT"),
                func.min(p.c.entry_at),
            )
            .select_from(p.join(gold_sim_accounts, gold_sim_accounts.c.id == p.c.account_id))
            .where(gold_sim_accounts.c.rule == trend_sim.RULE, p.c.status == sim.CLOSED)
        )
    ).one()
    return Forward(int(row[0]), int(row[1]), row[2])


def choose_probability(forward: Forward, p_lab: Decimal, n_lab: int, kind: str) -> Probability:
    """Verified only from the forward test (out of sample) and only from ``FORWARD_MIN`` closed
    trades; otherwise the lab's in-sample record, marked not verified."""
    if forward.n >= FORWARD_MIN and forward.p is not None:
        low, high = wilson(forward.p, forward.n)
        return Probability(
            verified=True,
            p=forward.p,
            n=forward.n,
            low=low,
            high=high,
            period=f"the rule's forward test since {forward.since:%Y-%m-%d}"
            if forward.since
            else "the rule's forward test",
            kind="its forward test, both directions, every kind of market",
        )
    low, high = wilson(p_lab, n_lab)
    return Probability(verified=False, p=p_lab, n=n_lab, low=low, high=high, period=LAB, kind=kind)


def _trend_says(trend: str | None, side: str) -> bool:
    return trend == ("UP" if side == trend_buy.BUY else "DOWN")


async def _levels(conn: AsyncConnection) -> list[tuple[str, Decimal]]:
    out: list[tuple[str, Decimal]] = []
    days = await hfm_candles.candles(conn, "1d", 1)
    if days:
        out += [("Previous day's high", days[-1].high), ("Previous day's low", days[-1].low)]
    hours = await hfm_candles.candles(conn, "1h", 24)
    if hours:
        out += [
            ("24-hour high", max(c.high for c in hours)),
            ("24-hour low", min(c.low for c in hours)),
        ]
    return out


async def build(conn: AsyncConnection, now: datetime, account: LiveAccount) -> Insight:
    """Trader Insight now: the rule's reading, its record and every reason not to trade."""
    forward = await forward_record(conn)
    if account.priced and account.quote is not None:
        bid, ask = account.quote.bid, account.quote.ask
        source = "your live account's price"
    else:
        price = await sim.price_now(conn, now)
        if price is None:
            return Insight(
                None,
                "none",
                NO_TRADE,
                None,
                "no current HFM price: the market is closed or the terminals are not reporting",
                no_trade=("no current price",),
                levels=tuple(await _levels(conn)),
                market=await market_now(conn, None),
                forward=forward,
            )
        bid, ask = price.bid, price.ask
        source = "HFM's price from your demo terminal (your live account is not reporting)"
    reading = await trend_test.reading_now(conn, now, sell=True)
    levels = tuple(await _levels(conn))
    market = await market_now(conn, (bid + ask) / 2)
    trend = f"4-hour {reading.h4 or 'unknown'}, 1-hour {reading.h1 or 'unknown'}"
    tick = account.spec.tick_size if account.spec is not None else Decimal("0.01")
    conditions = [f"spread {ask - bid} ({source})"]
    if reading.atr is not None:
        conditions.append(f"the usual 15-minute move (ATR14) is {Decimal(str(reading.atr)):.2f}")
    side = reading.decision if reading.decision in trend_sim.SIDES else None
    if side is None or reading.atr is None:
        return Insight(
            reading.bar,
            source,
            NO_TRADE,
            None,
            f"the rule sees no setup now: {reading.why}",
            no_trade=(f"no setup: {reading.why}",),
            levels=levels,
            conditions=tuple(conditions),
            trend=trend,
            market=market,
            forward=forward,
        )
    found = trend_buy.levels(reading.atr, bid, ask, tick, Decimal(0), side)
    if isinstance(found, str):
        return Insight(
            reading.bar,
            source,
            NO_TRADE,
            side,
            f"a {side} setup, but {found}",
            no_trade=(found,),
            levels=levels,
            conditions=tuple(conditions),
            trend=trend,
            market=market,
            forward=forward,
        )
    rec = trend_sim.SIDES[side].regime(found.distance)
    prob = choose_probability(forward, rec.p_win, rec.trades, f"{rec.name} markets ({rec.where})")
    band = trend_sim.band(rec.p_win, rec.expected_r)
    spread_ok = (ask - bid) <= trend_buy.COST_FLOOR * found.distance
    quality = (
        Criterion(
            "Trend alignment",
            _trend_says(reading.h4, side) and _trend_says(reading.h1, side),
            f"4-hour {reading.h4}, 1-hour {reading.h1}: the rule needs both "
            f"{'up' if side == trend_buy.BUY else 'down'}",
        ),
        Criterion("Momentum trigger", True, f"the 15-minute momentum just turned: {reading.why}"),
        Criterion(
            "Record in this volatility",
            rec.expected_r > 0,
            f"{rec.expected_r:+} R a trade after costs over {rec.trades} trades in {rec.name} "
            "markets",
        ),
        Criterion("Reward-to-risk", True, "1:2 — the target twice the stop distance, as tested"),
        Criterion(
            "Cost",
            spread_ok,
            f"the spread is {(ask - bid) / found.distance * 100:.1f} % of the stop distance "
            "(at most 5 %, as tested)",
        ),
        Criterion("Price current", True, f"from {source}"),
    )
    strength = sum(
        (
            _trend_says(reading.h4, side),
            _trend_says(reading.h1, side),
            True,
            rec.expected_r > 0,
            spread_ok,
        )
    )
    no_trade: list[str] = []
    if band == probability.REJECT:
        no_trade.append(
            f"its record in {rec.name} markets is below your 30 %: {rec.p_win * 100:.1f} %"
        )
    if rec.expected_r <= 0:
        no_trade.append(
            f"in {rec.name} markets its record made no money ({rec.expected_r:+} R a trade)"
        )
    if trend_sim.SIDES[side].t < 2:
        conditions.append(
            f"the {side} side's whole record is about break-even (t {trend_sim.SIDES[side].t}): "
            "no proven edge"
        )
    entry = ask if side == trend_buy.BUY else bid
    return Insight(
        reading.bar,
        source,
        NO_TRADE if no_trade else side,
        side,
        reading.why,
        entry=entry,
        stop=found.stop,
        target=found.take_profit,
        rr=trend_buy.REWARD,
        probability=prob,
        expected_r=rec.expected_r,
        strength=strength,
        quality=quality,
        no_trade=tuple(no_trade),
        levels=levels,
        conditions=tuple(conditions),
        atr=Decimal(str(round(reading.atr, 4))),
        trend=trend,
        market=market,
        forward=forward,
    )
