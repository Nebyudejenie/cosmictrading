"""Research for one market (research/library/insight.py, ADR-032): why a rule lost or won —
costs, how trades ended, the session, the daily trend, holding times — and the benchmark of
buying every month and holding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cosmic.research.library import expect, insight
from cosmic.research.library.engine import Market

T0 = datetime(2026, 3, 2, tzinfo=UTC)  # a Monday


def trade(opened_h: int, hours: int, pnl: float, how: str = "rule exit") -> expect.Trade:
    opened = T0 + timedelta(days=60, hours=opened_h)
    return expect.Trade("G", opened, opened + timedelta(hours=hours), 1, 100, 97, 100, 3, pnl, how)


def result(*trades: expect.Trade) -> expect.Result:
    r = expect.Result(T0, T0 + timedelta(days=90), 90 * 24)
    r.trades = list(trades)
    return r


def daily(closes: list[float]) -> Market:
    dates = tuple(T0 + timedelta(days=i) for i in range(len(closes)))
    return Market(
        "G",
        dates,
        tuple(closes),
        tuple(closes),
        tuple(closes),
        tuple(closes),
        tuple(1.0 for _ in closes),
    )


def test_sessions_follow_the_utc_hour() -> None:
    names = [insight.session_of(T0 + timedelta(hours=h)) for h in (0, 6, 7, 12, 13, 20, 21, 23)]
    assert names == ["Asia", "Asia", "London", "London", "New York", "New York", "Late", "Late"]


def test_the_diagnosis_splits_the_result_and_says_it_plainly() -> None:
    net = result(
        trade(3, 5, -100.0, "stop-loss"),  # Asia, stopped
        trade(9, 20, 250.0),  # London, a winner held 20 h
        trade(14, 4, -60.0, "stop-loss"),  # New York
        trade(15, 6, -60.0),  # New York
    )
    gross = result(
        trade(3, 5, -80.0), trade(9, 20, 270.0), trade(14, 4, -40.0), trade(15, 6, -20.0)
    )
    # a falling then rising market: the trend is up when the trades open (day 60)
    closes = [100.0 - i * 0.2 for i in range(30)] + [94.0 + i * 0.5 for i in range(40)]
    d = insight.diagnose(net, gross, per_trade=100.0, daily=daily(closes), hold_pct=12.5)
    assert d.trades == 4 and d.net_r == pytest.approx(0.3) and d.gross_r == pytest.approx(1.3)
    assert d.cost_r == pytest.approx(1.0)
    assert (d.stops.trades, d.stops.total_r, d.exits.trades) == (2, -1.6, 2)
    by = {b.name: b for b in d.sessions}
    assert by["New York"].trades == 2 and by["New York"].total_r == pytest.approx(-1.2)
    assert by["London"].wins == 1 and by["Late"].trades == 0
    assert [b.name for b in d.trend] == ["uptrend"] and d.trend[0].trades == 4
    assert d.hold_win_h == 20 and d.hold_loss_h == pytest.approx(5.0)
    text = " ".join(d.findings)
    assert "costs took 1.0 R (0.25 R a trade), leaving +0.3 R" in text
    assert "London session made +2.5 R" in text and "New York session -1.2 R" in text
    assert "Simply holding over the same period: +12.5 %" in text


def test_costs_that_turn_a_gain_into_a_loss_are_named() -> None:
    d = insight.diagnose(result(trade(9, 2, -10.0)), result(trade(9, 2, 15.0)), per_trade=100.0)
    assert "Costs alone turned a gain into a loss." in d.findings[0]
    assert insight.diagnose(result(), result(), per_trade=100.0).findings == [
        "The rule made no trade in this period."
    ]


def test_buying_every_month_and_holding() -> None:
    flat = daily([100.0] * 70)  # 2 Mar → 10 May: three months begin
    acc = insight.accumulate(flat, 500.0, T0, cost=0.001)
    assert acc is not None and acc.months == 3 and acc.invested == 1500.0
    assert acc.value == pytest.approx(1500 * 0.999) and acc.worst_pct == pytest.approx(-0.1)
    doubling = daily([100.0] * 35 + [200.0] * 35)
    up = insight.accumulate(doubling, 500.0, T0, cost=0.0)
    assert up is not None and up.gain_pct > 0 and up.curve[-1][1] == up.invested
    assert insight.accumulate(flat, 500.0, T0 + timedelta(days=400)) is None


def test_change_over_a_number_of_bars() -> None:
    assert insight.change_pct([100.0, 110.0], 1) == pytest.approx(10.0)
    assert insight.change_pct([100.0], 1) is None
