"""Market regime classification (doc 07 §5). Explicit, deterministic method.

trend      = sign(SMA50 − SMA200) with SMA50 slope over 24 bars; RANGE when they disagree
volatility = percentile of 24-bar realised volatility within the trailing window (≤ 720 bars):
             < 33 % LOW_VOL, < 67 % NORMAL_VOL, otherwise HIGH_VOL
label      = f"{trend}_{volatility}"; INSUFFICIENT_DATA with fewer than 200 complete bars
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from cosmic.market.indicators import sma

MIN_BARS = 200


@dataclass(frozen=True)
class Regime:
    trend: str  # TREND_UP | TREND_DOWN | RANGE | INSUFFICIENT_DATA
    volatility: str  # LOW_VOL | NORMAL_VOL | HIGH_VOL | INSUFFICIENT_DATA
    vol_percentile: float | None

    @property
    def label(self) -> str:
        if self.trend == "INSUFFICIENT_DATA":
            return "INSUFFICIENT_DATA"
        return f"{self.trend}_{self.volatility}"


def _rolling_vol(closes: Sequence[float], window: int) -> list[float]:
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    out = []
    for i in range(window, len(rets) + 1):
        w = rets[i - window : i]
        m = sum(w) / window
        out.append(math.sqrt(sum((x - m) ** 2 for x in w) / (window - 1)))
    return out


def classify(closes: Sequence[float], *, vol_window: int = 24, lookback: int = 720) -> Regime:
    if len(closes) < MIN_BARS:
        return Regime("INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None)
    s50, s200 = sma(closes, 50), sma(closes, 200)
    a, b, a_prev = s50[-1], s200[-1], s50[-25] if len(closes) >= 225 else s50[-1]
    assert a is not None and b is not None and a_prev is not None
    slope_up = a > a_prev
    if a > b and slope_up:
        trend = "TREND_UP"
    elif a < b and not slope_up:
        trend = "TREND_DOWN"
    else:
        trend = "RANGE"
    vols = _rolling_vol(list(closes)[-lookback:], vol_window)
    current = vols[-1]
    pct = sum(1 for v in vols if v <= current) / len(vols)
    vol = "LOW_VOL" if pct < 0.33 else ("NORMAL_VOL" if pct < 0.67 else "HIGH_VOL")
    return Regime(trend, vol, round(pct, 4))
