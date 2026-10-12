from __future__ import annotations

from cosmic.workforce.priority import narrative_priority

SIZED = {"sizing": {"quantity": "0.004"}}


def test_decision_value_ordering() -> None:
    good = narrative_priority({**SIZED, "backtest": {"trades": 25, "total_return": 0.04}})
    thin = narrative_priority({**SIZED, "backtest": {"trades": 3, "total_return": 0.10}})
    bad = narrative_priority({**SIZED, "backtest": {"trades": 25, "total_return": -0.02}})
    unsized = narrative_priority({"sizing": None, "backtest": {"trades": 25, "total_return": 1}})
    assert [good[0], thin[0], bad[0], unsized[0]] == [20, 40, 60, 90]
    assert "positive historical simulation over 25 trades" in good[1]
    assert narrative_priority({**SIZED, "backtest": None})[0] == 40  # no history to judge
