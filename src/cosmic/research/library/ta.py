"""Indicators for the strategy library: full series, causal, ``None`` during warm-up.

Every function returns a list aligned with its input in which element ``t`` is computed from
inputs ``0..t`` only. ``tests/unit/test_library_causality.py`` checks this for every
strategy by recomputing on truncated histories. Floats: analytics, never ledger values
(ADR-007). Sources are named on the strategies that use these indicators.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from cosmic.market import indicators as base

Series = list[float | None]
sma = base.sma
rsi = base.rsi
atr = base.atr
true_range = base.true_range


def _first(values: Sequence[float | None]) -> int:
    """Index of the first defined value (len if none)."""
    for i, v in enumerate(values):
        if v is not None:
            return i
    return len(values)


def ema(values: Sequence[float | None], n: int) -> Series:
    """EMA (alpha 2/(n+1)) of a series that may start with ``None``; SMA-seeded."""
    start = _first(values)
    out: Series = [None] * len(values)
    tail = [float(v) for v in values[start:] if v is not None]
    if len(tail) != len(values) - start:
        raise ValueError("ema input has gaps after its first value")
    for i, v in enumerate(base.ema(tail, n)):
        out[start + i] = v
    return out


def wilder(values: Sequence[float | None], n: int) -> Series:
    """Wilder smoothing (alpha 1/n), SMA-seeded — as in RSI, ATR and ADX."""
    start = _first(values)
    out: Series = [None] * len(values)
    if len(values) - start < n:
        return out
    prev = sum(float(v) for v in values[start : start + n] if v is not None) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        v = values[i]
        if v is None:
            raise ValueError("wilder input has gaps")
        prev = (prev * (n - 1) + v) / n
        out[i] = prev
    return out


def sma_opt(values: Sequence[float | None], n: int) -> Series:
    start = _first(values)
    out: Series = [None] * len(values)
    tail = [float(v) for v in values[start:] if v is not None]
    for i, v in enumerate(base.sma(tail, n)):
        out[start + i] = v
    return out


def wma(values: Sequence[float | None], n: int) -> Series:
    start = _first(values)
    out: Series = [None] * len(values)
    weights = list(range(1, n + 1))
    total = sum(weights)
    for i in range(start + n - 1, len(values)):
        window = values[i - n + 1 : i + 1]
        out[i] = sum(
            w * float(v) for w, v in zip(weights, window, strict=True) if v is not None
        ) / (total)
    return out


def hma(values: Sequence[float], n: int) -> Series:
    """Hull moving average (Alan Hull, 2005): WMA(2·WMA(n/2) − WMA(n), √n)."""
    half, full = wma(values, max(1, n // 2)), wma(values, n)
    raw: Series = [
        None if a is None or b is None else 2 * a - b for a, b in zip(half, full, strict=True)
    ]
    return wma(raw, max(1, round(math.sqrt(n))))


def dema(values: Sequence[float], n: int) -> Series:
    """Double EMA (Mulloy, 1994): 2·EMA − EMA(EMA)."""
    e1 = ema(values, n)
    e2 = ema(e1, n)
    return [None if a is None or b is None else 2 * a - b for a, b in zip(e1, e2, strict=True)]


def tema(values: Sequence[float], n: int) -> Series:
    """Triple EMA (Mulloy, 1994): 3·E1 − 3·E2 + E3."""
    e1 = ema(values, n)
    e2 = ema(e1, n)
    e3 = ema(e2, n)
    return [
        None if a is None or b is None or c is None else 3 * a - 3 * b + c
        for a, b, c in zip(e1, e2, e3, strict=True)
    ]


def kama(values: Sequence[float], n: int = 10, fast: int = 2, slow: int = 30) -> Series:
    """Kaufman adaptive moving average (Kaufman, *Smarter Trading*, 1995)."""
    out: Series = [None] * len(values)
    if len(values) <= n:
        return out
    fast_sc, slow_sc = 2 / (fast + 1), 2 / (slow + 1)
    prev = values[n - 1]
    out[n - 1] = prev
    for i in range(n, len(values)):
        change = abs(values[i] - values[i - n])
        vol = sum(abs(values[k] - values[k - 1]) for k in range(i - n + 1, i + 1))
        er = change / vol if vol > 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = prev + sc * (values[i] - prev)
        out[i] = prev
    return out


def efficiency_ratio(values: Sequence[float], n: int) -> Series:
    """Kaufman's efficiency ratio: net change / sum of absolute changes over n bars."""
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        vol = sum(abs(values[k] - values[k - 1]) for k in range(i - n + 1, i + 1))
        out[i] = abs(values[i] - values[i - n]) / vol if vol > 0 else 0.0
    return out


