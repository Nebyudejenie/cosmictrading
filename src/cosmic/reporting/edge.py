"""Is the rule's edge real? (S47)

Each daily rule's five-year replay on Binance gold says what it would have done. Three more views
say how far to trust that, all HISTORICAL SIMULATION on the same prices, costs and limits:

- **Nearby settings:** the rule with its numbers a little shorter and longer (a breakout of 44,
  55 or 66 days, say). A result that holds across them did not come from one lucky number.
- **Switching, and restarting:** on each 1 January, the rule that had made the most so far, as
  it stood that day (an open trade valued at that day's close), started afresh for the year; and
  each rule restarted every 1 January. A rule started afresh waits for its own next signal, as a
  newly started auto-trader does; set beside the same rule kept running, that shows what a lapse
  costs.
- **Since chosen:** each rule replayed from the day the daily rules were registered
  (``mandates.RULES_REGISTERED``, ADR-030 addendum 5): days the research never saw. Beside it,
  how often the rule's own history did as badly over as many trades, in units of each trade's
  risk (a half-size trade counts as half), from its trades closed before the registration.

They all come from one ``autotrader.Bench``: the prices are read once, each rule's signals worked
out once. The views are kept for the day (``build``), so the page does not replay them on every
visit.
"""

from __future__ import annotations

import random
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from cosmic.reporting import autotrader
from cosmic.research.library import expect
from cosmic.research.library.catalog import BY_KEY, Spec
from cosmic.trading import mandates

BREAKOUT_SCALES = (0.8, 1.0, 1.2)  # a breakout's entry and exit lengths, each
AVERAGE_SCALES = (0.75, 0.875, 1.0, 1.125, 1.25)  # an average's length
MIN_HISTORY = timedelta(days=365)  # the first pick needs a year of results behind it
RESAMPLES = 10_000
SEED = 7
UNUSUAL = 0.05  # a forward record this rare in the rule's history is worth a look (S48)
KEEP_VIEWS = 8  # views kept in memory (one a day per market and policy, in practice)


def nearby_settings(spec: Spec) -> list[dict[str, Any]]:
    """The rule's own settings and those around them; none for a rule with no single number to
    vary (an ensemble of six rules)."""
    p = spec.params
    if set(p) == {"entry", "exit"}:
        return [
            {"entry": round(p["entry"] * a), "exit": round(p["exit"] * b)}
            for a in BREAKOUT_SCALES
            for b in BREAKOUT_SCALES
        ]
    if set(p) == {"ma", "n"}:
        return [{"ma": p["ma"], "n": round(p["n"] * a)} for a in AVERAGE_SCALES]
    return []


@dataclass(frozen=True)
class Nearby:
    """The rule over its nearby settings, its own included."""

    settings: int
    made_money: int
    low: float
    high: float


def _total(e: autotrader.Expectation) -> float:
    """A replay's result, an open trade marked at its last close."""
    return e.summary.total if e.summary is not None else 0.0


@dataclass(frozen=True)
class Year:
    year: int
    known: dict[str, float]  # each rule's result so far on 1 January, as it stood that day
    picked: str  # the rule with the most made so far (ties: the first listed)
    made: float  # what the picked rule made that year, started afresh on 1 January
    kept: dict[str, float]  # what each rule made that year, kept running from before
    so_far: bool = False  # the year is not over


@dataclass(frozen=True)
class Switching:
    years: list[Year]
    restarted: dict[str, float]  # each rule started afresh every 1 January, over the years

    @property
    def total(self) -> float:
        return sum(y.made for y in self.years)

    @property
    def kept_all(self) -> dict[str, float]:
        """Each rule kept running over the same years."""
        return {k: sum(y.kept[k] for y in self.years) for k in self.restarted}

    @property
    def beaten(self) -> int:
        """How many of the rules kept running switching did better than."""
        return sum(1 for v in self.kept_all.values() if self.total > v)


def switching(
    b: autotrader.Bench,
    rules: Sequence[str],
    now: datetime,
    unstopped: Mapping[str, autotrader.Expectation],
) -> Switching | None:
    """Each year beginning at least a year after the bench starts: the pick as it stood on 1
    January, what it made started afresh, what each rule made kept running, and each rule
    restarted every 1 January. None without such a year. ``unstopped``: each rule's whole replay
    without the stop for good (its value now). No stop for good here: a fresh start each year
    would have its own."""
    years = [
        y
        for y in range(b.start.year, now.year + 1)
        if datetime(y, 1, 1, tzinfo=UTC) - b.start >= MIN_HISTORY
        and datetime(y, 1, 1, tzinfo=UTC) < now
    ]
    if not years or not rules:
        return None
    specs = {k: BY_KEY[k] for k in rules}
    values: dict[tuple[str, datetime | None], float] = {}

    def value(k: str, at: datetime | None) -> float:  # kept running, as it stood at ``at``
        if (k, at) not in values:
            whole = unstopped[k] if at is None else b.run(specs[k], total_stop=None, until=at)
            values[(k, at)] = _total(whole)
        return values[(k, at)]

    def fresh(k: str, start: datetime, end: datetime | None) -> float:
        return _total(b.run(specs[k], total_stop=None, since=start, until=end))

    out: list[Year] = []
    restarted = dict.fromkeys(rules, 0.0)
    for y in years:
        start = datetime(y, 1, 1, tzinfo=UTC)
        end = datetime(y + 1, 1, 1, tzinfo=UTC) if y < now.year else None
        known = {k: value(k, start) for k in rules}
        later = {k: value(k, end) for k in rules}
        afresh = {k: fresh(k, start, end) for k in rules}
        for k in rules:
            restarted[k] += afresh[k]
        picked = max(rules, key=lambda k: (known[k], -list(rules).index(k)))
        kept = {k: later[k] - known[k] for k in rules}
        out.append(Year(y, known, picked, afresh[picked], kept, end is None))
    return Switching(out, restarted)


