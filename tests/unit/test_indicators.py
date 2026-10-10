"""Indicator reference tests: hand-computed values plus an independent naive implementation."""

from __future__ import annotations

import math
import random

import pytest

from cosmic.market import indicators as ind


def approx(a: float | None, b: float, tol: float = 1e-9) -> bool:
    return a is not None and abs(a - b) <= tol * max(1.0, abs(b))


def test_sma_and_ema_hand_values() -> None:
    v = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert ind.sma(v, 3) == [None, None, 2.0, 3.0, 4.0]
    e = ind.ema(v, 3)  # alpha = 0.5, seed = 2
    assert e[:2] == [None, None]
    assert approx(e[2], 2.0) and approx(e[3], 3.0) and approx(e[4], 4.0)
    e2 = ind.ema([2.0, 4.0, 6.0, 10.0], 2)  # alpha 2/3, seed 3 → 3+(6-3)*2/3=5 → 5+(10-5)*2/3
    assert approx(e2[2], 5.0) and approx(e2[3], 5.0 + 5.0 * 2 / 3)


def naive_rsi(closes: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    ag = sum(max(closes[i] - closes[i - 1], 0) for i in range(1, n + 1)) / n
    al = sum(max(closes[i - 1] - closes[i], 0) for i in range(1, n + 1)) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def test_rsi_matches_independent_implementation() -> None:
    rng = random.Random(7)
    closes = [100.0]
    for _ in range(300):
        closes.append(closes[-1] * math.exp(rng.gauss(0, 0.01)))
    got, want = ind.rsi(closes, 14), naive_rsi(closes, 14)
    assert got[:14] == [None] * 14
    assert all(approx(g, w) for g, w in zip(got[14:], want[14:], strict=True))  # type: ignore[arg-type]
    assert all(0 <= x <= 100 for x in got[14:] if x is not None)


def test_rsi_extremes() -> None:
    assert ind.rsi([float(i) for i in range(1, 30)], 14)[-1] == 100.0
    assert ind.rsi([float(30 - i) for i in range(29)], 14)[-1] == 0.0
    assert ind.rsi([5.0] * 30, 14)[-1] == 50.0


def test_atr_hand_values() -> None:
    high, low, close = [10.0, 12.0, 13.0], [8.0, 9.0, 11.0], [9.0, 11.0, 12.0]
    assert ind.true_range(high, low, close) == [2.0, 3.0, 2.0]
    a = ind.atr(high, low, close, 2)
    assert a[0] is None and approx(a[1], 2.5) and approx(a[2], (2.5 * 1 + 2.0) / 2)


def test_bollinger_constant_and_known() -> None:
    b = ind.bollinger([5.0] * 25, 20, 2)
    assert approx(b.upper[-1], 5.0) and approx(b.lower[-1], 5.0)
    b2 = ind.bollinger([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0], 8, 1)  # mean 5, population sd 2
    assert approx(b2.middle[-1], 5.0) and approx(b2.upper[-1], 7.0) and approx(b2.lower[-1], 3.0)


def test_macd_alignment() -> None:
    closes = [100 + math.sin(i / 5) * 3 for i in range(80)]
    m = ind.macd(closes)
    assert m.macd[24] is None and m.macd[25] is not None
    assert m.signal[32] is None and m.signal[33] is not None
    assert approx(m.histogram[50], m.macd[50] - m.signal[50])  # type: ignore[operator]
    with pytest.raises(ValueError, match="fast"):
        ind.macd(closes, 26, 12)


def test_volatility_drawdown_correlation() -> None:
    closes = [100.0, 110.0, 99.0, 108.9]
    r = ind.log_returns(closes)
    assert approx(r[0], math.log(1.1))
    vol = ind.realized_volatility(closes, 8760)
    assert approx(vol, ind.stdev(r) * math.sqrt(8760))
    dd = ind.max_drawdown([100, 120, 90, 130, 65])
    assert approx(dd.max_drawdown, 0.5) and (dd.peak_index, dd.trough_index) == (3, 4)
    assert approx(ind.correlation([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)
    assert approx(ind.correlation([1, 2, 3, 4], [8, 6, 4, 2]), -1.0)
    with pytest.raises(ValueError):
        ind.correlation([1, 1, 1], [1, 2, 3])
    with pytest.raises(ValueError):
        ind.log_returns([1.0, 0.0])


def test_no_lookahead_property() -> None:
    """Values at index i must not change when data after i changes."""
    rng = random.Random(3)
    base = [100 + rng.random() for _ in range(120)]
    altered = base[:60] + [x * 5 for x in base[60:]]
    for fn in (
        lambda s: ind.sma(s, 10),
        lambda s: ind.ema(s, 10),
        lambda s: ind.rsi(s, 14),
        lambda s: ind.macd(s).macd,
        lambda s: ind.bollinger(s).upper,
    ):
        a, b = fn(base), fn(altered)
        assert a[:60] == b[:60]
