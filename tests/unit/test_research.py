from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from cosmic.market.regime import classify
from cosmic.research.backtest import DISCLAIMER, CostModel, run_backtest, walk_forward
from cosmic.research.strategy import TrendSmaAtr, version_hash
from tests.research_data import entry_signal_bars, series, to_bars


def test_regime_method() -> None:
    assert classify([100.0] * 50).label == "INSUFFICIENT_DATA"
    up = [100 * 1.001**i for i in range(400)]
    assert classify(up).trend == "TREND_UP"
    down = [100 * 0.999**i for i in range(400)]
    assert classify(down).trend == "TREND_DOWN"
    r = classify(series())
    assert (
        r.volatility in ("LOW_VOL", "NORMAL_VOL", "HIGH_VOL") and 0 <= (r.vol_percentile or 0) <= 1
    )


def test_entry_signal_at_last_bar_only() -> None:
    bars = entry_signal_bars()
    strat = TrendSmaAtr()
    sig = strat.signal(bars)
    assert sig.action == "ENTER_LONG" and sig.stop is not None and sig.take_profit is not None
    assert sig.stop < float(bars[-1].close) < sig.take_profit
    assert strat.signal(bars[:-1]).action != "ENTER_LONG"


def test_version_hash_changes_with_params() -> None:
    a, b = TrendSmaAtr(), TrendSmaAtr(params={**TrendSmaAtr().params, "fast": 10})
    assert version_hash(a) != version_hash(b)
    assert version_hash(a) == version_hash(TrendSmaAtr())


def test_backtest_no_lookahead() -> None:
    bars = to_bars(series())
    base = run_backtest(bars, TrendSmaAtr())
    cut = len(bars) // 2
    perturbed = bars[:cut] + [
        replace(b, close=b.close * 2, high=b.high * 2, open=b.open * 2) for b in bars[cut:]
    ]
    other = run_backtest(perturbed, TrendSmaAtr())
    before = [t for t in base.trades if t.exit_time < bars[cut - 1].open_time.isoformat()]
    before2 = [t for t in other.trades if t.exit_time < bars[cut - 1].open_time.isoformat()]
    assert before == before2
    assert base.equity_curve[: cut - 1] == other.equity_curve[: cut - 1]


def test_costs_reduce_returns_and_are_disclosed() -> None:
    bars = to_bars(series())
    free = run_backtest(bars, TrendSmaAtr(), costs=CostModel(0, 0, 0))
    paid = run_backtest(bars, TrendSmaAtr())
    assert paid.trades and free.trades
    assert paid.total_return < free.total_return and paid.fees_paid > 0
    s = paid.summary()
    assert s["disclaimer"] == DISCLAIMER and s["assumptions"]["basis"] == "ASSUMPTION"
    assert any("statistically weak" in n for n in s["notes"])
    assert run_backtest(bars, TrendSmaAtr()).summary() == s  # reproducible


def test_stop_gap_fills_at_open() -> None:
    bars = entry_signal_bars()
    sig = TrendSmaAtr().signal(bars)
    assert sig.stop is not None
    gap_open = Decimal(f"{sig.stop * 0.97:.2f}")
    nxt = [
        replace(
            bars[-1],
            open_time=bars[-1].open_time.replace(hour=21),
            open=bars[-1].close,
            high=bars[-1].close,
            low=bars[-1].close,
            close=bars[-1].close,
        ),
        replace(
            bars[-1],
            open_time=bars[-1].open_time.replace(hour=22),
            open=gap_open,
            high=gap_open,
            low=gap_open * Decimal("0.99"),
            close=gap_open,
        ),
    ]
    res = run_backtest(bars + nxt, TrendSmaAtr(), costs=CostModel(0, 0, 0))
    stop_trades = [t for t in res.trades if t.exit_reason == "stop"]
    assert (
        stop_trades and abs(stop_trades[-1].exit_price - float(gap_open)) < 1e-6
    )  # worse than the stop


def test_walk_forward_discloses_trials_and_insufficient_data() -> None:
    bars = to_bars(series())
    grid = [{**TrendSmaAtr().params, "fast": f, "slow": s} for f, s in ((10, 30), (20, 50))]
    wf = walk_forward(bars, lambda g: TrendSmaAtr(params=g), grid, train=168, test=72)
    assert wf.windows and wf.trials == len(grid) * len(wf.windows)
    assert "multiple testing" in wf.notes[0]
    tiny = walk_forward(bars[:100], lambda g: TrendSmaAtr(params=g), grid, train=168, test=72)
    assert tiny.out_of_sample_return is None and "insufficient data" in tiny.notes[0]


def test_opportunity_paths() -> None:
    from cosmic.research.opportunities import path

    assert path("APPROVAL_REQUIRED", "EXECUTION") == ["APPROVED", "EXECUTION"]
    assert path("IDEA", "APPROVAL_REQUIRED") == [
        "RESEARCH",
        "CHALLENGED",
        "QUALIFIED",
        "APPROVAL_REQUIRED",
    ]
    assert path("REJECTED", "EXECUTION") == []
    assert path("IDEA", "EXECUTION") == [
        "RESEARCH",
        "CHALLENGED",
        "QUALIFIED",
        "APPROVAL_REQUIRED",
        "APPROVED",
        "EXECUTION",
    ]  # never a shortcut from idea to execution