def highest(values: Sequence[float], n: int) -> Series:
    """Max of the last n values INCLUDING t."""
    out: Series = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = max(values[i - n + 1 : i + 1])
    return out


def lowest(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = min(values[i - n + 1 : i + 1])
    return out


def prior_highest(values: Sequence[float], n: int) -> Series:
    """Max of the n values BEFORE t (a channel that a close at t can break)."""
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = max(values[i - n : i])
    return out


def prior_lowest(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = min(values[i - n : i])
    return out


def rolling_std(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1 : i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / (n - 1)) if n > 1 else 0.0
    return out


def zscore(values: Sequence[float], n: int) -> Series:
    mean, sd = sma(values, n), rolling_std(values, n)
    return [
        None if m is None or s is None or s == 0 else (v - m) / s
        for v, m, s in zip(values, mean, sd, strict=True)
    ]


def roc(values: Sequence[float], n: int) -> Series:
    """Rate of change over n bars (fraction)."""
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = values[i] / values[i - n] - 1 if values[i - n] > 0 else None
    return out


def log_returns(values: Sequence[float]) -> Series:
    out: Series = [None] * len(values)
    for i in range(1, len(values)):
        if values[i] > 0 and values[i - 1] > 0:
            out[i] = math.log(values[i] / values[i - 1])
    return out


def realized_vol(values: Sequence[float], n: int, periods_per_year: float = 365.0) -> Series:
    r = log_returns(values)
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        w = [x for x in r[i - n + 1 : i + 1] if x is not None]
        if len(w) == n:
            m = sum(w) / n
            out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / (n - 1) * periods_per_year)
    return out


def linreg_slope(values: Sequence[float], n: int) -> Series:
    """Least-squares slope over the last n values, as a fraction of the window mean per bar."""
    out: Series = [None] * len(values)
    xs = list(range(n))
    xm = (n - 1) / 2
    sxx = sum((x - xm) ** 2 for x in xs)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1 : i + 1]
        ym = sum(w) / n
        slope = sum((x - xm) * (y - ym) for x, y in zip(xs, w, strict=True)) / sxx
        out[i] = slope / ym if ym else None
    return out


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    """MACD line, signal line and histogram (Appel, 1979)."""
    f, s = ema(closes, fast), ema(closes, slow)
    line: Series = [None if a is None or b is None else a - b for a, b in zip(f, s, strict=True)]
    sig = ema(line, signal)
    hist: Series = [
        None if a is None or b is None else a - b for a, b in zip(line, sig, strict=True)
    ]
    return line, sig, hist


def bollinger(
    closes: Sequence[float], n: int = 20, k: float = 2.0
) -> tuple[Series, Series, Series]:
    mid, sd = sma(closes, n), rolling_std(closes, n)
    upper: Series = [
        None if m is None or s is None else m + k * s for m, s in zip(mid, sd, strict=True)
    ]
    lower: Series = [
        None if m is None or s is None else m - k * s for m, s in zip(mid, sd, strict=True)
    ]
    return lower, mid, upper


def keltner(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    n: int = 20,
    k: float = 2.0,
    atr_n: int = 10,
) -> tuple[Series, Series, Series]:
    mid, a = ema(close, n), atr(high, low, close, atr_n)
    upper: Series = [
        None if m is None or x is None else m + k * x for m, x in zip(mid, a, strict=True)
    ]
    lower: Series = [
        None if m is None or x is None else m - k * x for m, x in zip(mid, a, strict=True)
    ]
    return lower, mid, upper


def stochastic(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14, d: int = 3
) -> tuple[Series, Series]:
    """%K and %D (George Lane)."""
    hh, ll = highest(high, n), lowest(low, n)
    k: Series = [
        None if h is None or lo is None else (100.0 if h == lo else 100 * (c - lo) / (h - lo))
        for c, h, lo in zip(close, hh, ll, strict=True)
    ]
    return k, sma_opt(k, d)


