"""No strategy in the library can see the future.

For every rule, positions computed on a truncated history must equal the same positions
computed on the full history. A rule that peeked at bar t+1 would change its earlier
positions when the later bars are removed.
"""

from __future__ import annotations

import pytest

from cosmic.research.library.catalog import SPECS, Spec
from cosmic.research.library.engine import Panel, align
from tests.library_data import random_walk

FULL = random_walk(bars=600, seed=11)
OTHER = random_walk("SYN2", bars=600, seed=23, drift=0.0002, vol=0.012)
CUTS = (420, 517, 599)


def _panel_head(p: Panel, n: int) -> Panel:
    return Panel({k: m.head(n) for k, m in p.markets.items()}, p.dates[:n])


@pytest.mark.parametrize("spec", [s for s in SPECS if s.fn], ids=lambda s: s.key)
def test_single_asset_rule_is_causal(spec: Spec) -> None:
    assert spec.fn is not None
    full = spec.fn(FULL, spec.params)
    assert len(full) == len(FULL)
    assert all(0.0 <= w <= 1.0 for w in full), "weights must be long or flat"
    for n in CUTS:
        assert spec.fn(FULL.head(n), spec.params) == full[:n], f"look-ahead at cut {n}"


@pytest.mark.parametrize("spec", [s for s in SPECS if s.panel_fn], ids=lambda s: s.key)
def test_rotation_rule_is_causal(spec: Spec) -> None:
    assert spec.panel_fn is not None
    panel = align([FULL, OTHER])
    full = spec.panel_fn(panel, spec.params)
    for t in range(len(panel.dates)):
        assert sum(w[t] for w in full.values()) <= 1.0 + 1e-9
    for n in CUTS:
        head = spec.panel_fn(_panel_head(panel, n), spec.params)
        for key, series in full.items():
            assert head[key] == series[:n], f"look-ahead in {key} at cut {n}"


def test_a_peeking_rule_is_caught() -> None:
    """The check itself works: a rule that reads tomorrow's close fails it."""

    def peek(m, _p):  # type: ignore[no-untyped-def]
        return [
            1.0 if t + 1 < len(m) and m.close[t + 1] > m.close[t] else 0.0 for t in range(len(m))
        ]

    full = peek(FULL, {})
    assert any(peek(FULL.head(n), {}) != full[:n] for n in CUTS)
