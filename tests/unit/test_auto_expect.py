"""What to expect (research/library/expect.py): the auto-trader's rule replayed on past hourly
bars. Fills at the next open, stops and gaps, sizing as live, the mandate's limits, no
look-ahead, and the summary the owner reads."""

from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from cosmic.research.library import auto, expect
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import Market

T0 = datetime(2026, 1, 1, tzinfo=UTC)
TURTLE = BY_KEY["turtle_20_10"]
Row = tuple[float, float, float, float]  # open, high, low, close
FLAT: list[Row] = [(100.0, 101.0, 99.0, 100.0)] * 30  # true range 2: ATR(20) = 2
# The close of bar 30 breaks the 20-hour high (101); its true range 4 makes ATR(20) 2.1, so
# a buy at the next open (103) has its stop 3 × 2.1 below the limit (103 × 1.001).
BREAKOUT: list[Row] = [*FLAT, (100.0, 103.5, 99.5, 103.0), (103.0, 104.0, 102.5, 103.5)]
STOP = 103 * 1.001 - 6.3
LIMITS = expect.Limits(per_trade=63.0, daily_stop=1000.0, trades_per_day=5, open_positions=2)


def hours(i: int) -> datetime:
    return T0 + timedelta(hours=i)


def market(rows: list[Row], key: str = "SYN") -> Market:
    return Market(
        key,
        tuple(hours(i) for i in range(len(rows))),
        tuple(r[0] for r in rows),
        tuple(r[1] for r in rows),
        tuple(r[2] for r in rows),
        tuple(r[3] for r in rows),
        tuple(1.0 for _ in rows),
    )


def venue(rows: list[Row], key: str = "X", **kw: float) -> expect.Venue:
    terms = {"lot": 1.0, "min_qty": 1.0, "fee": 0.0, "half_spread": 0.0, **kw}
    return expect.Venue(key, market(rows, key), **terms)  # type: ignore[arg-type]


def run(*venues: expect.Venue, limits: expect.Limits = LIMITS) -> expect.Result:
    return expect.simulate(venues, limits, TURTLE, start=T0, slippage=0.0)


# ---- the rule, its fills and its stop -----------------------------------------------------------


def test_it_buys_at_the_next_open_and_sells_after_a_close_below_the_ten_hour_low() -> None:
    rising = [
        (103.5 + k * 0.2, 104.5 + k * 0.2, 103.0 + k * 0.2, 104.0 + k * 0.2) for k in range(9)
    ]
    # Bar 41 closes at 101, below the lowest low of the ten bars before it (102.5): sell at 42.
    rows = [*BREAKOUT, *rising, (105.6, 105.6, 100.5, 101.0), (101.2, 101.5, 100.8, 101.0)]
    (t,) = run(venue(rows)).trades
    assert (t.opened, t.closed, t.how) == (hours(31), hours(42), "rule exit")
    assert (t.quantity, t.entry, t.exit) == (10.0, 103.0, 101.2)  # 63 / 6.3 = 10 whole lots
    assert t.stop == pytest.approx(STOP) and t.risk == pytest.approx(63.0)
    assert t.pnl == pytest.approx(10 * (101.2 - 103.0))


def test_the_stop_fills_at_the_stop_and_a_gap_through_it_at_the_open() -> None:
    (stopped,) = run(venue([*BREAKOUT, (102.0, 102.5, 96.0, 97.0)])).trades
    assert (stopped.how, stopped.closed) == ("stop-loss", hours(32))
    assert stopped.exit == pytest.approx(STOP) and stopped.pnl == pytest.approx(-61.97)
    (gapped,) = run(venue([*BREAKOUT, (95.0, 95.5, 94.0, 95.0)])).trades
    assert gapped.exit == 95.0 and gapped.pnl == pytest.approx(-80.0)
    assert gapped.pnl < -LIMITS.per_trade  # a gap loses more than the limit: shown, not hidden


