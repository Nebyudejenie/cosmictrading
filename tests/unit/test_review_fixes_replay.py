"""Regressions from the review of 2026-10-23 (replay, competition, research): each test pins
a confirmed finding so it cannot come back."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from cosmic.research.library import compete, expect, insight
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import Market

TURTLE = BY_KEY["turtle_20_10"]


def hourly(closes: list[float], t0: datetime) -> Market:
    opens = [closes[0], *closes[:-1]]
    return Market(
        "X",
        tuple(t0 + timedelta(hours=i) for i in range(len(closes))),
        tuple(opens),
        tuple(max(o, c) * 1.002 for o, c in zip(opens, closes, strict=True)),
        tuple(min(o, c) * 0.998 for o, c in zip(opens, closes, strict=True)),
        tuple(closes),
        tuple(1.0 for _ in closes),
    )


def waves(cycles: int, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(cycles):
        for _ in range(100):
            price *= math.exp(0.004 + rng.gauss(0, 0.002))
            out.append(price)
        for _ in range(30):
            price *= math.exp(-0.006 + rng.gauss(0, 0.002))
            out.append(price)
    return out


def test_the_backtest_never_sees_the_forward_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation ends at the forward date, and adding forward bars changes nothing in the
    backtest verdict (the split is fixed)."""
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    closes = waves(80)
    m = hourly(closes, t0)
    ff = m.dates[len(closes) - 2_600]
    monkeypatch.setattr(compete, "FORWARD_FROM", ff)
    short = hourly(closes[: len(closes) - 1_300], t0)  # the same series, fewer forward bars
    (a,) = compete.compete([compete.Series("X", "1h", m, 0.0, 0.0003)], [TURTLE]).entries
    (b,) = compete.compete([compete.Series("X", "1h", short, 0.0, 0.0003)], [TURTLE]).entries
    assert (a.dev, a.val, a.val_double, a.tier) == (b.dev, b.val, b.val_double, b.tier)
    assert a.forward.trades > b.forward.trades > 0  # only the forward record grew
    w = compete.window(m)
    assert w is not None and w.end < ff <= w.last  # type: ignore[operator]


def test_a_doubled_cost_never_doubles_a_swap_credit() -> None:
    m = hourly([100.0] * 400, datetime(2025, 1, 1, tzinfo=UTC))
    paid = compete._venue(compete.Series("X", "1h", m, 0.001, 0.0002, swap=-0.0001), 2.0)
    earned = compete._venue(compete.Series("X", "1h", m, 0.001, 0.0002, swap=0.0001), 2.0)
    assert paid.swap == pytest.approx(-0.0002) and earned.swap == pytest.approx(0.0001)


def test_a_venue_closed_at_weekends_trades_nothing_then_and_gaps_at_the_reopening() -> None:
    """Gold CFDs do not trade from Friday 21:00 to Sunday 22:00 UTC, although PAXG does: a
    stop crossed over the weekend fills at the reopening, never inside the close."""
    t0 = datetime(2026, 10, 15, 0, tzinfo=UTC)  # a Thursday
    rows = [(100.0, 101.0, 99.0, 100.0)] * 30  # to Friday 05:00
    rows += [(100.0, 103.5, 99.5, 103.0)]  # Friday 06:00: the breakout, bought at 07:00
    rows += [(103.0, 104.0, 102.5, 103.5)] * 14  # Friday 07:00 to 20:00
    rows += [(103.5, 103.6, 80.0, 81.0)] + [(81.0, 81.5, 80.5, 81.0)] * 48  # the closed hours
    rows += [(81.0, 82.0, 80.0, 81.5)] * 3  # Sunday 22:00: the reopening
    bars = Market(
        "X",
        tuple(t0 + timedelta(hours=i) for i in range(len(rows))),
        *(tuple(r[k] for r in rows) for k in range(4)),
        tuple(1.0 for _ in rows),
    )
    limits = expect.Limits(per_trade=63.0, daily_stop=1000.0, trades_per_day=5, open_positions=1)

    def run(closed: bool) -> list[expect.Trade]:
        v = expect.Venue("X", bars, 1.0, 1.0, 0.0, 0.0, weekend_closed=closed)
        return expect.simulate([v], limits, TURTLE, start=t0, slippage=0.0).trades

    (all_week,) = run(False)
    (gold,) = run(True)
    assert all_week.closed == t0 + timedelta(hours=45)  # PAXG stops out on Friday 21:00
    assert expect.weekend_closed_at(all_week.closed)
    assert gold.closed == t0 + timedelta(hours=94)  # Sunday 22:00, the first open bar
    assert gold.how == "stop-loss" and gold.exit == 81.0  # filled at the reopening gap
    assert not expect.weekend_closed_at(gold.opened) and not expect.weekend_closed_at(gold.closed)


def test_the_trend_at_entry_is_the_last_closed_day_even_without_the_days_own_bar() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    falling, rising = [100.0 - i for i in range(55)], [46.0 + 3 * i for i in range(10)]
    days = falling + rising  # day 64 is the last stored day; its close is far above the average
    daily = Market(
        "D",
        tuple(t0 + timedelta(days=i) for i in range(len(days))),
        *(tuple(days),) * 4,
        tuple(1.0 for _ in days),
    )
    opened = t0 + timedelta(days=65, hours=10)  # the current day: its daily bar is not stored
    trade = expect.Trade(
        "X", opened, opened + timedelta(hours=3), 1, 100, 97, 101, 3, 50.0, "rule exit"
    )
    r = expect.Result(t0, opened + timedelta(days=1), 24)
    r.trades = [trade]
    d = insight.diagnose(r, r, per_trade=100.0, daily=daily)
    assert [b.name for b in d.trend] == ["uptrend"]  # day 64 (up), not day 63 (down)


def test_the_weekend_close_and_the_rollover_follow_new_york_time() -> None:
    summer_friday = datetime(2026, 10, 16, tzinfo=UTC)  # New York on daylight time
    winter_friday = datetime(2026, 11, 6, tzinfo=UTC)  # New York on standard time
    hour = timedelta(hours=1)
    assert expect.weekend_closed_at(summer_friday + 21 * hour)  # 17:00 in New York
    assert not expect.weekend_closed_at(summer_friday + (2 * 24 + 22) * hour)  # Sun 18:00 NY
    assert not expect.weekend_closed_at(winter_friday + 21 * hour)  # 16:00 in New York
    assert expect.weekend_closed_at(winter_friday + 22 * hour)
    assert expect.weekend_closed_at(winter_friday + (2 * 24 + 22) * hour)  # Sun 17:00 NY
    assert not expect.weekend_closed_at(winter_friday + (2 * 24 + 23) * hour)
    monday_summer = datetime(2026, 10, 19, 21, 30, tzinfo=UTC)
    monday_winter = datetime(2026, 11, 9, 21, 30, tzinfo=UTC)
    assert expect.nights(monday_summer, monday_summer + 1.5 * hour, None) == 0  # 21:00 passed
    assert expect.nights(monday_winter, monday_winter + 1.5 * hour, None) == 1  # 22:00 UTC
