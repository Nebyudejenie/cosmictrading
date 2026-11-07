"""Gold read on several timeframes (S61): BUY, SELL or WAIT, with the reasons.

HFM's own candles only (``market.hfm_candles``). Pure: it reads candles and returns a reading;
the simulator and the risk checks decide what happens. The rules are a forward test, not a proven
edge (ADR-037):

- **every timeframe:** trend from two EMAs (8 and 21) and where the price is; momentum from
  RSI(14); volatility from ATR(14); support and resistance, the low and high of the last 20
  candles;
- **BUY:** the hourly trend is up, the daily trend is not down, and on 1-minute candles the RSI
  crosses up through 50 with the price above its 21 EMA;
- **SELL:** the mirror: hourly down, daily not up, the 1-minute RSI crossing down through 50
  with the price below its 21 EMA;
- **WAIT:** otherwise; also when the hourly volatility is too quiet or too wild to place a stop,
  or the candles are too few or too old;
- **stop-loss:** 1.5 × the hourly ATR from the entry. The take-profit is a multiple of that
  distance, the owner's setting.

The daily trend filters only once 21 daily candles of HFM exist; until then the reading says how
many there are, and decides on the hourly and the 1-minute candles.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from cosmic.market import indicators as ind
from cosmic.market.hfm_candles import Candle

FAST, SLOW, RSI_N, ATR_N, RANGE_N = 8, 21, 14, 14, 20
ENOUGH = SLOW  # candles a timeframe needs for a trend
STOP_ATR = Decimal("1.5")  # the stop-loss, in hourly ATRs from the entry
QUIET, WILD = 0.0005, 0.015  # hourly ATR as a share of the price: below or above, WAIT
STALE = {"1m": timedelta(minutes=3), "1h": timedelta(hours=2)}  # last complete candle's age
TIMEFRAMES = ("1m", "5m", "15m", "1h", "1d")
NAMES = {"1m": "M1", "5m": "M5", "15m": "M15", "1h": "H1", "1d": "D1"}
UP, DOWN, SIDEWAYS, UNKNOWN = "UP", "DOWN", "SIDEWAYS", "NOT ENOUGH DATA"
BUY, SELL, WAIT = "BUY", "SELL", "WAIT"


@dataclass(frozen=True)
class Frame:
    """One timeframe's reading."""

    tf: str
    candles: int
    last_time: datetime | None = None
    close: float | None = None
    trend: str = UNKNOWN
    ema_fast: float | None = None
    ema_slow: float | None = None
    rsi: float | None = None
    rsi_before: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    support: float | None = None
    resistance: float | None = None

    @property
    def name(self) -> str:
        return NAMES[self.tf]


@dataclass(frozen=True)
class Reading:
    at: datetime
    frames: dict[str, Frame]
    decision: str
    reasons: list[str] = field(default_factory=list)
    stop_distance: Decimal | None = None  # per ounce
    bar: datetime | None = None  # the 1-minute candle it decided on


def frame(tf: str, candles: Sequence[Candle]) -> Frame:
    if not candles:
        return Frame(tf, 0)
    close = [float(c.close) for c in candles]
    high = [float(c.high) for c in candles]
    low = [float(c.low) for c in candles]
    base = Frame(
        tf,
        len(candles),
        last_time=candles[-1].open_time,
        close=close[-1],
        support=min(low[-RANGE_N:]),
        resistance=max(high[-RANGE_N:]),
    )
    rsi = ind.rsi(close, RSI_N) if len(close) > RSI_N else []
    atr = ind.atr(high, low, close, ATR_N) if len(close) > ATR_N else []
    last_atr = atr[-1] if atr else None
    fast = ind.ema(close, FAST)[-1] if len(close) >= FAST else None
    slow = ind.ema(close, SLOW)[-1] if len(close) >= SLOW else None
    trend = UNKNOWN
    if fast is not None and slow is not None:
        if fast > slow and close[-1] > fast:
            trend = UP
        elif fast < slow and close[-1] < fast:
            trend = DOWN
        else:
            trend = SIDEWAYS
    return Frame(
        tf,
        len(candles),
        last_time=base.last_time,
        close=base.close,
        trend=trend,
        ema_fast=fast,
        ema_slow=slow,
        rsi=rsi[-1] if rsi else None,
        rsi_before=rsi[-2] if len(rsi) > 1 else None,
        atr=last_atr,
        atr_pct=last_atr / close[-1] if last_atr and close[-1] else None,
        support=base.support,
        resistance=base.resistance,
    )


