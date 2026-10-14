"""PSR/DSR and the normal helpers (Bailey & López de Prado, 2014)."""

from __future__ import annotations

import random

import pytest

from cosmic.research.library import stats


@pytest.mark.parametrize("x", [-3.5, -1.96, -0.3, 0.0, 0.7, 1.645, 2.9])
def test_ppf_inverts_cdf(x: float) -> None:
    assert stats.norm_ppf(stats.norm_cdf(x)) == pytest.approx(x, abs=1e-7)


def test_known_quantiles() -> None:
    assert stats.norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-6)
    assert stats.norm_ppf(0.5) == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(ValueError):
        stats.norm_ppf(1.0)


def test_psr_at_the_benchmark_is_one_half() -> None:
    assert stats.psr(0.1, 0.1, 1000, 0.0, 3.0) == pytest.approx(0.5)
    assert stats.psr(0.1, 0.0, 1000, 0.0, 3.0) > 0.99
    # Negative skew and fat tails make the same Sharpe ratio less convincing.
    assert stats.psr(0.05, 0.0, 500, -1.0, 9.0) < stats.psr(0.05, 0.0, 500, 0.0, 3.0)


def test_expected_maximum_of_many_trials() -> None:
    # About 2.5 standard deviations for the best of 100 unit-variance trials.
    assert stats.expected_max_sharpe(100, 1.0) == pytest.approx(2.53, abs=0.03)
    assert stats.expected_max_sharpe(1, 1.0) == 0.0
    assert stats.expected_max_sharpe(1000, 1.0) > stats.expected_max_sharpe(100, 1.0)


def test_more_trials_deflate_more() -> None:
    rng = random.Random(3)
    returns = [rng.gauss(0.001, 0.02) for _ in range(1500)]
    assert (
        stats.dsr(returns, 200, 0.0004)
        < stats.dsr(returns, 10, 0.0004)
        < stats.dsr(returns, 2, 0.0004)
    )


def test_moments() -> None:
    m, sd, skew, kurt = stats.moments([1.0, 2.0, 3.0, 4.0, 5.0])
    assert m == 3.0 and sd == pytest.approx(1.5811388, rel=1e-6)
    assert skew == pytest.approx(0.0, abs=1e-12) and kurt == pytest.approx(1.7, rel=1e-6)