def test_costs_are_paid_on_both_sides() -> None:
    rows = [*BREAKOUT, (102.0, 102.5, 96.0, 97.0)]
    (t,) = run(venue(rows, fee=0.001, half_spread=0.0005)).trades
    entry = 103 * 1.0005
    assert t.entry == pytest.approx(entry)
    assert t.pnl == pytest.approx(
        t.quantity * (t.exit - entry) - t.quantity * (entry + t.exit) * 0.001
    )
    assert t.risk <= LIMITS.per_trade


def test_after_a_stop_it_waits_for_a_fresh_breakout() -> None:
    rows = [
        *BREAKOUT,
        (102.0, 103.0, 96.5, 102.8),  # 32: the stop is hit, the close stays above the 10-hour low
        (102.8, 106.0, 102.5, 105.5),  # 33: a higher close, but the rule is still long: no buy
        (105.0, 105.0, 95.0, 95.5),  # 34: a close below the 10-hour low: the rule turns flat
        (95.5, 96.0, 95.0, 95.8),
        (96.0, 107.0, 95.9, 106.5),  # 36: a fresh breakout: buy at 37
        (106.5, 107.0, 106.0, 106.8),
    ]
    first, second = run(venue(rows)).trades
    assert (first.how, first.closed) == ("stop-loss", hours(32))
    assert (second.opened, second.how) == (hours(37), "still open")


# ---- sizing and the mandate's limits -----------------------------------------------------------


def test_a_trade_too_big_for_the_limit_is_skipped_never_resized() -> None:
    r = run(venue(BREAKOUT), limits=expect.Limits(5.0, 1000.0, 5, 2))
    assert r.trades == [] and r.skipped == [expect.Skip("X", hours(31), "TOO_BIG")]


def test_the_money_caps_the_size_and_what_is_left_for_another_market() -> None:
    money = expect.Limits(63.0, 1000.0, 5, 2, capital=500.0)
    r = run(venue(BREAKOUT, "A"), venue(BREAKOUT, "B"), limits=money)
    (a,) = r.trades
    assert (a.key, a.quantity) == ("A", 4.0)  # 4 × 103.10 fits in 500; 10 lots would not
    assert r.skipped == [expect.Skip("B", hours(31), "NO_MONEY")]


def test_trades_a_day_open_trades_and_the_daily_stop() -> None:
    both = (venue(BREAKOUT, "A"), venue(BREAKOUT, "B"))
    one_a_day = run(*both, limits=expect.Limits(63.0, 1000.0, 1, 2))
    assert [s.reason for s in one_a_day.skipped] == ["TRADES_TODAY"]
    one_open = run(*both, limits=expect.Limits(63.0, 1000.0, 5, 1))
    assert [s.reason for s in one_open.skipped] == ["OPEN_LIMIT"]
    # A loses 61.97 at its stop during bar 32; B breaks out at that bar's close the same day.
    a = venue([*BREAKOUT, (102.0, 102.5, 96.0, 97.0), (97.0, 97.5, 96.5, 97.0)], "A")
    quiet = (100.0, 100.5, 99.5, 100.0)
    b = venue([*FLAT, quiet, quiet, *BREAKOUT[30:]], "B")  # breaks out at 32, buys at 33
    stopped = run(a, b, limits=expect.Limits(63.0, 50.0, 5, 2))
    assert [t.key for t in stopped.trades] == ["A"]
    assert stopped.skipped == [expect.Skip("B", hours(33), "DAILY_STOP")]


@pytest.mark.parametrize("seed", range(40))
def test_sizing_matches_the_live_auto_trader(seed: int) -> None:
    rng = random.Random(seed)
    entry = rng.uniform(1_000, 100_000)
    stop_limit = entry - rng.uniform(0.002, 0.05) * entry
    fee_bps = rng.choice([0, 6, 10, 60])
    lot = rng.choice([0.001, 0.01, 1.0])
    per_trade = rng.uniform(5, 500)
    room = rng.uniform(500, 50_000)
    max_qty = rng.choice([None, rng.uniform(0.0, 5.0)])  # the risk policy's R19 cap (S36)
    qty, _ = expect.size(
        per_trade=per_trade,
        entry=entry,
        stop_limit=stop_limit,
        fee=fee_bps / 10_000,
        lot=lot,
        min_qty=lot,
        room=room,
        max_qty=max_qty if max_qty is not None else math.inf,
    )
    live, _ = auto.size_for_max_loss(
        max_loss=Decimal(str(per_trade)),
        entry=Decimal(str(entry)),
        stop_limit=Decimal(str(stop_limit)),
        fee_bps=Decimal(fee_bps),
        lot_size=Decimal(str(lot)),
        min_qty=Decimal(str(lot)),
        max_order_notional=Decimal(str(room)),
        max_quantity=Decimal(str(max_qty)) if max_qty is not None else None,
    )
    assert qty == pytest.approx(float(live))