def williams_r(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14
) -> Series:
    hh, ll = highest(high, n), lowest(low, n)
    return [
        None if h is None or lo is None else (-50.0 if h == lo else -100 * (h - c) / (h - lo))
        for c, h, lo in zip(close, hh, ll, strict=True)
    ]


def cci(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 20) -> Series:
    """Commodity channel index (Lambert, 1980)."""
    tp = [(h + lo + c) / 3 for h, lo, c in zip(high, low, close, strict=True)]
    m = sma(tp, n)
    out: Series = [None] * len(tp)
    for i in range(n - 1, len(tp)):
        mi = m[i]
        assert mi is not None
        md = sum(abs(x - mi) for x in tp[i - n + 1 : i + 1]) / n
        out[i] = (tp[i] - mi) / (0.015 * md) if md > 0 else 0.0
    return out


def dmi(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14
) -> tuple[Series, Series, Series]:
    """+DI, −DI and ADX (Wilder, *New Concepts in Technical Trading Systems*, 1978)."""
    size = len(close)
    plus_dm: list[float | None] = [None] + [
        max(high[i] - high[i - 1], 0.0) if high[i] - high[i - 1] > low[i - 1] - low[i] else 0.0
        for i in range(1, size)
    ]
    minus_dm: list[float | None] = [None] + [
        max(low[i - 1] - low[i], 0.0) if low[i - 1] - low[i] > high[i] - high[i - 1] else 0.0
        for i in range(1, size)
    ]
    tr: list[float | None] = [None, *base.true_range(high, low, close)[1:]]
    s_tr, s_p, s_m = wilder(tr, n), wilder(plus_dm, n), wilder(minus_dm, n)
    pdi: Series = []
    mdi: Series = []
    dx: Series = []
    for t, p, m in zip(s_tr, s_p, s_m, strict=True):
        if t is None or p is None or m is None or t == 0:
            pdi.append(None)
            mdi.append(None)
            dx.append(None)
            continue
        a, b = 100 * p / t, 100 * m / t
        pdi.append(a)
        mdi.append(b)
        dx.append(100 * abs(a - b) / (a + b) if a + b > 0 else 0.0)
    return pdi, mdi, wilder(dx, n)


def aroon(high: Sequence[float], low: Sequence[float], n: int = 25) -> tuple[Series, Series]:
    """Aroon up/down (Chande, 1995): time since the n-bar high/low, scaled 0–100."""
    up: Series = [None] * len(high)
    down: Series = [None] * len(high)
    for i in range(n, len(high)):
        wh, wl = high[i - n : i + 1], low[i - n : i + 1]  # n + 1 bars, most recent last
        bars_since_high = wh[::-1].index(max(wh))  # 0 when the high is today
        bars_since_low = wl[::-1].index(min(wl))
        up[i] = 100 * (n - bars_since_high) / n
        down[i] = 100 * (n - bars_since_low) / n
    return up, down


def psar(
    high: Sequence[float], low: Sequence[float], step: float = 0.02, cap: float = 0.2
) -> list[bool | None]:
    """Parabolic SAR (Wilder, 1978). Returns True while the SAR is below price (uptrend)."""
    out: list[bool | None] = [None] * len(high)
    if len(high) < 2:
        return out
    up = high[1] >= high[0]
    sar = low[0] if up else high[0]
    ep = high[1] if up else low[1]
    af = step
    out[1] = up
    for i in range(2, len(high)):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, low[i - 1], low[i - 2])
            if low[i] < sar:
                up, sar, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(cap, af + step)
        else:
            sar = max(sar, high[i - 1], high[i - 2])
            if high[i] > sar:
                up, sar, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(cap, af + step)
        out[i] = up
    return out


def supertrend(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 10, k: float = 3.0
) -> list[bool | None]:
    """SuperTrend (Olivier Seban): True while in the up regime. Band logic per the common
    definition (final bands ratchet; flip on a close through the opposite band)."""
    a = atr(high, low, close, n)
    out: list[bool | None] = [None] * len(close)
    fu = fl = 0.0
    up = True
    started = False
    for i in range(len(close)):
        ai = a[i]
        if ai is None:
            continue
        mid = (high[i] + low[i]) / 2
        bu, bl = mid + k * ai, mid - k * ai
        if not started:
            fu, fl, up, started = bu, bl, close[i] >= mid, True
        else:
            fu = bu if bu < fu or close[i - 1] > fu else fu
            fl = bl if bl > fl or close[i - 1] < fl else fl
            if up and close[i] < fl:
                up = False
            elif not up and close[i] > fu:
                up = True
        out[i] = up
    return out


