"""Research for one market (ADR-032): why a rule lost or won, and what simply holding did.

Built for the Gold page; any market works.

- ``diagnose`` takes a rule's replayed trades, once with costs and once without, and splits
  the result: what costs took, how trades ended (stop or the rule's exit), the trading
  session they were opened in, the daily trend at the time, and how long winners and losers
  were held. It writes the findings as plain sentences.
- ``accumulate`` replays buying a fixed amount at the first price of every month and
  holding: the benchmark any trading of the market has to beat.

Everything here explains the past and changes nothing the auto-trader does. A pattern seen in
one period (a session, a trend) is an observation, not a rule. A rule built on it must win the
rule competition (ADR-031), on unseen data and then forward, before the auto-trader may use
it. Floats: analytics only (ADR-007). Past results are no promise.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from cosmic.market.indicators import sma
from cosmic.research.library import expect
from cosmic.research.library.engine import Market

# Trading sessions by the UTC hour a trade was opened (gold trades around the clock, but
# London and New York carry most of the volume).
SESSIONS: tuple[tuple[str, int, int], ...] = (
    ("Asia", 0, 7),
    ("London", 7, 13),
    ("New York", 13, 21),
    ("Late", 21, 24),
)
TREND_DAYS = 50
CAUTION = (
    "These are observations from the past, not proven rules: a rule built on them must win "
    "the rule competition, on unseen data and then forward, before the auto-trader may use it."
)


def session_of(t: datetime) -> str:
    return next(name for name, start, end in SESSIONS if start <= t.hour < end)


@dataclass(frozen=True)
class Bucket:
    name: str
    trades: int = 0
    wins: int = 0
    total_r: float = 0.0

    @property
    def mean_r(self) -> float | None:
        return self.total_r / self.trades if self.trades else None

    @property
    def win_rate(self) -> int | None:
        return round(100 * self.wins / self.trades) if self.trades else None

    def add(self, r: float) -> Bucket:
        return Bucket(self.name, self.trades + 1, self.wins + (r > 0), self.total_r + r)


@dataclass(frozen=True)
class Diagnosis:
    trades: int
    net_r: float  # after costs
    gross_r: float  # the same rule without costs
    stops: Bucket  # trades the stop ended
    exits: Bucket  # trades the rule's exit ended (or still open at the end)
    sessions: list[Bucket]
    trend: list[Bucket]  # by the daily trend when the trade was opened
    hold_win_h: float | None
    hold_loss_h: float | None
    hold_pct: float | None  # holding the market over the same span, in %
    swap_r: float = 0.0  # the part of the costs that was overnight swap (≤ 0 when paid)
    findings: list[str] = field(default_factory=list)

    @property
    def cost_r(self) -> float:
        return self.gross_r - self.net_r


def _trend_at(daily: Market | None) -> tuple[list[datetime], list[str]]:
    """Each completed day's trend for the next day: up when its close was above its 50-day
    average. Causal: a trade opened during a day sees the day before."""
    if daily is None or len(daily) <= TREND_DAYS:
        return [], []
    avg = sma(daily.close, TREND_DAYS)
    times: list[datetime] = []
    states: list[str] = []
    for d, c, a in zip(daily.dates, daily.close, avg, strict=True):
        if a is not None and isinstance(d, datetime):
            times.append(d)
            states.append("uptrend" if c > a else "downtrend")
    return times, states


def diagnose(
    net: expect.Result,
    gross: expect.Result,
    *,
    per_trade: float,
    daily: Market | None = None,
    hold_pct: float | None = None,
) -> Diagnosis:
    trades = sorted(net.trades, key=lambda t: t.closed)
    rs = [t.pnl / per_trade for t in trades]
    stops, exits = Bucket("stop-loss"), Bucket("the rule's exit")
    by_session = {name: Bucket(name) for name, _, _ in SESSIONS}
    by_trend = {name: Bucket(name) for name in ("uptrend", "downtrend")}
    day_times, day_states = _trend_at(daily)
    win_hours: list[float] = []
    loss_hours: list[float] = []
    for t, r in zip(trades, rs, strict=True):
        if t.how == "stop-loss":
            stops = stops.add(r)
        else:
            exits = exits.add(r)
        s = session_of(t.opened)
        by_session[s] = by_session[s].add(r)
        # the last day that closed before the trade opened (a daily bar closes a day later)
        k = bisect.bisect_right(day_times, t.opened) - 2
        if k >= 0:
            state = day_states[k]
            by_trend[state] = by_trend[state].add(r)
        hours = (t.closed - t.opened).total_seconds() / 3600
        (win_hours if r > 0 else loss_hours).append(hours)
    d = Diagnosis(
        trades=len(rs),
        net_r=sum(rs),
        gross_r=sum(t.pnl for t in gross.trades) / per_trade,
        stops=stops,
        exits=exits,
        sessions=list(by_session.values()),
        trend=[b for b in by_trend.values() if b.trades],
        hold_win_h=sum(win_hours) / len(win_hours) if win_hours else None,
        hold_loss_h=sum(loss_hours) / len(loss_hours) if loss_hours else None,
        hold_pct=hold_pct,
        swap_r=sum(t.swap for t in trades) / per_trade,
    )
    d.findings.extend(_findings(d))
    return d


def _findings(d: Diagnosis) -> list[str]:
    if not d.trades:
        return ["The rule made no trade in this period."]
    out = [
        f"Before costs the rule's {d.trades} trades made {d.gross_r:+.1f} R; costs took "
        f"{d.cost_r:.1f} R ({d.cost_r / d.trades:.2f} R a trade), leaving {d.net_r:+.1f} R."
        + (" Costs alone turned a gain into a loss." if d.gross_r > 0 >= d.net_r else "")
    ]
    if d.swap_r <= -0.05:
        out.append(
            f"Of those costs, the overnight swap took {-d.swap_r:.1f} R "
            f"({-d.swap_r / d.trades:.2f} R a trade)."
        )
    if d.stops.trades:
        out.append(
            f"{d.stops.trades} trades ended at the stop ({d.stops.mean_r:+.2f} R each on average); "
            f"{d.exits.trades} ended at the rule's exit ({(d.exits.mean_r or 0):+.2f} R each)."
        )
    active = [b for b in d.sessions if b.trades]
    if len(active) >= 2:
        best = max(active, key=lambda b: b.total_r)
        worst = min(active, key=lambda b: b.total_r)
        out.append(
            f"Trades opened in the {best.name} session made {best.total_r:+.1f} R "
            f"({best.trades} trades); in the {worst.name} session {worst.total_r:+.1f} R "
            f"({worst.trades} trades)."
        )
    if len(d.trend) == 2:
        up, down = d.trend
        out.append(
            f"Opened while the daily trend was up: {up.total_r:+.1f} R over {up.trades} trades; "
            f"while it was down: {down.total_r:+.1f} R over {down.trades}."
        )
    if d.hold_win_h is not None and d.hold_loss_h is not None:
        out.append(
            f"Winning trades were held {d.hold_win_h:.0f} hours on average, losing ones "
            f"{d.hold_loss_h:.0f} hours."
        )
    if d.hold_pct is not None:
        out.append(f"Simply holding over the same period: {d.hold_pct:+.1f} %.")
    return out


# ---- the benchmark: buy every month and hold -----------------------------------------------------


@dataclass(frozen=True)
class Accumulation:
    since: datetime
    months: int
    invested: float
    value: float
    worst_pct: float  # the deepest the value fell below the money put in so far, in % (≤ 0)
    curve: list[tuple[datetime, float, float]]  # (day, money put in so far, value)

    @property
    def gain_pct(self) -> float:
        return (self.value / self.invested - 1) * 100 if self.invested else 0.0


def accumulate(
    daily: Market, monthly: float, since: datetime, cost: float = 0.001
) -> Accumulation | None:
    """Buy ``monthly`` at the first daily open of each month from ``since`` (paying ``cost``,
    ASSUMPTION), hold, and value it at every daily close."""
    units = invested = 0.0
    worst = 0.0
    month: tuple[int, int] | None = None
    months = 0
    curve: list[tuple[datetime, float, float]] = []
    for d, o, c in zip(daily.dates, daily.open, daily.close, strict=True):
        if not isinstance(d, datetime) or d < since:
            continue
        if (d.year, d.month) != month:
            month = (d.year, d.month)
            units += monthly * (1 - cost) / o
            invested += monthly
            months += 1
        value = units * c
        worst = min(worst, (value / invested - 1) * 100)
        curve.append((d, invested, value))
    if not curve:
        return None
    return Accumulation(curve[0][0], months, invested, curve[-1][2], worst, curve)


def change_pct(closes: Sequence[float], bars_back: int) -> float | None:
    if len(closes) <= bars_back or closes[-1 - bars_back] <= 0:
        return None
    return (closes[-1] / closes[-1 - bars_back] - 1) * 100