# ---- no look-ahead -----------------------------------------------------------------------------


def walk(key: str, n: int, seed: int) -> list[Row]:
    rng = random.Random(seed)
    rows: list[Row] = []
    price = 100.0
    for _ in range(n):
        close = price * math.exp(rng.gauss(0, 0.006))
        high = max(price, close) * math.exp(abs(rng.gauss(0, 0.003)))
        low = min(price, close) * math.exp(-abs(rng.gauss(0, 0.003)))
        rows.append((price, high, low, close))
        price = close * math.exp(rng.gauss(0, 0.001))  # small gaps between hours
    return rows


def test_nothing_decided_before_an_hour_depends_on_later_hours() -> None:
    a, b = walk("A", 1500, 1), walk("B", 1500, 2)
    limits = expect.Limits(50.0, 120.0, 3, 2, capital=2_000.0)
    full = run(
        venue(a, "A", lot=0.1, min_qty=0.1), venue(b, "B", lot=0.1, min_qty=0.1), limits=limits
    )
    assert len(full.trades) > 20
    for cut in (400, 777, 1200):
        part = run(
            venue(a[:cut], "A", lot=0.1, min_qty=0.1),
            venue(b[:cut], "B", lot=0.1, min_qty=0.1),
            limits=limits,
        )
        last = hours(cut - 1)
        assert [t for t in part.trades if t.how != "still open"] == [
            t for t in full.trades if t.closed <= last
        ]
        assert part.skipped == [s for s in full.skipped if s.at <= last]


# ---- the summary ---------------------------------------------------------------------------------


def trade(day: int, pnl: float, how: str = "rule exit", key: str = "A") -> expect.Trade:
    when = T0 + timedelta(days=day)
    return expect.Trade(key, when, when, 1.0, 100.0, 90.0, 100.0 + pnl, 10.0, pnl, how)


def test_the_summary_says_what_happened_in_plain_numbers() -> None:
    r = expect.Result(T0, T0 + timedelta(days=90), 90 * 24)
    r.trades = [
        trade(5, 30.0),
        trade(10, -10.0, "stop-loss"),
        trade(12, -12.0, "stop-loss", "B"),
        trade(20, -8.0),
        trade(40, 2.0),
        trade(70, -12.0, "stop-loss"),
        trade(71, -10.0),
        trade(72, -10.0, "stop-loss", "B"),
        trade(80, 50.0, "still open"),
    ]
    r.skipped = [expect.Skip("A", T0, "TOO_BIG"), expect.Skip("B", T0, "TOO_BIG")]
    r.hours_in_market = 540
    s = expect.summarize(r)
    assert (s.trades, s.wins, s.losses, s.win_rate) == (9, 3, 6, 33)
    assert s.total == pytest.approx(20.0) and s.per_trade == pytest.approx(20 / 9)
    assert (s.avg_win, s.best, s.worst) == (pytest.approx(82 / 3), 50.0, -12.0)
    assert s.avg_loss == pytest.approx(-62 / 6)
    assert (s.losing_streak, s.streak_loss) == (3, -32.0)  # two runs of three: the costlier
    assert s.max_drawdown == pytest.approx(-60.0)  # from +30 down to −30
    assert (s.first_half, s.second_half) == (2.0, 18.0)
    assert s.stopped == 4 and s.in_market == pytest.approx(0.25)
    assert s.per_week == pytest.approx(9 / 90 * 7)
    assert s.months == [
        (date(2026, 1, 1), 0.0),  # +30 −10 −12 −8
        (date(2026, 2, 1), 2.0),
        (date(2026, 3, 1), 18.0),
        (date(2026, 4, 1), 0.0),  # a month without trades is shown, at zero
    ]
    assert [(m.key, m.trades, m.wins) for m in s.markets] == [("A", 7, 3), ("B", 2, 0)]
    assert s.skipped == {"TOO_BIG": 2}
    assert [v for _, v in s.curve][-1] == pytest.approx(20.0)


