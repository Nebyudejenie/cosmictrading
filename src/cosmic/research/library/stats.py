"""Statistics for judging a backtest honestly.

- Probabilistic Sharpe ratio (PSR) and the Deflated Sharpe ratio (DSR): Bailey, D. H. &
  López de Prado, M. (2014), "The Deflated Sharpe Ratio: Correcting for Selection Bias,
  Backtest Overfitting and Non-Normality", *Journal of Portfolio Management* 40(5).
  DSR is the PSR measured against the Sharpe ratio the best of N unskilled trials would be
  expected to reach by luck alone; it also allows for skewed, fat-tailed returns.
- Normal quantile: Acklam's rational approximation (relative error < 1.2e-9).

All Sharpe ratios here are per period (not annualised) unless a name says otherwise.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

EULER_GAMMA = 0.5772156649015329


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


_A = (
    -39.69683028665376,
    220.9460984245205,
    -275.9285104469687,
    138.3577518672690,
    -30.66479806614716,
    2.506628277459239,
)
_B = (
    -54.47609879822406,
    161.5858368580409,
    -155.6989798598866,
    66.80131188771972,
    -13.28068155288572,
)
_C = (
    -0.007784894002430293,
    -0.3223964580411365,
    -2.400758277161838,
    -2.549732539343734,
    4.374664141464968,
    2.938163982698783,
)
_D = (0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416)


def norm_ppf(p: float) -> float:
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    low = 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1
        )
    if p > 1 - low:
        return -norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (
        (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5])
        * q
        / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1)
    )


def moments(returns: Sequence[float]) -> tuple[float, float, float, float]:
    """Mean, sample standard deviation, skewness and (non-excess) kurtosis."""
    n = len(returns)
    if n < 3:
        return 0.0, 0.0, 0.0, 3.0
    m = sum(returns) / n
    var = sum((r - m) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return m, 0.0, 0.0, 3.0
    pop = math.sqrt(sum((r - m) ** 2 for r in returns) / n)
    skew = sum(((r - m) / pop) ** 3 for r in returns) / n
    kurt = sum(((r - m) / pop) ** 4 for r in returns) / n
    return m, sd, skew, kurt


def sharpe(returns: Sequence[float]) -> float:
    m, sd, _, _ = moments(returns)
    return m / sd if sd > 0 else 0.0


def psr(sr: float, sr_benchmark: float, t: int, skew: float, kurt: float) -> float:
    """P(true Sharpe > ``sr_benchmark``) given an observed per-period ``sr`` over t returns."""
    if t < 3:
        return 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        return 0.0
    return norm_cdf((sr - sr_benchmark) * math.sqrt(t - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """Sharpe ratio the best of ``n_trials`` skill-less strategies is expected to reach."""
    if n_trials < 2 or var_sr <= 0:
        return 0.0
    return math.sqrt(var_sr) * (
        (1 - EULER_GAMMA) * norm_ppf(1 - 1 / n_trials)
        + EULER_GAMMA * norm_ppf(1 - 1 / (n_trials * math.e))
    )


def dsr(returns: Sequence[float], n_trials: int, var_sr: float) -> float:
    _, _, skew, kurt = moments(returns)
    return psr(sharpe(returns), expected_max_sharpe(n_trials, var_sr), len(returns), skew, kurt)


def variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)