def ichimoku(
    high: Sequence[float],
    low: Sequence[float],
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b: int = 52,
) -> tuple[Series, Series, Series, Series]:
    """Tenkan, kijun and the cloud (senkou A/B) AS PLOTTED AT t — i.e. computed kijun bars
    earlier, so the cloud at t uses no data after t − kijun (no look-ahead)."""

    def mid(n: int) -> Series:
        hh, ll = highest(high, n), lowest(low, n)
        return [
            None if h is None or lo is None else (h + lo) / 2 for h, lo in zip(hh, ll, strict=True)
        ]

    t, k, b = mid(tenkan), mid(kijun), mid(senkou_b)
    a_now: Series = [
        None if x is None or y is None else (x + y) / 2 for x, y in zip(t, k, strict=True)
    ]
    shift = kijun
    span_a: Series = [None] * shift
    span_a += a_now[: len(a_now) - shift]
    span_b: Series = [None] * shift
    span_b += b[: len(b) - shift]
    return t, k, span_a, span_b


def vortex(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14
) -> tuple[Series, Series]:
    """Vortex indicator (Botes & Siepman, 2010)."""
    size = len(close)
    vp = [0.0] + [abs(high[i] - low[i - 1]) for i in range(1, size)]
    vm = [0.0] + [abs(low[i] - high[i - 1]) for i in range(1, size)]
    tr = base.true_range(high, low, close)
    plus: Series = [None] * size
    minus: Series = [None] * size
    for i in range(n, size):
        s = sum(tr[i - n + 1 : i + 1])
        if s > 0:
            plus[i] = sum(vp[i - n + 1 : i + 1]) / s
            minus[i] = sum(vm[i - n + 1 : i + 1]) / s
    return plus, minus


def trix(closes: Sequence[float], n: int = 15) -> Series:
    """TRIX (Hutson, 1983): 1-bar rate of change of a triple-smoothed EMA."""
    e3 = ema(ema(ema(closes, n), n), n)
    out: Series = [None] * len(closes)
    for i in range(1, len(closes)):
        a, b = e3[i], e3[i - 1]
        if a is not None and b is not None and b != 0:
            out[i] = a / b - 1
    return out


def kst(closes: Sequence[float]) -> tuple[Series, Series]:
    """Know Sure Thing (Pring), daily parameters: ROC 10/15/20/30 smoothed 10/10/10/15,
    weights 1–4, signal SMA 9."""
    parts = [(10, 10, 1), (15, 10, 2), (20, 10, 3), (30, 15, 4)]
    total: list[float | None] = [0.0] * len(closes)
    for r, s, w in parts:
        sm = sma_opt(roc(closes, r), s)
        total = [
            None if t is None or x is None else t + w * x for t, x in zip(total, sm, strict=True)
        ]
    return total, sma_opt(total, 9)


def coppock(closes: Sequence[float], r1: int = 14, r2: int = 11, n: int = 10) -> Series:
    """Coppock curve (Coppock, 1962) on monthly-equivalent lengths supplied by the caller."""
    a, b = roc(closes, r1), roc(closes, r2)
    s: Series = [None if x is None or y is None else x + y for x, y in zip(a, b, strict=True)]
    return wma(s, n)


def obv(close: Sequence[float], volume: Sequence[float]) -> list[float]:
    """On-balance volume (Granville, 1963)."""
    out = [0.0]
    for i in range(1, len(close)):
        sign = 1 if close[i] > close[i - 1] else -1 if close[i] < close[i - 1] else 0
        out.append(out[-1] + sign * volume[i])
    return out


def _mfm(h: float, lo: float, c: float) -> float:
    return ((c - lo) - (h - c)) / (h - lo) if h > lo else 0.0


def cmf(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    volume: Sequence[float],
    n: int = 20,
) -> Series:
    """Chaikin money flow."""
    mfv = [_mfm(h, lo, c) * v for h, lo, c, v in zip(high, low, close, volume, strict=True)]
    out: Series = [None] * len(close)
    for i in range(n - 1, len(close)):
        vs = sum(volume[i - n + 1 : i + 1])
        out[i] = sum(mfv[i - n + 1 : i + 1]) / vs if vs > 0 else 0.0
    return out


