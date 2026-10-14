"""Evaluate the whole library the same way and decide what, if anything, deserves paper trading.

Per series (one asset's daily history):

1. Simulate every rule once with its published parameters (no fitting) at the series' cost
   scenario, and again at double costs.
2. Skip the first year (burn-in) so every rule and the benchmark start on the same day.
   Split the rest: first 60% "development", last 40% "validation"; also report the last two
   years ("recent").
3. Benchmarks: buy-and-hold, and an exposure-matched holding — holding, all the time, the
   rule's own average position. A long/flat rule on a rising asset shows a positive Sharpe
   ratio just by being invested; only *timing* that beats the exposure-matched holding is
   evidence of skill.
4. Deflated Sharpe ratio (Bailey & López de Prado, 2014) of the timing returns, with N = every
   evaluation in the run (all rules × all series) and the variance of their Sharpe ratios.

Tiers:
- QUALIFIED — timing DSR ≥ 0.95, timing positive in the validation period, still profitable
  at double costs, at least 10 trades, and a drawdown no worse than buy-and-hold.
- WATCH — timing positive in both development and validation, but not significant.
- REJECTED — everything else.
Only QUALIFIED rules may be switched on for paper proposals, and only by the owner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from cosmic.research.library import stats
from cosmic.research.library.catalog import SPECS, Spec, buy_and_hold
from cosmic.research.library.engine import (
    DISCLAIMER,
    Costs,
    Market,
    Metrics,
    Panel,
    Simulation,
    daily_returns,
    simulate,
    simulate_panel,
    window_metrics,
)

BURN_IN = 365
DEV_FRACTION = 0.6
RECENT_DAYS = 730
QUALIFY_DSR = 0.95
MIN_TRADES = 10
TIERS = ("QUALIFIED", "WATCH", "REJECTED")


@dataclass
class Windows:
    start: int
    split: int
    end: int
    recent: int


def windows(n: int) -> Windows:
    start = min(BURN_IN, max(0, n - 60))
    split = start + int((n - start) * DEV_FRACTION)
    return Windows(start, split, n, max(start, n - RECENT_DAYS))


@dataclass
class Evaluation:
    spec: Spec
    series: str
    costs: Costs
    full: Metrics
    dev: Metrics
    val: Metrics
    recent: Metrics
    full_2x: Metrics
    timing_sr: float  # per period, full window
    timing_sr_dev: float
    timing_sr_val: float
    timing_returns: list[float]
    psr_zero: float = 0.0
    dsr: float = 0.0
    tier: str = "REJECTED"
    reasons: list[str] = field(default_factory=list)
    rank: int = 0
    last_weight: float = 0.0
    previous_weight: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.spec.key,
            "number": self.spec.number,
            "series": self.series,
            "costs": {
                "name": self.costs.name,
                "one_way_bps": round(self.costs.one_way * 1e4, 2),
                "basis": self.costs.basis,
            },
            "full": self.full.as_dict(),
            "development": self.dev.as_dict(),
            "validation": self.val.as_dict(),
            "recent": self.recent.as_dict(),
            "full_double_costs": self.full_2x.as_dict(),
            "timing_sharpe_annualised": round(self.timing_sr * 365**0.5, 4),
            "timing_sharpe_development": round(self.timing_sr_dev * 365**0.5, 4),
            "timing_sharpe_validation": round(self.timing_sr_val * 365**0.5, 4),
            "psr_vs_zero": round(self.psr_zero, 4),
            "deflated_sharpe_probability": round(self.dsr, 4),
            "tier": self.tier,
            "reasons": self.reasons,
            "rank": self.rank,
            "last_weight": round(self.last_weight, 4),
            "previous_weight": round(self.previous_weight, 4),
        }


@dataclass
class SeriesReport:
    series: str
    costs: Costs
    dates: tuple[str, str]
    bars: int
    windows: Windows
    buy_hold: dict[str, Metrics]
    evaluations: list[Evaluation]


@dataclass
class LibraryReport:
    series: list[SeriesReport]
    trials: int
    var_sr: dict[str, float]
    disclaimer: str = DISCLAIMER


def _timing(sim: Simulation, bench: Simulation, w: Windows, a: int, b: int) -> list[float]:
    """Rule returns minus an exposure-matched holding of the benchmark, over bars [a, b)."""
    exposure = sum(sim.weights_held[w.start : w.end]) / max(1, w.end - w.start)
    r = daily_returns(sim, a, b)
    rb = daily_returns(bench, a, b)
    return [x - exposure * y for x, y in zip(r, rb, strict=False)]


def _evaluate(
    spec: Spec,
    series: str,
    sim: Simulation,
    sim2: Simulation,
    bench: Simulation,
    weights: Sequence[float],
    costs: Costs,
    w: Windows,
) -> Evaluation:
    timing = _timing(sim, bench, w, w.start, w.end)
    ev = Evaluation(
        spec=spec,
        series=series,
        costs=costs,
        full=window_metrics(sim, w.start, w.end, weights=weights),
        dev=window_metrics(sim, w.start, w.split, weights=weights),
        val=window_metrics(sim, w.split, w.end, weights=weights),
        recent=window_metrics(sim, w.recent, w.end, weights=weights),
        full_2x=window_metrics(sim2, w.start, w.end, weights=weights),
        timing_sr=stats.sharpe(timing),
        timing_sr_dev=stats.sharpe(_timing(sim, bench, w, w.start, w.split)),
        timing_sr_val=stats.sharpe(_timing(sim, bench, w, w.split, w.end)),
        timing_returns=timing,
    )
    _, _, skew, kurt = stats.moments(daily_returns(sim, w.start, w.end))
    ev.psr_zero = stats.psr(ev.full.sr_period, 0.0, ev.full.days, skew, kurt)
    ev.last_weight = float(weights[-1]) if weights else 0.0
    ev.previous_weight = float(weights[-2]) if len(weights) > 1 else 0.0
    return ev


def evaluate_single(m: Market, costs: Costs, specs: Sequence[Spec] = SPECS) -> SeriesReport:
    w = windows(len(m))
    bench_w = buy_and_hold(m, {})
    bench = simulate(m, bench_w, costs)
    evaluations = []
    for spec in specs:
        if spec.fn is None:
            continue
        weights = spec.fn(m, spec.params)
        evaluations.append(
            _evaluate(
                spec,
                m.key,
                simulate(m, weights, costs),
                simulate(m, weights, costs.scaled(2)),
                bench,
                weights,
                costs,
                w,
            )
        )
    return SeriesReport(
        m.key,
        costs,
        (m.dates[0].isoformat(), m.dates[-1].isoformat()),
        len(m),
        w,
        _bench_metrics(bench, w),
        evaluations,
    )


def evaluate_panel(p: Panel, costs: Costs, specs: Sequence[Spec] = SPECS) -> SeriesReport:
    keys = list(p.markets)
    w = windows(len(p.dates))
    half = {k: [1.0 / len(keys)] * len(p.dates) for k in keys}
    bench = simulate_panel(p, half, costs)
    evaluations = []
    for spec in specs:
        if spec.panel_fn is None:
            continue
        weights = spec.panel_fn(p, spec.params)
        total = [sum(ws[t] for ws in weights.values()) for t in range(len(p.dates))]
        evaluations.append(
            _evaluate(
                spec,
                "+".join(keys),
                simulate_panel(p, weights, costs),
                simulate_panel(p, weights, costs.scaled(2)),
                bench,
                total,
                costs,
                w,
            )
        )
    return SeriesReport(
        "+".join(keys),
        costs,
        (p.dates[0].isoformat(), p.dates[-1].isoformat()),
        len(p.dates),
        w,
        _bench_metrics(bench, w),
        evaluations,
    )


def _bench_metrics(bench: Simulation, w: Windows) -> dict[str, Metrics]:
    return {
        "full": window_metrics(bench, w.start, w.end),
        "development": window_metrics(bench, w.start, w.split),
        "validation": window_metrics(bench, w.split, w.end),
        "recent": window_metrics(bench, w.recent, w.end),
    }


def judge(reports: Sequence[SeriesReport]) -> LibraryReport:
    """Deflate every Sharpe ratio by all trials in the run, then tier and rank per series."""
    trials = sum(len(r.evaluations) for r in reports)
    var_sr: dict[str, float] = {}
    for r in reports:
        v = stats.variance([e.timing_sr for e in r.evaluations])
        var_sr[r.series] = v
        bh = r.buy_hold["full"]
        for e in r.evaluations:
            _, _, skew, kurt = stats.moments(e.timing_returns)
            e.dsr = stats.psr(
                e.timing_sr, stats.expected_max_sharpe(trials, v), len(e.timing_returns), skew, kurt
            )
            e.tier, e.reasons = _tier(e, bh)
        ranked = sorted(r.evaluations, key=lambda e: (TIERS.index(e.tier), -e.dsr, -e.timing_sr))
        for i, e in enumerate(ranked, start=1):
            e.rank = i
        r.evaluations = ranked
    return LibraryReport(list(reports), trials, var_sr)


def _tier(e: Evaluation, bh: Metrics) -> tuple[str, list[str]]:
    failed: list[str] = []
    if e.dsr < QUALIFY_DSR:
        failed.append(
            f"timing skill not significant after {QUALIFY_DSR:.0%} deflation "
            f"(probability {e.dsr:.2f})"
        )
    if e.timing_sr_val <= 0:
        failed.append("timing did not beat the exposure-matched holding in the validation period")
    if e.full_2x.sharpe <= 0:
        failed.append("not profitable at double the assumed costs")
    if e.full.trades < MIN_TRADES:
        failed.append(f"only {e.full.trades} trades (minimum {MIN_TRADES})")
    if e.full.max_drawdown < bh.max_drawdown:
        failed.append("deeper drawdown than buy-and-hold")
    if not failed:
        return "QUALIFIED", ["passed every test"]
    if e.timing_sr_dev > 0 and e.timing_sr_val > 0:
        return "WATCH", failed
    return "REJECTED", failed
