"""Trader Insight (S113): what the system's tested gold rule sees now, for the owner's own
decision. It never places, changes or closes anything.

- **The idea** is Robot A's rule on HFM's own candles (4-hour and 1-hour trend, 15-minute
  trigger, stop 2 × ATR, target 1:2; the lab's Run 8): BUY, SELL or NO TRADE, with the entry,
  stop-loss and target from the price now.
- **No invented probability.** The rule's win rate in markets as volatile as this one is its
  in-sample record (2022-11 to 2026-11): it is shown with its sample size, period and 95 %
  range, under "Probability not verified" (no out-of-sample test per kind of market exists).
- **Separate measures:** the probability (the record), the signal's strength (how many of its
  conditions hold) and the setup's quality (tested criteria, each explained) are never merged.
- **News:** no economic-calendar source is connected, so no news warning is given and none is
  claimed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.gold import probability, sim, trend_buy, trend_sim, trend_test
from cosmic.manual.live import LiveAccount
from cosmic.market import hfm_candles

NO_TRADE = "NO TRADE"
NEWS = (
    "News: not checked. No economic-calendar source is connected, so no news warning can be "
    "given; look at the calendar yourself before a trade."
)
LAB = "HFM gold, 2022-11 to 2026-11 (the lab's Run 8, HFM's spread and swap included)"


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
            )
        bid, ask = price.bid, price.ask
        source = "HFM's price from your demo terminal (your live account is not reporting)"
    reading = await trend_test.reading_now(conn, now, sell=True)
    levels = tuple(await _levels(conn))
    tick = account.spec.tick_size if account.spec is not None else Decimal("0.01")
    conditions = [
        f"4-hour trend {reading.h4 or 'unknown'}, 1-hour trend {reading.h1 or 'unknown'}",
        f"spread {ask - bid} ({source})",
    ]
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
        )
    rec = trend_sim.SIDES[side].regime(found.distance)
    low, high = wilson(rec.p_win, rec.trades)
    prob = Probability(
        verified=False,
        p=rec.p_win,
        n=rec.trades,
        low=low,
        high=high,
        period=LAB,
        kind=f"{rec.name} markets ({rec.where})",
    )
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
    )