def test_an_empty_test_has_no_rate_and_no_average() -> None:
    s = expect.summarize(expect.Result(T0, T0 + timedelta(days=40), 960))
    assert (s.trades, s.win_rate, s.per_trade, s.avg_win) == (0, None, None, None)
    assert (s.max_drawdown, s.losing_streak, len(s.months)) == (0.0, 0, 2)


# ---- the total loss stop (ADR-030 addendum 3) ------------------------------------------------


def test_the_total_loss_stop_ends_the_replay_where_it_would_switch_itself_off() -> None:
    rows = [
        *BREAKOUT,
        (102.0, 103.0, 96.5, 102.8),  # 32: the stop loses 61.97
        (102.8, 106.0, 102.5, 105.5),
        (105.0, 105.0, 95.0, 95.5),
        (95.5, 96.0, 95.0, 95.8),
        (96.0, 107.0, 95.9, 106.5),  # 36: a fresh breakout, bought at 37 without the stop
        (106.5, 107.0, 106.0, 106.8),
    ]
    free = run(venue(rows))
    assert len(free.trades) == 2 and free.stopped_at is None
    capped = run(venue(rows), limits=expect.Limits(63.0, 1000.0, 5, 2, total_stop=100.0))
    (only,) = capped.trades  # 61.97 lost: no room for another 63 within 100
    assert only.how == "stop-loss" and capped.stopped_at == hours(32)
    assert expect.summarize(capped).stopped_at == hours(32)


def test_an_entry_whose_worst_case_could_pass_the_total_limit_is_skipped() -> None:
    both = (venue(BREAKOUT, "A"), venue(BREAKOUT, "B"))
    r = run(*both, limits=expect.Limits(63.0, 1000.0, 5, 2, total_stop=120.0))
    assert [t.key for t in r.trades] == ["A"]  # A alone could lose 63; with B, 126 > 120
    assert r.skipped == [expect.Skip("B", hours(31), "TOTAL_STOP")] and r.stopped_at is None


# ---- size to the evidence (ADR-030 addendum 4) -----------------------------------------------


def test_after_a_deep_drop_the_replay_trades_at_half_size() -> None:
    rows = [
        *BREAKOUT,
        (102.0, 103.0, 96.5, 102.8),  # 32: the stop loses 61.97 (almost one full trade)
        (102.8, 106.0, 102.5, 105.5),
        (105.0, 105.0, 95.0, 95.5),
        (95.5, 96.0, 95.0, 95.8),
        (96.0, 107.0, 95.9, 106.5),  # 36: a fresh breakout, bought at 37
        (106.5, 107.0, 106.0, 106.8),
    ]
    full = run(venue(rows), limits=expect.Limits(63.0, 1000.0, 5, 2))
    halved = run(venue(rows), limits=expect.Limits(63.0, 1000.0, 5, 2, halve_after=0.5))
    assert full.trades[0] == halved.trades[0]  # the first trade is sized the same
    assert halved.trades[1].risk <= 31.5 + 1e-9 < full.trades[1].risk
    assert halved.trades[1].quantity < full.trades[1].quantity


# ---- the account's risk policy (S36) -----------------------------------------------------------