def as_bad(history: Sequence[float], observed: float, n: int, seed: int = SEED) -> float | None:
    """The share of runs of ``n`` trades drawn from ``history`` (with replacement) that ended at
    ``observed`` or lower. None without a history or a trade."""
    if n <= 0 or not history:
        return None
    rng = random.Random(seed)  # noqa: S311 - a reproducible simulation  # nosec B311
    worse = sum(1 for _ in range(RESAMPLES) if sum(rng.choices(history, k=n)) <= observed + 1e-9)
    return worse / RESAMPLES


def in_risk(trades: Sequence[expect.Trade]) -> list[float]:
    """Each trade's result in units of its own risk at the stop (a half-size trade's loss is
    half a full one's); trades without a risk are left out."""
    return [t.pnl / t.risk for t in trades if t.risk > 0]


@dataclass(frozen=True)
class Forward:
    """A rule's record since the rules were registered: closed trades, and any open one."""

    key: str
    trades: int  # closed
    wins: int
    total: float  # the closed trades' result
    open_result: float | None  # an open trade, marked at the last close
    as_bad: float | None  # how often its history did this badly over as many trades

    @property
    def unusual(self) -> bool:
        return self.as_bad is not None and self.as_bad < UNUSUAL


def forward(key: str, e: autotrader.Expectation, history: Sequence[float]) -> Forward:
    """``e``: the rule replayed from the registration; ``history``: its trades closed before it,
    in risk units (``in_risk``)."""
    closed = [t for t in e.trades if t.how != "still open"]
    still = [t for t in e.trades if t.how == "still open"]
    return Forward(
        key,
        len(closed),
        sum(1 for t in closed if t.pnl > 0),
        sum(t.pnl for t in closed),
        sum(t.pnl for t in still) if still else None,
        as_bad(history, sum(in_risk(closed)), len(in_risk(closed))),
    )


def _record(b: autotrader.Bench, spec: Spec, whole: autotrader.Expectation) -> Forward:
    """A rule's forward record from the registration, against its trades closed before then in
    ``whole`` (its replay without the stop for good)."""
    since = mandates.RULES_REGISTERED[mandates.DAILY]
    before = [t for t in whole.trades if t.how != "still open" and t.closed < since]
    return forward(spec.key, b.run(spec, since=since), in_risk(before))


@dataclass
class Edge:
    since: datetime  # where the forward record starts: the registration (or the first day after
    # the rule's warm-up, when the prices stored begin later)
    nearby: dict[str, Nearby]  # by rule; none for a rule with no single number to vary
    switching: Switching | None
    forward: list[Forward]
    names: dict[str, str]  # each rule's name


_views: OrderedDict[tuple[Any, ...], Edge] = OrderedDict()


def _view_key(b: autotrader.Bench, rules: Sequence[str], now: datetime) -> tuple[Any, ...]:
    """What the views depend on: the prices (to their last bar), costs, limits and the day."""
    markets = tuple(
        (v.key, len(v.bars), v.bars.dates[-1], v.fee, v.half_spread, v.swap, v.market_stop)
        for v in b.venues
    )
    return (markets, b.limits, b.start.date(), b.until, tuple(rules), now.date())


def build(
    b: autotrader.Bench,
    rules: Sequence[str],
    now: datetime,
    own: Mapping[str, autotrader.Expectation] | None = None,
) -> Edge:
    """The three views of ``rules`` on bench ``b``, kept for the day. ``own``: each rule's replay
    already made with the bench's limits (reused). CPU only: run it off the event loop."""
    key = _view_key(b, rules, now)
    if key in _views:
        _views.move_to_end(key)
        return _views[key]
    own = own or {}
    nearby: dict[str, Nearby] = {}
    unstopped: dict[str, autotrader.Expectation] = {}
    ahead: list[Forward] = []
    for k in rules:
        spec = BY_KEY[k]
        mine = own.get(k) or b.run(spec)
        results = [
            _total(mine) if p == spec.params else _total(b.run(replace(spec, params=p)))
            for p in nearby_settings(spec)
        ]
        if results:
            nearby[k] = Nearby(
                len(results), sum(1 for x in results if x > 0), min(results), max(results)
            )
        # without the stop for good: a stop would cut the years after it short
        # (the same replay when the stop never switched it off)
        stopped = mine.summary is not None and mine.summary.stopped_at is not None
        unstopped[k] = b.run(spec, total_stop=None) if stopped else mine
        ahead.append(_record(b, spec, unstopped[k]))
    names = {k: mandates.AUTO_RULES.get(k, BY_KEY[k].name) for k in rules}
    since = b.start_for(mandates.RULES_REGISTERED[mandates.DAILY])
    view = Edge(since, nearby, switching(b, rules, now, unstopped), ahead, names)
    _views[key] = view
    while len(_views) > KEEP_VIEWS:
        _views.popitem(last=False)
    return view


def forward_records(b: autotrader.Bench, rules: Sequence[str]) -> list[Forward]:
    """The forward record of each of ``rules`` since the daily rules were registered (the rule
    watch, S48). CPU only: run it off the event loop."""
    return [_record(b, BY_KEY[k], b.run(BY_KEY[k], total_stop=None)) for k in rules]
