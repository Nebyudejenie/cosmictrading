"""The simulation engine's accounting, against hand-computed values."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cosmic.research.library.engine import (
    Costs,
    Market,
    align,
    simulate,
    simulate_panel,
    window_metrics,
)

FREE = Costs("free", 0.0, 0.0, 0.0)


def market(closes: list[float], opens: list[float] | None = None, key: str = "T") -> Market:
    opens = opens or closes
    d = tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(len(closes)))
    return Market(
        key,
        d,
        tuple(opens),
        tuple(max(o, c) for o, c in zip(opens, closes, strict=True)),
        tuple(min(o, c) for o, c in zip(opens, closes, strict=True)),
        tuple(closes),
        tuple(1.0 for _ in closes),
    )


def test_decision_at_close_fills_at_next_open() -> None:
    m = market([100, 110, 121, 133.1], opens=[100, 105, 115, 125])
    sim = simulate(m, [1.0, 1.0, 1.0, 1.0], FREE)
    # Decided at close 0, bought at open 1 (105): equity follows close / 105.
    assert sim.equity[0] == 1.0
    assert sim.equity[1] == pytest.approx(110 / 105)
    assert sim.equity[3] == pytest.approx(133.1 / 105)


def test_costs_charged_on_entry_and_exit() -> None:
    costs = Costs("c", 50.0, 0.0, 0.0)  # 0.5 % each way
    m = market([100.0] * 5)
    sim = simulate(m, [1.0, 1.0, 0.0, 0.0, 0.0], costs)
    # Buy at open 1 (purchase + fee fit the cash: invested 1/1.005), sell at open 3.
    assert sim.equity[-1] == pytest.approx((1 - 0.005) / (1 + 0.005), rel=1e-9)
    assert min(sim.equity) > 0.98
    assert len(sim.round_trips) == 1 and sim.round_trips[0][2] < 0
    assert sim.turnover == pytest.approx(2.0, rel=1e-3)


def test_partial_weights_and_rebalance_threshold() -> None:
    m = market([100.0, 100, 200, 200, 200], opens=[100.0, 100, 100, 200, 200])  # +100 % on day 2
    sim = simulate(m, [0.5, 0.5, 0.65, 0.65, 0.65], FREE, min_rebalance=0.05)
    # Half invested from open 1; the price doubles, so the holding drifts to 2/3. A 0.65 target
    # is within the threshold of that drifted weight: no trade. Equity = 0.5 + 0.5 × 2.
    assert sim.equity[-1] == pytest.approx(1.5)
    assert sim.weights_held[-1] == pytest.approx(2 / 3)
    rebalanced = simulate(m, [0.5, 0.5, 0.5, 0.5, 0.5], FREE, min_rebalance=0.05)
    assert rebalanced.weights_held[-1] == pytest.approx(0.5)  # 2/3 → 1/2 is traded


def test_flat_rule_has_flat_equity_and_no_trades() -> None:
    sim = simulate(market([100, 90, 80, 120]), [0.0] * 4, FREE)
    assert sim.equity == [1.0] * 4 and not sim.round_trips


def test_window_metrics() -> None:
    closes = [100 * 1.01**i for i in range(400)]
    sim = simulate(market(closes), [1.0] * 400, FREE)
    w = window_metrics(sim, 100, 400)
    assert w.days == 300 and w.max_drawdown == pytest.approx(0.0)
    assert w.total_return == pytest.approx(1.01**300 - 1, rel=1e-9)
    assert w.exposure == pytest.approx(1.0)


def test_panel_normalises_over_full_weights_and_aligns_dates() -> None:
    a = market([100.0] * 6, key="A")
    b = market([50.0, 50, 100, 100, 100, 100], key="B")
    p = align([a, b])
    sim = simulate_panel(p, {"A": [1.0] * 6, "B": [1.0] * 6}, FREE)
    # Asked for 200 %: scaled to 50/50. B doubles after the buy at open 1: equity 1.5.
    assert sim.equity[-1] == pytest.approx(1.5)
    shorter = market([1.0] * 4, key="C")
    assert len(align([a, shorter]).dates) == 4


def test_invalid_weights_are_clipped_not_levered() -> None:
    m = market([100.0, 100, 200])
    sim = simulate(m, [3.0, 3.0, 3.0], FREE)
    assert sim.equity[-1] == pytest.approx(2.0)  # 100 % at most: no leverage
    sim = simulate(m, [-1.0, -1.0, -1.0], FREE)
    assert sim.equity[-1] == pytest.approx(1.0)  # no shorting
