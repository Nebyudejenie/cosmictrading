"""Deterministic synthetic hourly bars for research tests (labelled synthetic; tests only)."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmic.market.data import Bar
from cosmic.research.strategy import TrendSmaAtr

END = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)  # last complete hourly bar before 21:06 UTC


def series(n: int = 420, seed: int = 11, base: float = 80000.0) -> list[float]:
    rng = random.Random(seed)
    closes, p = [], base
    for i in range(n):
        drift = -0.0007 if i < n * 0.6 else 0.0016
        p *= math.exp(drift + rng.gauss(0, 0.0012))
        closes.append(p)
    return closes


def to_bars(closes: list[float], end: datetime = END) -> list[Bar]:
    bars, prev = [], closes[0]
    start = end - timedelta(hours=len(closes) - 1)
    for i, c in enumerate(closes):
        o = prev
        hi, lo = max(o, c) * 1.0008, min(o, c) * 0.9992
        bars.append(
            Bar(
                start + timedelta(hours=i),
                Decimal(f"{o:.2f}"),
                Decimal(f"{hi:.2f}"),
                Decimal(f"{lo:.2f}"),
                Decimal(f"{c:.2f}"),
                Decimal("12.5"),
                True,
            )
        )
        prev = c
    return bars


def entry_signal_bars(target_last: float = 82986.5) -> list[Bar]:
    """Bars truncated exactly at the first ENTER_LONG crossover, rescaled so the last close ≈ target."""
    closes = series()
    strat = TrendSmaAtr()
    bars = to_bars(closes)
    for i in range(strat.warmup() + 5, len(bars)):
        if strat.signal(bars[: i + 1]).action == "ENTER_LONG":
            cut = closes[: i + 1]
            scale = target_last / cut[-1]
            return to_bars([c * scale for c in cut])
    raise AssertionError("synthetic series produced no entry signal")
