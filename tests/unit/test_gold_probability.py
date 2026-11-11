"""The 1-minute rule's probability model (S85): the owner's decision bands, the one trade shape
and its arithmetic, when there is no estimate, and that the live calculation is the one the
study measured (a streaming reference over a long series against the windowed live code)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from cosmic.gold import probability as pr
from cosmic.market.hfm_candles import Candle

T0 = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)  # a Wednesday, 10:00 in New York


def _candles(closes: list[float], half: float, minutes: int, last_open: datetime) -> list[Candle]:
    step = timedelta(minutes=minutes)
    return [
        Candle(
            last_open - step * (len(closes) - 1 - k),
            D(str(round(c, 4))),
            D(str(round(c + half, 4))),
            D(str(round(c - half, 4))),
            D(str(round(c, 4))),
            D(1),
            True,
        )
        for k, c in enumerate(closes)
    ]


def _turn(sign: int = 1) -> list[float]:
    """Sixty quiet candles, a rise, eight falling candles and a jump back: the 1-minute RSI
    crosses 50 with the close beyond its EMA21 (mirrored for ``sign`` -1)."""
    flat = [4030 + 0.05 * i for i in range(40)]
    up = [flat[-1] + 0.2 * (i + 1) for i in range(30)]
    down = [up[-1] - 0.9 * (i + 1) for i in range(8)]
    series = [*flat, *up, *down, down[-1] + 6.0]
    return series if sign == 1 else [2 * series[0] - x for x in series]


def _market(sign: int = 1, at: datetime = T0) -> tuple[list[Candle], list[Candle], list[Candle]]:
    bar = at - timedelta(minutes=1)
    m1 = _candles(_turn(sign), 0.3, 1, bar)
    m5 = _candles([4000 + sign * 0.4 * i for i in range(120)], 0.5, 5, at - timedelta(minutes=5))
    m15 = _candles([3990 + sign * 1.0 * i for i in range(120)], 1.0, 15, at - timedelta(minutes=15))
    return m1, m5, m15


def test_the_owners_bands_and_the_models_one_trade_shape() -> None:
    m = pr.MODEL
    assert (m.version, m.risk, m.target, m.break_even) == (
        "m1-prob-1",
        D("2.00"),
        D("4.00"),
        D("0.3333"),
    )
    assert [pr.band(D(p)) for p in ("0.50", "0.4999", "0.30", "0.2999")] == [
        pr.AUTO,
        pr.ASK,
        pr.ASK,
        pr.REJECT,
    ]
    assert pr.band(None) == pr.NO_ESTIMATE  # no estimate is never a trade
    tick = D("0.01")
    buy = pr.levels(m, pr.BUY, D("4100.00"), D("4100.30"), tick)
    assert isinstance(buy, pr.Levels)
    # entered within 0.10 of the ask or not at all; 2.00 from that limit to the stop; 4.00 up
    assert (buy.limit, buy.stop, buy.take_profit) == (D("4100.40"), D("4098.40"), D("4104.30"))
    assert (buy.band_low, buy.band_high) == (D("4099.80"), D("4100.40"))
    sell = pr.levels(m, pr.SELL, D("4100.00"), D("4100.30"), tick)
    assert isinstance(sell, pr.Levels)
    assert (sell.limit, sell.stop, sell.take_profit) == (D("4099.90"), D("4101.90"), D("4096.00"))
    assert (sell.band_low, sell.band_high) == (D("4099.90"), D("4100.50"))
    wide = pr.levels(m, pr.BUY, D("4100.00"), D("4100.60"), tick)
    assert isinstance(wide, str) and "wider than the 0.50" in wide
    eco = pr.economics(m, D(1), D(0), D("0.27"))
    # 0.27 × 4.00 − 0.73 × 2.00: such a trade loses on average, and the screen says so
    assert (eco.max_loss, eco.net_at_target, eco.expected, eco.reward_to_risk) == (
        D("2.00"),
        D("4.00"),
        D("-0.38"),
        D("2.00"),
    )
    assert pr.economics(m, D(2), D(0), D("0.50")).expected == D("2.00")


def test_a_setup_in_the_trend_gets_the_measured_probability_and_its_mirror() -> None:
    for sign, side in ((1, pr.BUY), (-1, pr.SELL)):
        m1, m5, m15 = _market(sign)
        p = pr.predict(pr.MODEL, T0, m1, m5, m15)
        assert (p.setup, p.why, p.bar) == (side, None, T0 - timedelta(minutes=1))
        assert p.features["m15"] == p.features["m5"] == ("UP" if sign == 1 else "DOWN")
        best = p.of(side)
        # the model's whole range: what HFM's four years measured, never a confident number
        assert best is not None and D("0.2614") <= best <= D("0.2704")
        assert pr.band(best) == pr.REJECT and p.best is not None and p.best[1] >= best
    # The same turn against a falling 15-minute trend is no setup, though it has an estimate.
    m1, m5, _ = _market(1)
    _, _, down = _market(-1)
    against = pr.predict(pr.MODEL, T0, m1, m5, down)
    assert against.setup == pr.WAIT and against.p_buy is not None


def test_no_reliable_estimate_is_said_and_is_no_trade() -> None:
    m1, m5, m15 = _market()
    cases = [
        (pr.predict(pr.MODEL, T0, [], m5, m15), "no 1-minute candles yet"),
        (pr.predict(pr.MODEL, T0, m1[-30:], m5, m15), "30 of 60 1-minute candles"),
        (pr.predict(pr.MODEL, T0, m1, m5[-20:], m15), "too few complete 5- or 15-minute"),
        (pr.predict(pr.MODEL, T0 + timedelta(minutes=9), m1, m5, m15), "no new 1-minute candle"),
        (pr.predict(pr.MODEL, T0, m1, m5, m15, "gold closes soon"), "gold closes soon"),
    ]
    for p, why in cases:
        assert p.p_buy is None and p.p_sell is None and p.setup == pr.WAIT, why
        assert why in (p.why or ""), (p.why, why)
        assert pr.band(p.of(pr.BUY)) == pr.NO_ESTIMATE


def test_a_candle_that_ends_with_this_minute_is_not_counted_as_the_study_did_not() -> None:
    bar = datetime(2026, 10, 14, 13, 59, tzinfo=UTC)  # the last minute of its 5- and 15-minute
    five = _candles([4000.0 + i for i in range(60)], 0.5, 5, bar.replace(minute=55))
    kept = pr._before(five, bar, 5)
    assert kept[-1].open_time == bar.replace(minute=50) and len(kept) == 59
    first = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)  # the first minute of the next one
    assert pr._before(five, first, 5)[-1].open_time == bar.replace(minute=55)


def _reference(rows: list[tuple[int, float, float, float, float]], step: int) -> list[int]:
    """The study's streaming trend state per 1-minute candle (lab prob6.py)."""
    out, state, n = [], 1, 0
    e20 = e50 = atr = prev = None
    bucket = bh = bl = bc = None
    for t, _o, h, low, c in rows:
        b = t // step
        if b != bucket:
            if bucket is not None:
                assert bh is not None and bl is not None and bc is not None
                tr = bh - bl if prev is None else max(bh - bl, abs(bh - prev), abs(bl - prev))
                atr = tr if atr is None else (atr * 13 + tr) / 14
                e20 = bc if e20 is None else e20 + 2 / 21 * (bc - e20)
                e50 = bc if e50 is None else e50 + 2 / 51 * (bc - e50)
                prev, n = bc, n + 1
                if n >= 50 and atr > 0:
                    z = (e20 - e50) / atr
                    state = 0 if z > 0.25 else 2 if z < -0.25 else 1
            bucket, bh, bl = b, h, low
        else:
            assert bh is not None and bl is not None
            bh, bl = max(bh, h), min(bl, low)
        bc = c
        out.append(state)
    return out


