"""The TEST rule (S98; the owner's choice of 2026-11-17, "Cent demo, 2 USD a trade"): a forward
test, on a demo account, of the best candidate the gold lab has found in 24 pre-registered
trials. It is NOT an edge: on 4 years of HFM's own candles it made +0.18 R a trade on its BUY
side (t 2.41, positive in 2022-24 and in 2025-26), but the whole rule did not beat random timing
(p 0.115 against the 0.0167 required) and lost slightly in 2026 (lab Run 8, trial 24). The
forward test is how it earns or loses the right to be called one.

The rule, exactly as pre-registered (Run 8, family C, BUY side only):
- at the close of a 15-minute candle, when the 4-hour and the 1-hour trends are both UP (from
  completed candles: (EMA20 − EMA50) / ATR14 above +0.25, 50 candles at least; the definition
  of ``probability._trend``),
- and the 15-minute RSI(14) has just crossed up through 50 (at or below 50 on the candle
  before, above it now) with the close above its EMA(21):
- BUY at the next price; the stop-loss 2 × ATR14 of the 15-minute candles below the entry, the
  take-profit twice that distance above it (the owner's 1:2); out after 24 hours at the latest.
- No entry when HFM's spread is more than 5 % of the stop distance (the lab's kept cost floor).

Its 4-hour candles are built here from HFM's 1-hour candles (four to a candle, from 00:00 UTC),
as the lab built them from 1-minute candles. Pure: no database, and no clock but the one passed
in. Indicators are floats (statistics, not money); every price is a Decimal."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from cosmic.market.hfm_candles import Candle

VERSION = "m15-trend-buy-1"
BUY, WAIT = "BUY", "WAIT"
MIN_CANDLES = 50  # completed candles a trend needs (the lab's definition)
TREND_AT = 0.25
STOP_ATR = Decimal(2)  # the stop: 2 × ATR14 of the 15-minute candles
REWARD = Decimal(2)  # the take-profit: twice the stop distance (1:2)
HOLD = timedelta(hours=24)  # out at the latest after this long
COST_FLOOR = Decimal("0.05")  # no entry when the spread is more than 5 % of the stop distance
FOUR_HOURS = timedelta(hours=4)
EVIDENCE = (
    "lab Run 8 trial 24 (pre-registered 2026-11-17 09:32 UTC), HFM M1 2022-10-31 to "
    "2026-11-09: BUY side 372 trades, +0.177 R a trade (t 2.41), train +0.236 R, test +0.159 R; "
    "the whole rule +0.122 R (t 2.12), random timing beaten in 88.5 % of 200 runs (needed "
    "98.3 %), 2026 −0.02 R: not an edge, a candidate for a forward test"
)


@dataclass(frozen=True)
class Reading:
    """What the rule sees at a 15-minute candle's close."""

    bar: datetime | None  # the 15-minute candle that just closed
    decision: str  # BUY or WAIT
    why: str
    h4: str | None  # UP, FLAT or DOWN
    h1: str | None
    rsi: float | None
    rsi_before: float | None
    atr: float | None  # ATR14 of the 15-minute candles, in price


def _trend(candles: Sequence[Candle]) -> str | None:
    """UP, FLAT or DOWN from completed candles, oldest first; None with too few."""
    if len(candles) < MIN_CANDLES:
        return None
    e20 = e50 = atr = prev = None
    k20, k50 = 2 / 21, 2 / 51
    for c in candles:
        high, low, close = float(c.high), float(c.low), float(c.close)
        tr = high - low if prev is None else max(high - low, abs(high - prev), abs(low - prev))
        atr = tr if atr is None else (atr * 13 + tr) / 14
        e20 = close if e20 is None else e20 + k20 * (close - e20)
        e50 = close if e50 is None else e50 + k50 * (close - e50)
        prev = close
    if not atr or e20 is None or e50 is None:
        return None
    z = (e20 - e50) / atr
    return "UP" if z > TREND_AT else "DOWN" if z < -TREND_AT else "FLAT"


def _fifteen(candles: Sequence[Candle]) -> tuple[float, float, float, float, float] | None:
    """ATR14, RSI14 now and one candle before, EMA21 and the last close, from completed
    15-minute candles, oldest first (Wilder's smoothing, seeded on the first candle)."""
    atr = gain = loss = ema = prev = None
    rsi = before = 50.0
    k21 = 2 / 22
    for c in candles:
        high, low, close = float(c.high), float(c.low), float(c.close)
        tr = high - low if prev is None else max(high - low, abs(high - prev), abs(low - prev))
        atr = tr if atr is None else (atr * 13 + tr) / 14
        ema = close if ema is None else ema + k21 * (close - ema)
        if prev is not None:
            up, down = max(close - prev, 0.0), max(prev - close, 0.0)
            gain = up if gain is None else (gain * 13 + up) / 14
            loss = down if loss is None else (loss * 13 + down) / 14
            before = rsi
            rsi = (50.0 if gain == 0 else 100.0) if loss == 0 else 100 - 100 / (1 + gain / loss)
        prev = close
    if atr is None or ema is None or prev is None or len(candles) < 2:
        return None
    return atr, rsi, before, ema, prev


