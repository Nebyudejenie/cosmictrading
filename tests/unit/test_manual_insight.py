"""Trader Insight's reading of the market (S114): structure, support and resistance, volatility,
and when a probability may be called verified. Candles are made up for each case."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from cosmic.manual.insight import (
    CALM,
    FORWARD_MIN,
    HIGH,
    LAB,
    NORMAL,
    Forward,
    Market,
    _context,
    _stand_aside,
    atr,
    choose_probability,
    hours_to_weekly_close,
    in_session,
    record_rows,
    structure_of,
    volatility_of,
    wilson,
    zones,
)
from cosmic.market.hfm_candles import Candle

START = datetime(2026, 11, 2, tzinfo=UTC)


def _candles(
    mids: list[float], *, width: str = "1", step: timedelta = timedelta(hours=1)
) -> list[Candle]:
    half = D(width) / 2
    out = []
    for i, m in enumerate(mids):
        mid = D(str(m))
        out.append(Candle(START + i * step, mid, mid + half, mid - half, mid, D(0), True))
    return out


RISING = [10, 11, 14, 11, 10.5, 12, 16, 12, 11.5, 13, 18, 13, 12.5]


def test_structure_reads_the_last_two_swings() -> None:
    assert structure_of(_candles(RISING)).startswith("rising: higher highs and higher lows")
    falling = [30 - m for m in RISING]
    assert structure_of(_candles(falling)).startswith("falling: lower highs and lower lows")
    mixed = [10, 11, 14, 11, 9, 12, 16, 12, 8, 13, 18, 13, 12.5]  # highs up, lows down
    assert structure_of(_candles(mixed)).startswith("mixed: no clear direction")
    assert structure_of(_candles([10, 11, 12, 13, 14])) is None  # no swings yet


def test_support_and_resistance_are_the_nearest_swings_around_the_price() -> None:
    supports, resistances = zones(_candles(RISING, step=timedelta(hours=4)), D(15))
    assert [v for _, v in resistances] == [D("16.5"), D("18.5")]  # above, nearest first
    assert [v for _, v in supports] == [D("11.0"), D("10.0")]  # below, nearest first
    assert all(name.startswith("Resistance") for name, _ in resistances)


def test_volatility_is_todays_daily_range_against_its_own_year() -> None:
    def days(last: str) -> list[Candle]:
        return _candles([100] * 200, width="10", step=timedelta(days=1)) + _candles(
            [100] * 14, width=last, step=timedelta(days=1)
        )

    assert volatility_of(days("10"))[0] == NORMAL
    kind, why = volatility_of(days("20"))
    assert kind == HIGH and "2.00 × its median of 10.00" in why
    assert volatility_of(days("5"))[0] == CALM
    assert volatility_of(days("10")[:50]) is None  # under 60 days: not judged
    assert atr(days("20")) == 20


def test_a_probability_is_verified_only_out_of_sample_from_enough_trades() -> None:
    lab = (D("0.347"), 75, "volatile markets")
    few = choose_probability(Forward(FORWARD_MIN - 1, 20, START), *lab)
    assert not few.verified and (few.p, few.n, few.period) == (D("0.347"), 75, LAB)
    assert (few.low, few.high) == wilson(D("0.347"), 75)
    enough = choose_probability(Forward(FORWARD_MIN, 12, START), *lab)
    assert enough.verified and (enough.p, enough.n) == (D("0.4"), FORWARD_MIN)
    assert enough.period == "the rule's forward test since 2026-11-02"
    assert (enough.low, enough.high) == wilson(D("0.4"), FORWARD_MIN)
    assert not choose_probability(Forward(0, 0, None), *lab).verified


# ---- S115: the setup against its market, the record, and the closed market -----------------


def test_the_rules_record_is_shown_by_side_volatility_and_year_with_its_range() -> None:
    rows = record_rows()
    buy_all = next(r for r in rows if r.side == "BUY" and r.label.startswith("all"))
    assert (buy_all.trades, buy_all.p_win, buy_all.expected_r) == (372, D("0.366"), D("0.177"))
    assert (buy_all.low, buy_all.high) == wilson(D("0.366"), 372)
    recent = next(r for r in rows if r.side == "BUY" and r.label.startswith("2026 only"))
    assert recent.expected_r < 0 and recent.trades == 102  # the edge has not shown in 2026
    assert {r.side for r in rows} == {"BUY", "SELL"} and len(rows) == 10


def test_candles_on_hfms_weekend_are_never_read() -> None:
    """The seven candles stored on 2026-10-25 had Friday's closing price stamped inside the
    closed market (server Saturday 00:00 and 01:00)."""
    bad = _candles([3995.49], step=timedelta(hours=1))[0]
    friday_close = datetime(2026, 10, 23, 21, tzinfo=UTC)  # server Saturday 00:00 (EDT + 7)
    monday_open = datetime(2026, 10, 25, 21, tzinfo=UTC)  # server Monday 00:00: HFM's own candle
    assert not in_session(replace(bad, open_time=friday_close))
    assert in_session(replace(bad, open_time=monday_open))
    assert in_session(replace(bad, open_time=datetime(2026, 11, 20, 20, tzinfo=UTC)))


def test_the_weekly_close_and_the_reasons_to_stand_aside() -> None:
    friday = datetime(2026, 11, 20, 20, 30, tzinfo=UTC)  # server 22:30, close at 23:59
    assert hours_to_weekly_close(friday) == D("1.5")
    assert hours_to_weekly_close(datetime(2026, 11, 19, 20, tzinfo=UTC)) is None  # Thursday
    market = Market(resistances=(("Resistance", D("3545.00")), ("Resistance", D("3560.00"))))
    why = _stand_aside("BUY", D("3538.59"), D("10"), market, friday, D("1.20"), D("0.34"))
    assert why[0].startswith("resistance at 3545.00 is nearer than the stop distance (10.00)")
    assert why[1].startswith("the spread is 1.20, 3.5 times its usual 0.34")
    assert why[2].startswith("HFM closes for the weekend in 1.5 hours")
    calm = _stand_aside("BUY", D("3538.59"), D("5"), market, START, D("0.35"), D("0.34"))
    assert calm == []  # 6.41 away is beyond a 5.00 stop; a usual spread; a Monday
    crit = {c.name: c for c in _context("BUY", D("3538.59"), D("3558.59"), D("10"), market, START)}
    assert not crit["Room to the target"].passed  # 3545.00 lies before 3558.59
    assert crit["Before the weekend"].passed
    falling = replace(market, structure="falling: lower highs and lower lows (…)")
    crit = {c.name: c for c in _context("BUY", D("3538.59"), D("3558.59"), D("10"), falling, START)}
    assert not crit["Structure"].passed and "against a BUY" in crit["Structure"].why
