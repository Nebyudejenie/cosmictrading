"""Trader Insight's reading of the market (S114): structure, support and resistance, volatility,
and when a probability may be called verified. Candles are made up for each case."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from cosmic.manual.insight import (
    CALM,
    FORWARD_MIN,
    HIGH,
    LAB,
    NORMAL,
    Forward,
    atr,
    choose_probability,
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