def _block(t: datetime) -> datetime:
    return t.replace(hour=t.hour - t.hour % 4, minute=0, second=0, microsecond=0)


def four_hours(h1: Sequence[Candle]) -> list[Candle]:
    """4-hour candles from complete 1-hour candles, oldest first: four to a candle from 00:00
    UTC. One is complete once its last hour is in, or once a later one has begun (the hours
    the market is closed are simply missing)."""
    out: list[Candle] = []
    group: list[Candle] = []

    def done() -> None:
        out.append(
            Candle(
                _block(group[0].open_time),
                group[0].open,
                max(c.high for c in group),
                min(c.low for c in group),
                group[-1].close,
                sum((c.volume for c in group), Decimal(0)),
                True,
            )
        )

    for c in h1:
        if not c.complete:
            break
        if group and _block(c.open_time) != _block(group[0].open_time):
            done()
            group = []
        group.append(c)
    if group and group[-1].open_time.hour % 4 == 3:
        done()
    return out


def read(m15: Sequence[Candle], h1: Sequence[Candle], now: datetime) -> Reading:
    """The rule at the close of the newest complete 15-minute candle. ``m15`` and ``h1``: HFM's
    candles, oldest first; only complete ones are used. ``now`` is for the age check."""
    m15 = [c for c in m15 if c.complete]
    h1 = [c for c in h1 if c.complete]
    if not m15:
        return Reading(None, WAIT, "no 15-minute candles yet", None, None, None, None, None)
    bar = m15[-1].open_time
    end = bar + timedelta(minutes=15)

    def wait(
        why: str,
        h4: str | None = None,
        h1: str | None = None,
        rsi: float | None = None,
        rsi_before: float | None = None,
        atr: float | None = None,
    ) -> Reading:
        return Reading(bar, WAIT, why, h4, h1, rsi, rsi_before, atr)

    if now - end > timedelta(minutes=15):
        return wait("no new 15-minute candle lately (the market may be closed)")
    # The trends from the 1-hour and 4-hour candles completed by this candle's close.
    hours = [c for c in h1 if c.open_time + timedelta(hours=1) <= end]
    t1, t4 = _trend(hours), _trend(four_hours(hours))
    one = _fifteen(m15)
    if t1 is None or t4 is None:
        return wait("too few 1-hour or 4-hour candles for a trend yet")
    if one is None:
        return wait("too few 15-minute candles yet", h4=t4, h1=t1)
    atr, rsi, before, ema, close = one
    seen = (t4, t1, rsi, before, atr)
    if t4 != "UP" or t1 != "UP":
        return wait(
            f"the 4-hour trend is {t4} and the 1-hour {t1}: BUY only when both are UP", *seen
        )
    if not (before <= 50 < rsi):
        return wait("the 15-minute momentum has not just turned up (RSI through 50)", *seen)
    if close <= ema:
        return wait("the 15-minute close is not above its 21-candle average", *seen)
    return Reading(
        bar,
        BUY,
        "both trends UP and the 15-minute momentum just turned up",
        t4,
        t1,
        rsi,
        before,
        atr,
    )


@dataclass(frozen=True)
class Levels:
    """One TEST trade's prices at HFM, from the price now."""

    limit: Decimal  # the entry: at the ask or better, at most this
    stop: Decimal
    take_profit: Decimal
    distance: Decimal  # from the entry to the stop


def _tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding) * tick


def levels(
    atr: float, bid: Decimal, ask: Decimal, tick: Decimal, slippage: Decimal
) -> Levels | str:
    """The entry, stop-loss and take-profit of a BUY now, or why there is none (the spread is
    too large a part of the stop: the lab's cost floor)."""
    tick = tick.normalize()
    distance = _tick(STOP_ATR * Decimal(str(atr)), tick, ROUND_CEILING)
    spread = ask - bid
    if distance <= 0 or spread < 0:
        return "no price or no volatility to set the stop by"
    if spread > COST_FLOOR * distance:
        return (
            f"HFM's spread ({spread:.2f}) is more than 5 % of the stop distance "
            f"({distance:.2f}): the cost would eat the trade"
        )
    return Levels(
        _tick(ask + slippage, tick, ROUND_CEILING),
        _tick(ask - distance, tick, ROUND_DOWN),
        _tick(ask + REWARD * distance, tick, ROUND_DOWN),
        distance,
    )


def ounces(
    risk: Decimal, distance: Decimal, slippage: Decimal, fee: Decimal, step: Decimal
) -> Decimal:
    """How many ounces risk at most ``risk`` at the stop, the slippage allowance and the fee
    (both ways, an ounce) included, in HFM's volume steps (rounded down). 0 when not even one
    step fits."""
    per_ounce = distance + slippage + fee
    if per_ounce <= 0 or step <= 0:
        return Decimal(0)
    return ((risk / per_ounce) / step).to_integral_value(ROUND_DOWN) * step
