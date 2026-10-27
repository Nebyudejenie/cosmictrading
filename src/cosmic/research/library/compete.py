"""The rule competition (ADR-031): which published rule, if any, has earned the auto-trader?

Every single-market rule of the strategy library is replayed with the auto-trader's own
mechanics (``expect.py``: decisions at the close filled at the next open, the 3×ATR stop with
gaps at the open, sizing to the per-trade loss, the daily limits) on each market the
auto-trader trades, on hourly and on daily bars, with the costs it would pay there. Results
are in R, multiples of the per-trade loss limit, so they compare across markets and plans.

The protocol, fixed in code before any result was seen (a change is a new code version):

Development and validation use only bars before ``FORWARD_FROM``, so the split is the same
on every run and the forward test never feeds the backtest verdict (review of 2026-10-23).

1. **Development:** the first 60 % of each series before the forward date, after a warm-up.
   A rule is a CANDIDATE on a series when it traded at least ``MIN_TRADES`` there with a
   positive mean R per trade and a t-statistic of at least ``DEV_T``.
2. **Validation:** the last 40 %, never used for choosing. A candidate QUALIFIES when it is
   positive there too, with at least half as many trades, a t-statistic above the one-sided
   5 % threshold shared among all candidates (Bonferroni), and still positive at double costs.
   A candidate positive in validation without that is PROMISING; everything else is REJECTED.
3. **Ideas** (``hypotheses.IDEAS``, addendum): filters this system found in its own research of
   the past. Found by looking at the past, they are judged on the forward test only (tier
   IDEA) and earn the auto-trader only with a forward record that is positive with a
   t-statistic of at least ``DEV_T``.
4. **Forward test:** from ``FORWARD_FROM``, registered before any forward bar existed, every
   rule is scored on new bars only, as they arrive. A QUALIFIED rule has EARNED the
   auto-trader once its forward record has ``FORWARD_TRADES`` trades and is positive after
   costs. Earning it means it may be proposed: it is added to the auto-trader only by a
   reviewed code change, and started only by the owner (ADR-030).

The t-statistics use the normal approximation. Floats: analytics only (ADR-007). Historical
simulations are not promises.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Any

from cosmic.research.library import expect
from cosmic.research.library.catalog import SPECS, Spec
from cosmic.research.library.engine import Market

FORWARD_FROM = datetime(2026, 10, 23, tzinfo=UTC)  # registered 2026-10-22, before any forward bar
DEV_FRACTION = 0.6
WARM_UP_BARS = 250  # the longest published lookbacks (200 bars) are defined before the start
R = 100.0  # the reference loss per trade; every result is reported in multiples of it
DEV_T = 2.0
ALPHA = 0.05
MIN_TRADES = {"1h": 30, "1d": 16}  # in development; validation needs half as many
FORWARD_TRADES = {"1h": 30, "1d": 10}
TIERS = ("QUALIFIED", "PROMISING", "IDEA", "REJECTED", "N/A")
LIMITS = expect.Limits(per_trade=R, daily_stop=3 * R, trades_per_day=5, open_positions=1)
TINY_LOT = 1e-9  # sizing in R without whole-lot rounding


@dataclass(frozen=True)
class Series:
    """One market as the auto-trader would trade it, on one timeframe."""

    market: str  # the traded instrument, e.g. CFD:BTC/USD@HFM
    timeframe: str  # 1h | 1d
    bars: Market  # replayed: completed bars, dates as UTC datetimes
    fee: float  # per fill, a fraction of the value
    half_spread: float
    rule_bars: Market | None = None  # the same bars with calendar dates, for daily rules
    swap: float = 0.0  # long swap per night, a fraction of the price (negative: paid)
    swap_triple: int | None = None
    weekend_closed: bool = False  # the venue does not trade at weekends (gold CFDs)


@dataclass(frozen=True)
class Score:
    trades: int
    total_r: float
    mean_r: float | None
    t: float | None
    win_rate: float | None
    drawdown_r: float  # the deepest fall of the running R from its high (≤ 0)

    @staticmethod
    def of(result: expect.Result) -> Score:
        rs = [t.pnl / R for t in sorted(result.trades, key=lambda t: t.closed)]
        n = len(rs)
        mean = sum(rs) / n if n else None
        t_stat = None
        if mean is not None and n >= 2:
            sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1))
            t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else None
        running = peak = drawdown = 0.0
        for r in rs:
            running += r
            peak = max(peak, running)
            drawdown = min(drawdown, running - peak)
        return Score(
            trades=n,
            total_r=sum(rs),
            mean_r=mean,
            t=t_stat,
            win_rate=sum(1 for r in rs if r > 0) / n if n else None,
            drawdown_r=drawdown,
        )

    def as_dict(self) -> dict[str, Any]:
        def rnd(x: float | None, k: int = 4) -> float | None:
            return None if x is None else round(x, k)

        return {
            "trades": self.trades,
            "total_r": rnd(self.total_r),
            "mean_r": rnd(self.mean_r),
            "t": rnd(self.t),
            "win_rate": rnd(self.win_rate),
            "drawdown_r": rnd(self.drawdown_r),
        }


EMPTY = Score(0, 0.0, None, None, None, 0.0)


@dataclass
class Entry:
    rule: str
    name: str
    market: str
    timeframe: str
    dev: Score = EMPTY
    val: Score = EMPTY
    val_double: Score = EMPTY
    forward: Score = EMPTY
    candidate: bool = False
    tier: str = "REJECTED"
    earned: bool = False
    curve: list[float] = field(default_factory=list)  # running R, development then validation
    split_trade: int = 0  # the first validation trade's place in ``curve``
    error: str | None = None
    idea: bool = False  # found in this system's research: judged forward only
    forward_curve: list[float] = field(default_factory=list)  # running R since FORWARD_FROM

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "name": self.name,
            "market": self.market,
            "timeframe": self.timeframe,
            "dev": self.dev.as_dict(),
            "val": self.val.as_dict(),
            "val_double": self.val_double.as_dict(),
            "forward": self.forward.as_dict(),
            "candidate": self.candidate,
            "tier": self.tier,
            "earned": self.earned,
            "curve": [round(v, 3) for v in self.curve],
            "split_trade": self.split_trade,
            "error": self.error,
            "idea": self.idea,
            "forward_curve": [round(v, 3) for v in self.forward_curve],
        }


@dataclass(frozen=True)
class Window:
    start: datetime
    split: datetime
    end: datetime  # the last bar before the forward date: development and validation end here
    last: datetime | None = None  # the series' last bar (the forward test runs to it)


@dataclass
class Report:
    entries: list[Entry]
    trials: int  # rule × series pairs that ran
    candidates: int
    val_threshold: float  # the validation t-statistic a candidate needs
    windows: dict[str, Window]  # by "market timeframe"
    forward_from: datetime = FORWARD_FROM

    @property
    def qualified(self) -> list[Entry]:
        return [e for e in self.entries if e.tier == "QUALIFIED"]

    @property
    def earned(self) -> list[Entry]:
        return [e for e in self.entries if e.earned]

    def as_dict(self) -> dict[str, Any]:
        return {
            "trials": self.trials,
            "candidates": self.candidates,
            "val_threshold": round(self.val_threshold, 4),
            "forward_from": self.forward_from.isoformat(),
            "windows": {
                k: {
                    "start": w.start.isoformat(),
                    "split": w.split.isoformat(),
                    "end": w.end.isoformat(),
                    "last": w.last.isoformat() if w.last else None,
                }
                for k, w in self.windows.items()
            },
            "entries": [e.as_dict() for e in self.entries],
        }


def window(bars: Market) -> Window | None:
    """Development and validation of a series: its bars before ``FORWARD_FROM``, after the
    warm-up, 60 % then 40 %. Bars from the forward date on belong to the forward test only."""
    every = [d for d in bars.dates if isinstance(d, datetime)]
    dates = [d for d in every if d < FORWARD_FROM]
    if len(dates) < WARM_UP_BARS + 100:
        return None
    first = WARM_UP_BARS
    split = first + int((len(dates) - first) * DEV_FRACTION)
    return Window(dates[first], dates[split], dates[-1], every[-1])


def threshold(candidates: int) -> float:
    """The validation t-statistic needed: one-sided 5 %, shared among the candidates."""
    return NormalDist().inv_cdf(1 - ALPHA / max(candidates, 1))


def _venue(s: Series, costs: float = 1.0) -> expect.Venue:
    return expect.Venue(
        s.market,
        s.bars,
        TINY_LOT,
        TINY_LOT,
        s.fee * costs,
        s.half_spread * costs,
        swap=min(s.swap, 0.0) * costs + max(s.swap, 0.0),  # a stress doubles a cost, not a credit
        swap_triple=s.swap_triple,
        weekend_closed=s.weekend_closed,
        decide_in_session=s.weekend_closed and s.timeframe == "1d",
    )


def _replay(
    spec: Spec,
    s: Series,
    signals: expect.Signals,
    start: datetime,
    end: datetime | None = None,
    costs: float = 1.0,
) -> expect.Result:
    return expect.simulate(
        [_venue(s, costs)],
        LIMITS,
        spec,
        start=start,
        end=end,
        slippage=expect.SLIPPAGE * costs,
        signals={s.market: signals},
    )


def _score_one(spec: Spec, s: Series, w: Window) -> Entry:
    e = Entry(spec.key, spec.name, s.market, s.timeframe)
    signals = expect.signals_for(spec, s.rule_bars or s.bars)
    dev = _replay(spec, s, signals, w.start, w.split)
    val = _replay(spec, s, signals, w.split, FORWARD_FROM)
    e.dev, e.val = Score.of(dev), Score.of(val)
    e.val_double = Score.of(_replay(spec, s, signals, w.split, FORWARD_FROM, costs=2.0))
    if w.last is not None and w.last >= FORWARD_FROM:
        fwd = _replay(spec, s, signals, FORWARD_FROM)
        e.forward = Score.of(fwd)
        running = 0.0
        for t in sorted(fwd.trades, key=lambda t: t.closed):
            running += t.pnl / R
            e.forward_curve.append(running)
    running = 0.0
    for t in [
        *sorted(dev.trades, key=lambda t: t.closed),
        *sorted(val.trades, key=lambda t: t.closed),
    ]:
        running += t.pnl / R
        e.curve.append(running)
    e.split_trade = len(dev.trades)
    e.candidate = (
        e.dev.trades >= MIN_TRADES[s.timeframe]
        and (e.dev.mean_r or 0.0) > 0
        and (e.dev.t or 0.0) >= DEV_T
    )
    return e


def compete(
    series: Sequence[Series],
    specs: Sequence[Spec] = SPECS,
    progress: Callable[[int, int], None] | None = None,
    ideas: Sequence[Spec] = (),
) -> Report:
    """Run the whole competition (CPU-bound: the worker runs it in a thread)."""
    singles = [s for s in specs if s.fn is not None]
    entries: list[Entry] = []
    windows: dict[str, Window] = {}
    done, total = 0, (len(singles) + len(ideas)) * len(series)
    for s in series:
        w = window(s.bars)
        if w is None:
            continue
        windows[f"{s.market} {s.timeframe}"] = w
        for spec, is_idea in [*((x, False) for x in singles), *((x, True) for x in ideas)]:
            if is_idea and "hourly" in spec.tags and s.timeframe != "1h":
                continue
            try:
                e = _score_one(spec, s, w)
            except Exception as exc:  # a rule that cannot run on these bars is shown, not hidden
                e = Entry(
                    spec.key, spec.name, s.market, s.timeframe, tier="N/A", error=str(exc)[:200]
                )
            if is_idea:
                e.idea, e.candidate = True, False
                if e.error is None:
                    e.tier = "IDEA"
                    e.earned = (
                        e.forward.trades >= FORWARD_TRADES[e.timeframe]
                        and (e.forward.mean_r or 0.0) > 0
                        and (e.forward.t or 0.0) >= DEV_T
                    )
            entries.append(e)
            done += 1
            if progress is not None:
                progress(done, total)
    ran = [e for e in entries if e.error is None and not e.idea]
    candidates = [e for e in ran if e.candidate]
    needed = threshold(len(candidates))
    for e in ran:
        if not e.candidate:
            continue
        positive = (e.val.mean_r or 0.0) > 0
        if (
            positive
            and e.val.trades >= MIN_TRADES[e.timeframe] // 2
            and (e.val.t or 0.0) >= needed
            and (e.val_double.mean_r or 0.0) > 0
        ):
            e.tier = "QUALIFIED"
            e.earned = (
                e.forward.trades >= FORWARD_TRADES[e.timeframe] and (e.forward.mean_r or 0.0) > 0
            )
        elif positive:
            e.tier = "PROMISING"
    order = {tier: i for i, tier in enumerate(TIERS)}
    entries.sort(key=lambda e: (order[e.tier], not e.candidate, -(e.dev.t or -99.0)))
    return Report(entries, len(ran), len(candidates), needed, windows)
