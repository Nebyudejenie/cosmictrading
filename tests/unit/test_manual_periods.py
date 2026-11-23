"""The owner's results by day, week and month, and how far below its best (S115): from HFM's
closed deals only, on HFM's own clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from cosmic.manual.book import Trade, by_day, by_month, by_week, current_drawdown

MONDAY = datetime(2026, 11, 2, 10, tzinfo=UTC)


def _t(pid: int, at: datetime, net: str) -> Trade:
    return Trade(pid, "BUY", D("0.01"), at, at, at, D(1), D(2), D(net), 0)


TRADES = [
    _t(1, MONDAY, "10"),
    _t(2, MONDAY + timedelta(hours=2), "-4"),
    _t(3, MONDAY + timedelta(days=1), "-8"),
    _t(4, MONDAY + timedelta(days=7), "5"),
    _t(5, MONDAY + timedelta(days=30), "-2"),
]


def test_days_weeks_and_months_add_up_and_show_their_deepest_fall() -> None:
    days = by_day(TRADES)
    assert [(p.trades, p.net) for p in days] == [(2, 6), (1, -8), (1, 5), (1, -2)]
    assert days[0].worst == 4 and days[0].win_rate == 50  # +10 then -4
    weeks = by_week(TRADES)
    assert [(p.label, p.net, p.worst) for p in weeks][:2] == [
        ("week of 02 Nov", D(-2), D(12)),  # +10, -4, -8: 12 below its best
        ("week of 09 Nov", D(5), D(0)),
    ]
    months = by_month(TRADES)
    assert [(p.label, p.trades, p.net) for p in months] == [
        ("November 2026", 4, D(3)),
        ("December 2026", 1, D(-2)),
    ]
    assert sum((p.net for p in days), D(0)) == sum((t.net for t in TRADES), D(0))


def test_how_far_below_its_best_the_result_is_now() -> None:
    assert current_drawdown(TRADES) == 9  # best +10, now +1
    assert current_drawdown(TRADES[:1]) == 0
    assert current_drawdown([]) == 0