def _breakouts(n: int = 400) -> Market:
    """A flat market that breaks out and falls back again and again: many losing trades."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    closes, highs, lows = [], [], []
    for i in range(n):
        # a close above the 20-bar high, then two bars later a fall through the stop and below
        # the 10-bar low (the rule turns flat), then back to the range
        c = {25: 108.0, 27: 95.0}.get(i % 40, 100.0)
        closes.append(c)
        highs.append(c + 0.5)
        lows.append(c - 0.5 if i % 40 != 27 else 90.0)
    return Market(
        "X",
        tuple(t0 + timedelta(hours=i) for i in range(n)),
        tuple(closes),
        tuple(highs),
        tuple(lows),
        tuple(closes),
        tuple(1.0 for _ in closes),
    )


def test_the_policys_loss_streak_stops_new_trades_in_the_replay_as_live() -> None:
    bars = _breakouts()
    venue = expect.Venue("X", bars, 0.001, 0.001, 0.0, 0.0)
    free = expect.Limits(per_trade=1.0, daily_stop=1e9, trades_per_day=99, open_positions=1)
    capped = replace(free, loss_streak=4)
    spec = BY_KEY["turtle_20_10"]
    start = bars.dates[30]
    assert isinstance(start, datetime)
    a = expect.simulate([venue], free, spec, start=start, slippage=0.0)
    b = expect.simulate([venue], capped, spec, start=start, slippage=0.0)
    assert len(a.trades) > 4 and all(t.pnl < 0 for t in a.trades)
    assert len(b.trades) == 4  # four losses in a row, then R08 refuses every new trade
    assert {s.reason for s in b.skipped} == {"RISK_LOSS_LIMITS"}


def test_the_policys_share_of_equity_caps_the_size_in_the_replay() -> None:
    bars = _breakouts()
    venue = expect.Venue("X", bars, 0.001, 0.001, 0.0, 0.0)
    limits = expect.Limits(
        per_trade=50.0,
        daily_stop=1e9,
        trades_per_day=99,
        open_positions=1,
        equity=100.0,
        concentration=0.5,
    )
    start = bars.dates[30]
    assert isinstance(start, datetime)
    r = expect.simulate([venue], limits, BY_KEY["turtle_20_10"], start=start, slippage=0.0)
    assert r.trades and all(t.quantity * t.entry <= 50.0 + 1e-6 for t in r.trades)


def test_at_a_fill_open_trades_are_marked_at_the_open_not_the_close_to_come() -> None:
    """No look-ahead: an entry at a bar's open never sees that bar's close (review fix)."""
    bars = _breakouts(60)
    venue = expect.Venue("X", bars, 0.001, 0.001, 0.0, 0.0)
    limits = expect.Limits(per_trade=1.0, daily_stop=1e9, trades_per_day=9, open_positions=2)
    start = bars.dates[30]
    assert isinstance(start, datetime)
    r = expect._Replay([venue], replace(limits, equity=100.0), BY_KEY["turtle_20_10"], start, 0.0)
    r.held["X"] = expect._Open(1.0, 100.0, 90.0, 10.0, start, 89.0)
    r.last["X"], r.step = 27, {"X"}  # bar 27 opens at 95 and closes at 95
    assert r._equity(at_open=True) == pytest.approx(100.0 + (bars.open[27] - 100.0))
    r.last["X"], r.step = 26, set()  # a market with no bar now: its last close
    assert r._equity(at_open=True) == pytest.approx(100.0 + (bars.close[26] - 100.0))


def test_without_equity_or_money_the_equity_limits_are_not_applied() -> None:
    """An auto-trader granted without money, on an account whose equity is unknown: the share
    of equity and the drawdown cannot be measured, so they do not stop the replay (review fix)."""
    bars = _breakouts()
    venue = expect.Venue("X", bars, 0.001, 0.001, 0.0, 0.0)
    free = expect.Limits(per_trade=1.0, daily_stop=1e9, trades_per_day=99, open_positions=1)
    blind = replace(free, concentration=0.5, open_risk=0.03, max_drawdown=0.1)
    start = bars.dates[30]
    assert isinstance(start, datetime)
    a = expect.simulate([venue], free, BY_KEY["turtle_20_10"], start=start, slippage=0.0)
    b = expect.simulate([venue], blind, BY_KEY["turtle_20_10"], start=start, slippage=0.0)
    assert len(a.trades) == len(b.trades) and not b.skipped
