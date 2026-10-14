"""The library's judgement: noise never qualifies; real (here: impossible) skill does."""

from __future__ import annotations

from collections import Counter
from typing import Any

from cosmic.research.library.catalog import BY_KEY, SPECS, Spec
from cosmic.research.library.engine import BINANCE_LIKE, Market, align
from cosmic.research.library.evaluate import evaluate_panel, evaluate_single, judge
from tests.library_data import random_walk


def test_catalogue_has_exactly_100_documented_rules() -> None:
    assert len(SPECS) == 100 and len(BY_KEY) == 100
    assert [s.number for s in SPECS] == list(range(1, 101))
    for s in SPECS:
        assert len(s.rule) > 20 and len(s.source) > 10, s.key
        assert (s.fn is None) != (s.panel_fn is None), s.key
    families = Counter(s.family for s in SPECS)
    assert families["ROTATION"] == 5 and sum(families.values()) == 100


def test_noise_does_not_qualify() -> None:
    m = random_walk(bars=1500, seed=5, drift=0.0, vol=0.03)
    singles = [s for s in SPECS if s.fn][:40]
    report = judge([evaluate_single(m, BINANCE_LIKE, singles)])
    tiers = Counter(e.tier for e in report.series[0].evaluations)
    assert tiers["QUALIFIED"] == 0
    assert report.trials == 40
    ranks = [e.rank for e in report.series[0].evaluations]
    assert ranks == list(range(1, 41))


def _oracle(m: Market, _p: dict[str, Any]) -> list[float]:
    """Cheats: knows tomorrow's open-to-open move. Only here to prove the gate can pass."""
    return [1.0 if t + 2 < len(m) and m.open[t + 2] > m.open[t + 1] else 0.0 for t in range(len(m))]


def test_genuine_skill_would_qualify() -> None:
    m = random_walk(bars=1500, seed=9, drift=0.0, vol=0.03)
    oracle = Spec(
        101, "oracle", "Oracle", "TEST", "Knows tomorrow (test only).", "test", {}, _oracle
    )
    others = [s for s in SPECS if s.fn][:30]
    report = judge([evaluate_single(m, BINANCE_LIKE, [*others, oracle])])
    best = report.series[0].evaluations[0]
    assert best.spec.key == "oracle" and best.tier == "QUALIFIED", best.reasons
    assert best.dsr > 0.99


def test_panel_rules_evaluated_on_aligned_markets() -> None:
    a = random_walk("A", bars=900, seed=1)
    b = random_walk("B", bars=900, seed=2, vol=0.01)
    report = judge([evaluate_panel(align([a, b]), BINANCE_LIKE)])
    evs = report.series[0].evaluations
    assert {e.spec.family for e in evs} == {"ROTATION"} and len(evs) == 5
    assert report.series[0].series == "A+B"
    row = evs[0].as_dict()
    assert (
        row["tier"] in {"QUALIFIED", "WATCH", "REJECTED"} and "deflated_sharpe_probability" in row
    )