def ad_line(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], volume: Sequence[float]
) -> list[float]:
    """Accumulation/distribution line (Chaikin)."""
    out: list[float] = []
    acc = 0.0
    for h, lo, c, v in zip(high, low, close, volume, strict=True):
        acc += _mfm(h, lo, c) * v
        out.append(acc)
    return out


def mfi(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    volume: Sequence[float],
    n: int = 14,
) -> Series:
    """Money flow index (Quong & Soudack, 1989)."""
    tp = [(h + lo + c) / 3 for h, lo, c in zip(high, low, close, strict=True)]
    out: Series = [None] * len(tp)
    for i in range(n, len(tp)):
        pos = sum(tp[k] * volume[k] for k in range(i - n + 1, i + 1) if tp[k] > tp[k - 1])
        neg = sum(tp[k] * volume[k] for k in range(i - n + 1, i + 1) if tp[k] < tp[k - 1])
        out[i] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    return out


def rolling_vwap(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    volume: Sequence[float],
    n: int = 20,
) -> Series:
    tp = [(h + lo + c) / 3 for h, lo, c in zip(high, low, close, strict=True)]
    out: Series = [None] * len(tp)
    for i in range(n - 1, len(tp)):
        vs = sum(volume[i - n + 1 : i + 1])
        out[i] = sum(tp[k] * volume[k] for k in range(i - n + 1, i + 1)) / vs if vs > 0 else None
    return out


def choppiness(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14
) -> Series:
    """Choppiness index (E. W. Dreiss): 100·log10(ΣATR1 / (HH − LL)) / log10(n)."""
    tr = base.true_range(high, low, close)
    hh, ll = highest(high, n), lowest(low, n)
    out: Series = [None] * len(close)
    for i in range(n, len(close)):
        h, lo = hh[i], ll[i]
        if h is None or lo is None or h <= lo:
            continue
        out[i] = 100 * math.log10(sum(tr[i - n + 1 : i + 1]) / (h - lo)) / math.log10(n)
    return out


def streak(closes: Sequence[float]) -> list[float]:
    """Consecutive up (+) / down (−) closes, as used by ConnorsRSI."""
    out = [0.0]
    for i in range(1, len(closes)):
        prev = out[-1]
        if closes[i] > closes[i - 1]:
            out.append(prev + 1 if prev > 0 else 1.0)
        elif closes[i] < closes[i - 1]:
            out.append(prev - 1 if prev < 0 else -1.0)
        else:
            out.append(0.0)
    return out


def percent_rank(values: Sequence[float | None], n: int) -> Series:
    """Share of the previous n values strictly below the current one (0–100)."""
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        v = values[i]
        window = values[i - n : i]
        if v is None or any(x is None for x in window):
            continue
        out[i] = 100 * sum(1 for x in window if x is not None and x < v) / n
    return out


def connors_rsi(
    closes: Sequence[float], n_rsi: int = 3, n_streak: int = 2, n_rank: int = 100
) -> Series:
    """ConnorsRSI (Connors Research, 2012): mean of RSI(3), RSI(2) of the streak and the
    100-bar percent rank of the 1-bar return."""
    a = rsi(closes, n_rsi)
    b = rsi(streak(closes), n_streak)
    c = percent_rank(roc(closes, 1), n_rank)
    return [
        None if x is None or y is None or z is None else (x + y + z) / 3
        for x, y, z in zip(a, b, c, strict=True)
    ]


def ibs(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> list[float]:
    """Internal bar strength: (close − low) / (high − low)."""
    return [
        (c - lo) / (h - lo) if h > lo else 0.5 for h, lo, c in zip(high, low, close, strict=True)
    ]


def heikin_ashi(
    open_: Sequence[float], high: Sequence[float], low: Sequence[float], close: Sequence[float]
) -> tuple[list[float], list[float]]:
    """Heikin-Ashi open and close (Valcu, 2004)."""
    ha_close = [(o + h + lo + c) / 4 for o, h, lo, c in zip(open_, high, low, close, strict=True)]
    ha_open = [(open_[0] + close[0]) / 2]
    for i in range(1, len(close)):
        ha_open.append((ha_open[-1] + ha_close[i - 1]) / 2)
    return ha_open, ha_close
