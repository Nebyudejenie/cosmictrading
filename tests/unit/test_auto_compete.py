"""The rule competition (research/library/compete.py, ADR-031): the protocol finds a real edge,
does not crown noise, raises the bar with the number of rules checked, shows a rule that
cannot run, and lets only a forward record earn the auto-trader."""

from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cosmic.research.library import compete
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import Market

T0 = datetime(2025, 9, 1, tzinfo=UTC)
TURTLE = BY_KEY["turtle_20_10"]


def market(closes: list[float], key: str = "SYN", wiggle: float = 0.002) -> Market:
    """Hourly bars from closes: each opens at the previous close, with a small range."""
    opens = [closes[0], *closes[:-1]]
    return Market(
        key,
        tuple(T0 + timedelta(hours=i) for i in range(len(closes))),
        tuple(opens),
        tuple(max(o, c) * (1 + wiggle) for o, c in zip(opens, closes, strict=True)),
        tuple(min(o, c) * (1 - wiggle) for o, c in zip(opens, closes, strict=True)),
        tuple(closes),
        tuple(1.0 for _ in closes),
    )


def trending_waves(cycles: int, seed: int = 1) -> list[float]:
    """Long, steady rises and short falls: a breakout rule has a real edge here."""
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


def noise(n: int, seed: int = 2) -> list[float]:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        price *= math.exp(rng.gauss(0, 0.006))
        out.append(price)
    return out


def series(closes: list[float], tf: str = "1h", key: str = "X") -> compete.Series:
    return compete.Series(key, tf, market(closes, key), fee=0.0, half_spread=0.0003)


def test_the_protocol_finds_a_real_edge() -> None:
    report = compete.compete([series(trending_waves(80))], [TURTLE])
    (e,) = report.entries
    assert e.candidate and e.tier == "QUALIFIED"
    assert e.dev.trades >= compete.MIN_TRADES["1h"] and (e.val.mean_r or 0) > 0
    assert (e.val_double.mean_r or 0) > 0 and (e.val.t or 0) >= report.val_threshold
    assert e.split_trade == e.dev.trades and len(e.curve) == e.dev.trades + e.val.trades
    assert not e.earned  # the synthetic bars end before the forward test began


def test_noise_crowns_no_rule() -> None:
    specs = [BY_KEY[k] for k in ("turtle_20_10", "turtle_55_20", "keltner_breakout")]
    report = compete.compete([series(noise(9_000))], specs)
    assert report.trials == 3 and not report.qualified
    assert all(e.tier in ("PROMISING", "REJECTED") for e in report.entries)


def test_the_bar_rises_with_the_number_of_rules_checked() -> None:
    assert compete.threshold(1) == pytest.approx(1.645, abs=1e-3)
    assert compete.threshold(10) == pytest.approx(2.576, abs=1e-3)
    assert compete.threshold(0) == compete.threshold(1)


def test_development_and_validation_split_after_the_warm_up() -> None:
    m = market(noise(1_250))
    w = compete.window(m)
    assert w is not None
    assert w.start == m.dates[compete.WARM_UP_BARS] and w.end == m.dates[-1]
    assert w.split == m.dates[compete.WARM_UP_BARS + 600]  # 60 % of the 1,000 after it
    assert compete.window(market(noise(300))) is None  # too short to judge


def test_a_rule_that_cannot_run_is_shown_not_hidden() -> None:
    def broken(m: Market, p: dict[str, object]) -> list[float]:
        raise TypeError("needs calendar dates")

    bad = replace(TURTLE, key="broken", name="Broken", fn=broken)
    report = compete.compete([series(noise(2_000))], [TURTLE, bad])
    (na,) = [e for e in report.entries if e.tier == "N/A"]
    assert na.rule == "broken" and na.error == "needs calendar dates"
    assert report.trials == 1  # only rules that ran count as trials


def test_only_a_forward_record_earns_the_auto_trader(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = trending_waves(80)
    m = market(closes)
    split_forward = m.dates[len(closes) - 2_600]  # the last 20 cycles come "after" registration
    monkeypatch.setattr(compete, "FORWARD_FROM", split_forward)
    (e,) = compete.compete([series(closes)], [TURTLE]).entries
    assert e.tier == "QUALIFIED" and e.forward.trades >= compete.FORWARD_TRADES["1h"] // 2
    assert e.earned == (
        e.forward.trades >= compete.FORWARD_TRADES["1h"] and (e.forward.mean_r or 0) > 0
    )
    monkeypatch.setitem(compete.FORWARD_TRADES, "1h", 5)
    (again,) = compete.compete([series(closes)], [TURTLE]).entries
    assert again.earned  # enough forward trades, in profit


def test_the_report_is_stored_as_plain_data() -> None:
    report = compete.compete([series(trending_waves(40))], [TURTLE])
    d = report.as_dict()
    assert d["trials"] == 1 and d["entries"][0]["rule"] == "turtle_20_10"
    assert set(d["entries"][0]["dev"]) == {
        "trades",
        "total_r",
        "mean_r",
        "t",
        "win_rate",
        "drawdown_r",
    }
    assert d["forward_from"] == compete.FORWARD_FROM.isoformat()


def test_the_registered_forward_date_is_fixed() -> None:
    """Changing it after results exist would let hindsight in; it is part of the protocol."""
    assert datetime(2026, 10, 23, tzinfo=UTC) == compete.FORWARD_FROM
