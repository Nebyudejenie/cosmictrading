"""Ideas from this system's research (research/library/hypotheses.py, ADR-031 addendum): a
published rule with a causal entry filter, judged on the forward test only."""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from cosmic.research.library import compete, hypotheses
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import Market

T0 = datetime(2025, 9, 1, tzinfo=UTC)


def hourly(closes: list[float]) -> Market:
    opens = [closes[0], *closes[:-1]]
    return Market(
        "X",
        tuple(T0 + timedelta(hours=i) for i in range(len(closes))),
        tuple(opens),
        tuple(max(o, c) * 1.002 for o, c in zip(opens, closes, strict=True)),
        tuple(min(o, c) * 0.998 for o, c in zip(opens, closes, strict=True)),
        tuple(closes),
        tuple(1.0 for _ in closes),
    )


def walk(n: int, seed: int = 3) -> list[float]:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        price *= math.exp(rng.gauss(0.0002, 0.006))
        out.append(price)
    return out


def test_an_idea_enters_only_where_its_filter_allows_and_exits_as_the_rule() -> None:
    m = hourly(walk(3_000))
    base = BY_KEY["turtle_20_10"]
    assert base.fn is not None
    w = base.fn(m, base.params)
    busy = hypotheses.busy_hours(m)
    f = hypotheses.filtered(base, hypotheses.busy_hours)(m, base.params)
    entries = [t for t in range(1, len(f)) if f[t - 1] == 0 and f[t] > 0]
    assert entries and all(busy[t] and w[t - 1] == 0 and w[t] > 0 for t in entries)
    assert all(f[t] <= w[t] for t in range(len(f)))  # never long when the rule is flat
    base_entries = [t for t in range(1, len(w)) if w[t - 1] == 0 and w[t] > 0]
    assert len(entries) < len(base_entries)  # some entries fell outside the busy hours


def test_ideas_are_causal() -> None:
    m = hourly(walk(3_000, seed=5))
    for spec in hypotheses.IDEAS:
        assert spec.fn is not None
        full = spec.fn(m, spec.params)
        for cut in (1_400, 2_200):
            assert spec.fn(m.head(cut), spec.params) == full[:cut], spec.key


def test_the_trend_filter_uses_fifty_days_on_any_bars() -> None:
    m = hourly(walk(1_300))
    assert not any(hypotheses.above_trend(m)[:1_199])  # 1,200 hours before it is defined
    days = Market(
        "D",
        tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(60)),
        *(tuple(float(100 + i) for i in range(60)),) * 4,
        tuple(1.0 for _ in range(60)),
    )
    assert hypotheses.above_trend(days)[49:] == [True] * 11  # a rising market, from day 50
    with pytest.raises(ValueError, match="hourly"):
        hypotheses.busy_hours(days)


def test_ideas_are_judged_forward_only(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = walk(9_000)
    m = hourly(closes)
    monkeypatch.setattr(compete, "FORWARD_FROM", m.dates[6_000])
    s = compete.Series("X", "1h", m, 0.0, 0.0003)
    report = compete.compete([s], [BY_KEY["turtle_20_10"]], ideas=hypotheses.IDEAS)
    ideas = [e for e in report.entries if e.idea]
    assert {e.rule for e in ideas} == {i.key for i in hypotheses.IDEAS}
    assert all(e.tier == "IDEA" and not e.candidate for e in ideas)
    assert report.trials == 1  # ideas are not trials of the backtest protocol
    for e in ideas:
        assert len(e.forward_curve) == e.forward.trades
        assert e.earned == (
            e.forward.trades >= compete.FORWARD_TRADES["1h"]
            and (e.forward.mean_r or 0) > 0
            and (e.forward.t or 0) >= compete.DEV_T
        )
    daily = compete.Series("X", "1d", m, 0.0, 0.0003)  # hourly-only ideas skip daily bars
    on_daily = compete.compete([daily], [], ideas=hypotheses.IDEAS).entries
    assert {e.rule for e in on_daily} == {"idea_turtle_trend", "idea_keltner_trend"}