def read(at: datetime, candles: Mapping[str, Sequence[Candle]]) -> Reading:
    """The reading at ``at`` from each timeframe's complete candles (oldest first)."""
    frames = {tf: frame(tf, candles.get(tf, ())) for tf in TIMEFRAMES}
    m1, h1, d1 = frames["1m"], frames["1h"], frames["1d"]
    reasons: list[str] = []
    bar = m1.last_time
    stop: Decimal | None = None  # known once the hourly volatility is usable

    def wait(*why: str) -> Reading:
        # A WAIT keeps the stop distance once it is known, so the owner sees what a trade
        # would be now; only BUY and SELL act on it.
        return Reading(at, frames, WAIT, [*reasons, *why], stop, bar)

    for f in (m1, h1):
        if f.candles < ENOUGH:
            return wait(f"{f.name}: {f.candles} of {ENOUGH} candles stored so far")
        if f.last_time is None or at - f.last_time > STALE[f.tf] + _length(f.tf):
            return wait(f"{f.name}: no new candle lately (the market may be closed)")
    assert h1.atr is not None and h1.atr_pct is not None and h1.close is not None
    if h1.atr_pct < QUIET:
        return wait(f"H1: too quiet to place a stop (ATR {h1.atr_pct:.3%} of the price)")
    if h1.atr_pct > WILD:
        return wait(f"H1: too wild (ATR {h1.atr_pct:.2%} of the price)")
    reasons.append(f"H1 trend {h1.trend}")
    daily = d1.trend if d1.candles >= ENOUGH else UNKNOWN
    reasons.append(
        f"D1 trend {daily}"
        if daily != UNKNOWN
        else f"D1: {d1.candles} of {ENOUGH} days stored, not used yet"
    )
    stop = (STOP_ATR * Decimal(str(round(h1.atr, 4)))).quantize(Decimal("0.01"))
    crossed_up = _crossed(m1, up=True)
    crossed_down = _crossed(m1, up=False)
    assert m1.close is not None
    if h1.trend == UP and daily != DOWN:
        if crossed_up and m1.ema_slow is not None and m1.close > m1.ema_slow:
            return Reading(
                at, frames, BUY, [*reasons, "M1 momentum turned up above its average"], stop, bar
            )
        return wait("M1: no upturn yet (waiting for momentum to turn up)")
    if h1.trend == DOWN and daily != UP:
        if crossed_down and m1.ema_slow is not None and m1.close < m1.ema_slow:
            return Reading(
                at,
                frames,
                SELL,
                [*reasons, "M1 momentum turned down below its average"],
                stop,
                bar,
            )
        return wait("M1: no downturn yet (waiting for momentum to turn down)")
    if h1.trend in (UP, DOWN):
        return wait(f"D1 trend {daily} against H1 {h1.trend}")
    return wait("H1: no clear trend")


def last_stop(reading: Reading) -> Decimal | None:
    """The stop distance from the stored hourly candles however old (the market closed for the
    weekend): to tell whether an amount carries HFM's smallest trade, never to trade on (S72)."""
    if reading.stop_distance is not None:
        return reading.stop_distance
    h1 = reading.frames.get("1h")
    if h1 is None or h1.candles < ENOUGH or h1.atr is None:
        return None
    return (STOP_ATR * Decimal(str(round(h1.atr, 4)))).quantize(Decimal("0.01"))


def _crossed(f: Frame, *, up: bool) -> bool:
    if f.rsi is None or f.rsi_before is None:
        return False
    return f.rsi_before <= 50 < f.rsi if up else f.rsi_before >= 50 > f.rsi


def _length(tf: str) -> timedelta:
    return timedelta(days=1) if tf == "1d" else timedelta(seconds={"1m": 60, "1h": 3600}[tf])
