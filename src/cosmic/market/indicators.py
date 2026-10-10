"""Deterministic technical indicators (QNT-03).

Analytics use binary floats; they are estimates, never ledger truth (ADR-007).
Every series function returns a list aligned with its input, with ``None`` during
warm-up, so callers can never accidentally read a value computed from future data.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Series = list[float | None]


def _check_period(n: int) -> None:
    if n < 1:
        raise ValueError("period must be >= 1")


def sma(values: Sequence[float], n: int) -> Series:
    _check_period(n)
    out: Series = [None] * len(values)
    window = 0.0
    for i, v in enumerate(values):
        window += v
        if i >= n:
            window -= values[i - n]
        if i >= n - 1:
            out[i] = window / n
    return out


def ema(values: Sequence[float], n: int) -> Series:
    """Exponential moving average, alpha = 2/(n+1), seeded with the SMA of the first n."""
    _check_period(n)
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    """Wilder's RSI. First average = simple mean of the first n changes."""
    _check_period(n)
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(closes)):
        avg_gain = (avg_gain * (n - 1) + gains[i - 1]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i - 1]) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 50.0 if avg_gain == 0.0 else 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> list[float]:
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high/low/close lengths differ")
    tr: list[float] = []
    for i in range(len(high)):
        if i == 0:
            tr.append(high[i] - low[i])
        else:
            tr.append(
                max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
            )
    return tr


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14) -> Series:
    """Wilder's ATR: first value = mean of the first n true ranges."""
    _check_period(n)
    tr = true_range(high, low, close)
    out: Series = [None] * len(tr)
    if len(tr) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


@dataclass(frozen=True)
class Bands:
    middle: Series
    upper: Series
    lower: Series


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0) -> Bands:
    """Bollinger bands with population standard deviation over the window."""
    mid = sma(closes, n)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        m = mid[i]
        assert m is not None
        window = closes[i - n + 1 : i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return Bands(mid, upper, lower)


@dataclass(frozen=True)
class Macd:
    macd: Series
    signal: Series
    histogram: Series


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Macd:
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [
        a - b if a is not None and b is not None else None for a, b in zip(ef, es, strict=True)
    ]
    defined = [v for v in line if v is not None]
    sig_defined = ema(defined, signal)
    sig: Series = [None] * len(line)
    first = next((i for i, v in enumerate(line) if v is not None), len(line))
    for j, v in enumerate(sig_defined):
        sig[first + j] = v
    hist: Series = [
        a - b if a is not None and b is not None else None for a, b in zip(line, sig, strict=True)
    ]
    return Macd(line, sig, hist)


def log_returns(closes: Sequence[float]) -> list[float]:
    if any(c <= 0 for c in closes):
        raise ValueError("prices must be positive")
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def stdev(values: Sequence[float], ddof: int = 1) -> float:
    if len(values) <= ddof:
        raise ValueError("not enough values")
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - ddof))


def realized_volatility(closes: Sequence[float], periods_per_year: float) -> float:
    """Annualised sample standard deviation of log returns (disclosed convention)."""
    return stdev(log_returns(closes)) * math.sqrt(periods_per_year)


@dataclass(frozen=True)
class Drawdown:
    max_drawdown: float  # fraction, e.g. 0.25 = 25 % below the running peak
    peak_index: int
    trough_index: int


def max_drawdown(equity: Sequence[float]) -> Drawdown:
    if not equity:
        raise ValueError("empty series")
    peak_value = equity[0]
    peak_i = 0
    best = Drawdown(0.0, 0, 0)
    for i, v in enumerate(equity):
        if v > peak_value:
            peak_value, peak_i = v, i
        if peak_value > 0:
            dd = (peak_value - v) / peak_value
            if dd > best.max_drawdown:
                best = Drawdown(dd, peak_i, i)
    return best


def correlation(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y) or len(x) < 3:
        raise ValueError("need two equal-length series with at least 3 points")
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        raise ValueError("constant series has no correlation")
    return sxy / math.sqrt(sxx * syy)
