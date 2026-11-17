"""The TEST rule (S98): BUY when the 4-hour and 1-hour trends are up and the 15-minute momentum
has just turned up; its 4-hour candles from the hours; its prices, its cost floor and its size.
That it decides exactly as the lab's study did was checked on 4 years of HFM's candles
(1,580 signals each, identical; PREREGISTRATION.md, 2026-11-17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from cosmic.gold import trend_buy as tb
from cosmic.market.hfm_candles import Candle

END = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)  # a Wednesday; a 15-minute candle closes now


def _candles(
    closes: list[float], minutes: int, last_open: datetime, half: float = 1.0
) -> list[Candle]:
    step = timedelta(minutes=minutes)
    return [
        Candle(
            last_open - step * (len(closes) - 1 - k),
            D(str(round(c, 2))),
            D(str(round(c + half, 2))),
            D(str(round(c - half, 2))),
            D(str(round(c, 2))),
            D(1),
            True,
        )
        for k, c in enumerate(closes)
    ]


def _hours(slope: float, n: int = 400) -> list[Candle]:
    """1-hour candles up to the one that closes at END, rising by ``slope`` an hour."""
    return _candles([4000 + slope * k for k in range(n)], 60, END - timedelta(hours=1), half=3.0)


def _turn_up() -> list[float]:
    """Fifteen-minute closes: quiet, a dip of seven candles, then a jump back: RSI(14) crosses up
    through 50 on the last candle, with its close above the 21-candle average."""
    return [4400.0] * 60 + [4400 - 1.5 * k for k in range(1, 8)] + [4400.0]


def test_a_buy_needs_both_trends_up_and_the_momentum_just_turned_up() -> None:
    m15 = _candles(_turn_up(), 15, END - timedelta(minutes=15))
    r = tb.read(m15, _hours(1.0), END)
    assert (r.decision, r.h4, r.h1) == ("BUY", "UP", "UP")
    assert r.rsi_before is not None and r.rsi is not None and r.rsi_before <= 50 < r.rsi
    assert r.bar == END - timedelta(minutes=15) and r.atr is not None and r.atr > 0
    # The hours falling: no BUY (this rule never sells), and it says why.
    down = tb.read(m15, _hours(-1.0), END)
    assert down.decision == "WAIT" and "BUY only when both are UP" in down.why
    assert (down.h4, down.h1) == ("DOWN", "DOWN")
    # A candle later the momentum is no longer turning: nothing new.
    later = _candles([*_turn_up(), 4400.0], 15, END)
    assert tb.read(later, _hours(1.0), END + timedelta(minutes=15)).decision == "WAIT"
    # Too few hours for a trend, or a market quiet for long: no decision.
    assert "too few" in tb.read(m15, _hours(1.0, n=100), END).why
    assert "market may be closed" in tb.read(m15, _hours(1.0), END + timedelta(hours=1)).why


def test_only_completed_candles_count() -> None:
    m15 = _candles(_turn_up(), 15, END - timedelta(minutes=15))
    forming = Candle(END, D(4300), D(4300), D(4300), D(4300), D(1), False)
    assert tb.read([*m15, forming], _hours(1.0), END).decision == "BUY"
    # An hour that has not closed by the 15-minute candle's close is not used for its trend.
    hours = _hours(1.0)
    late = Candle(END, D(3000), D(3000), D(3000), D(3000), D(1), True)
    assert tb.read(m15, [*hours, late], END).h1 == "UP"


def test_four_hour_candles_are_built_from_the_hours_from_midnight_utc() -> None:
    hours = _candles([float(k) for k in range(12)], 60, datetime(2026, 11, 18, 11, 0, tzinfo=UTC))
    fours = tb.four_hours(hours)
    assert [c.open_time.hour for c in fours] == [0, 4, 8]
    assert (fours[1].open, fours[1].close) == (D(4), D(7))
    assert fours[1].high == D(8) and fours[1].low == D(3)  # the hours' extremes (±1)
    # The last block without its fourth hour is still forming; a later block closes a short one.
    assert [c.open_time.hour for c in tb.four_hours(hours[:-1])] == [0, 4]
    gap = [*hours[:6], *hours[8:]]  # 06:00 and 07:00 missing (the market closed)
    assert [c.open_time.hour for c in tb.four_hours(gap)] == [0, 4, 8]


def test_the_trade_is_1_to_2_with_the_cost_floor() -> None:
    lv = tb.levels(5.0, D("4400.00"), D("4400.35"), D("0.01"), D("0.10"))
    assert isinstance(lv, tb.Levels)
    assert lv.distance == D("10.00")  # 2 × ATR 5.00
    assert (lv.limit, lv.stop, lv.take_profit) == (D("4400.45"), D("4390.35"), D("4420.35"))
    # The spread more than 5 % of the stop: no trade, and why.
    narrow = tb.levels(3.0, D("4400.00"), D("4400.35"), D("0.01"), D("0.10"))
    assert isinstance(narrow, str) and "more than 5 %" in narrow


def test_the_size_keeps_the_loss_within_the_amount_a_trade() -> None:
    # 2 USD a trade, a 10.00 stop, 0.10 slippage and no fee: 0.19 oz in 0.01-oz steps.
    assert tb.ounces(D(2), D("10.00"), D("0.10"), D(0), D("0.01")) == D("0.19")
    assert D("0.19") * (D("10.00") + D("0.10")) <= 2
    # A standard account's smallest trade (1 oz) does not fit: no trade.
    assert tb.ounces(D(2), D("10.00"), D("0.10"), D(0), D(1)) == 0
    assert tb.ounces(D(2), D(0), D(0), D(0), D("0.01")) == 0