def test_the_live_calculation_is_the_one_the_study_measured() -> None:
    rng = random.Random(85)
    start = int(T0.timestamp()) // 60
    rows, price = [], 4000.0
    for i in range(6000):  # a random walk with slow swings, in whole minutes
        drift = 0.08 if (i // 700) % 2 == 0 else -0.08
        o = price
        c = o + drift + rng.gauss(0, 0.6)
        rows.append((start + i, o, max(o, c) + abs(rng.gauss(0, 0.2)), min(o, c) - 0.1, c))
        price = c

    def candle(t: int, o: float, h: float, low: float, c: float) -> Candle:
        return Candle(
            datetime.fromtimestamp(t * 60, UTC),
            D(str(round(o, 4))),
            D(str(round(h, 4))),
            D(str(round(low, 4))),
            D(str(round(c, 4))),
            D(1),
            True,
        )

    def aggregate(step: int) -> list[Candle]:
        out: list[Candle] = []
        cur: list[tuple[int, float, float, float, float]] = []
        for r in rows:
            if cur and r[0] // step != cur[0][0] // step:
                out.append(
                    candle(
                        cur[0][0] // step * step,
                        cur[0][1],
                        max(x[2] for x in cur),
                        min(x[3] for x in cur),
                        cur[-1][4],
                    )
                )
                cur = []
            cur.append(r)
        return out

    m1 = [candle(*r) for r in rows]
    m5, m15 = aggregate(5), aggregate(15)
    s5, s15 = _reference(rows, 5), _reference(rows, 15)
    names = {0: "UP", 1: "FLAT", 2: "DOWN"}
    same = total = 0
    for i in range(3500, 6000, 13):
        now = m1[i].open_time + timedelta(minutes=1)
        done5 = [c for c in m5 if c.open_time + timedelta(minutes=5) <= now][-200:]
        done15 = [c for c in m15 if c.open_time + timedelta(minutes=15) <= now][-200:]
        p = pr.predict(pr.MODEL, now, m1[i - 239 : i + 1], done5, done15)
        assert p.p_buy is not None, p.why
        total += 1
        same += (p.features["m15"], p.features["m5"]) == (names[s15[i]], names[s5[i]])
    assert total == 193 and same >= total - 2  # the warm-up may differ at a threshold, rarely
