"""The broker's overnight swap (ADR-032 addendum): MT5's report converted to a share of the
price per night, and the nights a trade is charged for in the replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cosmic.research.library import expect
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import Market
from cosmic.trading.venue_terms import funding_text, swap_per_night

GOLD = {
    "swap_long": "-78.56",
    "swap_short": "0.0",
    "swap_rollover3days": 3,
    "contract_size": "100.0",
}


def test_each_mt5_mode_becomes_a_share_of_the_price_a_night() -> None:
    points = swap_per_night({**GOLD, "swap_mode": 1, "point": "0.01"}, 4000.0)
    assert points is not None
    assert points.long == pytest.approx(-78.56 * 0.01 / 4000)  # −0.79 USD an ounce a night
    assert points.short == 0.0 and points.triple_day == "Wednesday" and points.triple_weekday == 2
    money = swap_per_night({**GOLD, "swap_mode": 4}, 4000.0)  # USD a lot (100 oz) a night
    assert money is not None and money.long == pytest.approx(-78.56 / 100 / 4000)
    yearly = swap_per_night({**GOLD, "swap_mode": 5, "swap_long": "-7.2"}, 4000.0)
    assert yearly is not None and yearly.long == pytest.approx(-0.072 / 360)
    assert swap_per_night({**GOLD, "swap_mode": 0}, 4000.0).long == 0.0  # type: ignore[union-attr]
    sunday = swap_per_night({**GOLD, "swap_mode": 1, "point": "0.01", "swap_rollover3days": 0}, 1)
    assert sunday is not None and sunday.triple_weekday == 6


def test_unknown_or_non_money_swap_is_not_guessed() -> None:
    assert swap_per_night(GOLD, 4000.0) is None  # an older bridge: no mode
    assert swap_per_night({**GOLD, "swap_mode": 7}, 4000.0) is None  # reopening, not money
    assert swap_per_night({**GOLD, "swap_mode": 1}, 4000.0) is None  # points without a point
    assert swap_per_night(None, 4000.0) is None
    assert "in points per lot" in funding_text("CFD", {**GOLD, "swap_mode": 1})


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 10, 19, tzinfo=UTC) + timedelta(days=day, hours=hour)  # a Monday


def test_the_nights_a_trade_is_charged_for() -> None:
    wed = 2
    assert expect.nights(at(0, 10), at(0, 20), wed) == 0  # closed before the rollover
    assert expect.nights(at(0, 20), at(1, 10), wed) == 1  # Monday night
    assert expect.nights(at(2, 20), at(3, 1), wed) == 3  # Wednesday: three nights
    assert expect.nights(at(4, 20), at(7, 22), wed) == 2  # Friday and Monday; weekend on Wed
    assert expect.nights(at(4, 20), at(7, 22), None) == 4  # crypto pays every night
    assert expect.nights(at(0, 21), at(1, 21), None) == 1  # opened at the rollover: next one


def test_the_replay_charges_swap_for_the_nights_held() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rows = (
        [(100.0, 101.0, 99.0, 100.0)] * 30
        + [(100.0, 103.5, 99.5, 103.0)]
        + [(103.0, 104.0, 102.5, 103.5)] * 60
    )
    m = Market(
        "X",
        tuple(t0 + timedelta(hours=i) for i in range(len(rows))),
        tuple(r[0] for r in rows),
        tuple(r[1] for r in rows),
        tuple(r[2] for r in rows),
        tuple(r[3] for r in rows),
        tuple(1.0 for _ in rows),
    )
    limits = expect.Limits(per_trade=63.0, daily_stop=1000.0, trades_per_day=5, open_positions=2)

    def run(swap: float) -> expect.Trade:
        v = expect.Venue("X", m, 1.0, 1.0, 0.0, 0.0, swap=swap)
        (t,) = expect.simulate([v], limits, BY_KEY["turtle_20_10"], start=t0, slippage=0.0).trades
        return t

    free, paid = run(0.0), run(-0.0002)
    held = expect.nights(paid.opened, paid.closed, None)
    assert held >= 2 and paid.swap == pytest.approx(paid.quantity * paid.entry * -0.0002 * held)
    assert paid.pnl == pytest.approx(free.pnl + paid.swap) and free.swap == 0.0
