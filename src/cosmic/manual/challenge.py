"""The owner's $100 → $25,000 plan (S117): their PDF "Risk management plan challenge 100$ -
25000$" (2026-11-23) is the source of truth for these rules and figures.

The PDF: risk at most 10 % of the current balance on every trade, compounding; two trades a day,
Monday to Friday; a reward of 1.5, 2 or 3 times the risk; the win rates it assumes (55 %, 50 %,
45 %) are assumptions, not predictions; break-even = 1 ÷ (1 + reward); "blow-up" = the balance
below $1. Its own warnings are kept with its figures: 10 % is high risk, costs are left out, and
the expected path is not the typical one.

Exact figures (the roadmap, the stage, the streak table) are Decimal and reproduce the PDF's to
the cent (``tests/unit/test_manual_challenge.py``). The simulation's statistics are floats and
always labelled SIMULATED; they never become a money figure of the account.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from cosmic.manual import book

RISK = Decimal("0.10")  # of the current balance, on every trade: the PDF's maximum
TRADES_A_DAY = 2  # Monday to Friday
START = Decimal(100)
TARGET = Decimal(25_000)
FLOOR = Decimal(1)  # the PDF's "blow-up": the balance below $1
UNUSABLE = Decimal(10)  # the PDF: "below roughly $10 you can no longer size trades properly"
CENT = Decimal("0.01")
STREAKS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20)  # the rows of the PDF's streak table
MEASURED_FROM = 20  # closed trades before the owner's own win rate is shown beside the PDF's
PDF = "your PDF plan ($100 → $25,000)"


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Scenario:
    """One of the PDF's three plans, with the figures it publishes."""

    rr: Decimal  # the reward as a multiple of the risk
    name: str
    assumed_win: Decimal  # the PDF's assumption, never a prediction
    roadmap_trades: int  # trades to $25,000 on the expected path
    # The PDF's own 20,000-run simulation (page 5): independent trades, no costs.
    typical_trades: int
    fast_trades: int  # 10th percentile
    slow_trades: int  # 90th percentile
    within_roadmap: Decimal  # the runs reaching $25,000 within the roadmap's trades
    median_worst_drawdown: Decimal
    half_drawdown_share: Decimal  # the runs that fall 50 % or more at some point

    @property
    def label(self) -> str:
        return f"1:{self.rr.normalize():f} {self.name}"

    @property
    def breakeven(self) -> Decimal:
        """The win rate that breaks even before costs: 1 ÷ (1 + reward)."""
        return 1 / (1 + self.rr)

    @property
    def win(self) -> Decimal:
        return self.rr * RISK  # of the balance, per winning trade

    @property
    def ev(self) -> Decimal:
        """The expected result a trade, of the balance, at the PDF's assumed win rate."""
        return self.assumed_win * self.win - (1 - self.assumed_win) * RISK


SCENARIOS = {
    s.rr: s
    for s in (
        Scenario(
            Decimal("1.5"),
            "Conservative",
            Decimal("0.55"),
            150,
            182,
            124,
            264,
            Decimal("0.27"),
            Decimal("-0.54"),
            Decimal("0.63"),
        ),
        Scenario(
            Decimal(2),
            "Balanced",
            Decimal("0.50"),
            114,
            139,
            94,
            204,
            Decimal("0.27"),
            Decimal("-0.54"),
            Decimal("0.60"),
        ),
        Scenario(
            Decimal(3),
            "Aggressive",
            Decimal("0.45"),
            72,
            89,
            61,
            132,
            Decimal("0.25"),
            Decimal("-0.52"),
            Decimal("0.52"),
        ),
    )
}
DEFAULT_RR = Decimal(2)  # the owner's choice of 2026-11-23: 1:2 Balanced


def scenario(rr: Decimal | None) -> Scenario:
    return SCENARIOS.get(rr if rr is not None else DEFAULT_RR, SCENARIOS[DEFAULT_RR])


def expected_balance(start: Decimal, trades: int, s: Scenario) -> Decimal:
    """The PDF's roadmap: every trade moves the balance by the plan's expected value."""
    return cents(start * (1 + s.ev) ** trades)


def trades_to(balance: Decimal, s: Scenario, target: Decimal = TARGET) -> int:
    """Trades on the expected path from ``balance`` to the target (0 once reached)."""
    if balance <= 0 or s.ev <= 0:
        raise ValueError("no expected path from here")
    n, b = 0, balance
    while b < target:
        b *= 1 + s.ev
        n += 1
    return n


def blow_up_in(balance: Decimal) -> int:
    """Losses in a row, at 10 % each, that take the balance below $1."""
    n, b = 0, balance
    while b >= FLOOR:
        b *= 1 - RISK
        n += 1
    return n


def trading_days(trades: int) -> int:
    return -(-trades // TRADES_A_DAY)


@dataclass(frozen=True)
class NextTrade:
    base: Decimal  # the balance it is sized on
    risk: Decimal  # at most this is lost at the stop
    win: Decimal  # gained at the target
    blow_up_in: int
    to_target: int | None  # trades on the PDF's expected path
    days_to_target: int | None


def next_trade(base: Decimal, s: Scenario, target: Decimal = TARGET) -> NextTrade:
    """The next trade at the plan's 10 %: what it risks and what it can win."""
    risk = cents(base * RISK)
    to = trades_to(base, s, target) if base > 0 else None
    return NextTrade(
        base=base,
        risk=risk,
        win=cents(risk * s.rr),
        blow_up_in=blow_up_in(base) if base > 0 else 0,
        to_target=to,
        days_to_target=None if to is None else trading_days(to),
    )


@dataclass(frozen=True)
class StreakRow:
    losses: int
    left: Decimal  # of the balance
    drawdown: Decimal
    to_recover: Decimal  # the gain that brings it back
    becomes: Decimal  # this balance after the streak
    chance: Decimal  # that such a streak starts on a given trade, at the assumed win rate


def streaks(base: Decimal, s: Scenario) -> list[StreakRow]:
    """The PDF's losing-streak table for this balance and plan."""
    out = []
    for k in STREAKS:
        left = (1 - RISK) ** k
        out.append(
            StreakRow(
                losses=k,
                left=left,
                drawdown=left - 1,
                to_recover=1 / left - 1,
                becomes=cents(base * left),
                chance=(1 - s.assumed_win) ** k,
            )
        )
    return out


@dataclass(frozen=True)
class Progress:
    """The owner's own plan so far: their closed trades since they started it."""

    started_at: datetime
    start: Decimal | None  # the balance at the start
    start_derived: bool  # worked out from the balance less the trades since (none recorded)
    trades: int
    wins: int
    balance: Decimal | None
    expected: Decimal | None  # where the PDF's roadmap is after as many trades
    peak: Decimal | None
    drawdown: Decimal | None  # from the best closed balance, as a share (0 or less)
    losses_in_a_row: int
    longest_losing: int

    @property
    def ahead(self) -> Decimal | None:
        if self.balance is None or self.expected is None:
            return None
        return self.balance - self.expected

    @property
    def win_rate(self) -> Decimal | None:
        return Decimal(self.wins) / self.trades if self.trades else None


def progress(
    trades: Sequence[book.Trade],
    *,
    started_at: datetime,
    start: Decimal | None,
    balance: Decimal | None,
    s: Scenario,
) -> Progress:
    """Trade by trade since the start, against the PDF's roadmap. Deposits and withdrawals are
    not reported by the terminal, so a start worked out from the balance assumes none."""
    since = [t for t in trades if t.closed_at >= started_at]
    derived = start is None
    if start is None and balance is not None:
        start = balance - sum((t.net for t in since), Decimal(0))
    level = peak = start
    run = longest = 0
    for t in since:
        run = run + 1 if t.net < 0 else 0
        longest = max(longest, run)
        if level is not None and peak is not None:
            level += t.net
            peak = max(peak, level)
    now_dd = None if level is None or not peak or peak <= 0 else min(level / peak - 1, Decimal(0))
    return Progress(
        started_at=started_at,
        start=start,
        start_derived=derived,
        trades=len(since),
        wins=sum(1 for t in since if t.net > 0),
        balance=balance,
        expected=None if start is None else expected_balance(start, len(since), s),
        peak=peak,
        drawdown=now_dd,
        losses_in_a_row=run,
        longest_losing=longest,
    )


# ---- the simulation (SIMULATED: statistics of independent trades, no costs) ------------------


@dataclass(frozen=True)
class Simulated:
    """What many runs of the plan give at one win rate. Every figure is SIMULATED."""

    p: float
    rr: float
    runs: int
    cap: int  # trades a run at most
    reached: float  # the runs reaching the target within ``cap`` trades
    within_roadmap: float  # ... within the PDF's roadmap count
    typical_trades: int | None  # the median run (None: most runs never reach it)
    fast_trades: int | None  # 10th percentile
    slow_trades: int | None  # 90th percentile
    median_after_roadmap: float  # the median balance after the roadmap's count of trades
    median_worst_drawdown: float
    half_drawdown: float  # the runs that fall 50 % or more at some point
    unusable: float  # the runs that fall below $10 at some point


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    i = min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[i]


@lru_cache(maxsize=32)
def simulate(
    p: float,
    rr: float,
    *,
    roadmap: int,
    start: float = float(START),
    target: float = float(TARGET),
    runs: int = 1000,
    cap: int = 400,
    seed: int = 7,
) -> Simulated:
    """Runs of the plan at win rate ``p``: 10 % of the balance each trade, ``rr`` times it won.
    Fixed seed: the same figures each time. Floats: these are statistics, not money."""
    rng = random.Random(seed)  # noqa: S311 - a reproducible simulation  # nosec B311
    risk = float(RISK)
    up, down = 1 + rr * risk, 1 - risk
    times: list[float] = []
    after: list[float] = []
    worst: list[float] = []
    halves = unusable = 0
    for _ in range(runs):
        b = peak = start
        dd = 0.0
        low = False
        reached_at: int | None = None
        at_roadmap: float | None = None
        for i in range(1, cap + 1):
            b *= up if rng.random() < p else down
            if b > peak:
                peak = b
            dd = min(dd, b / peak - 1)
            if b < float(UNUSABLE):
                low = True
            if i == roadmap:
                at_roadmap = b
            if b >= target:
                reached_at = i
                break
            if b < 0.01:  # nothing left to trade with
                break
        # A run that stopped before the roadmap's count (at the target, or with nothing left)
        # stays where it stopped.
        times.append(float(reached_at) if reached_at is not None else math.inf)
        after.append(b if at_roadmap is None else at_roadmap)
        worst.append(dd)
        halves += dd <= -0.5
        unusable += low
    times.sort()
    after.sort()
    worst.sort()

    def trades_at(q: float) -> int | None:
        v = _percentile(times, q)
        return None if math.isinf(v) else int(v)

    return Simulated(
        p=p,
        rr=rr,
        runs=runs,
        cap=cap,
        reached=sum(1 for t in times if not math.isinf(t)) / runs,
        within_roadmap=sum(1 for t in times if t <= roadmap) / runs,
        typical_trades=trades_at(0.5),
        fast_trades=trades_at(0.1),
        slow_trades=trades_at(0.9),
        median_after_roadmap=_percentile(after, 0.5),
        median_worst_drawdown=_percentile(worst, 0.5),
        half_drawdown=halves / runs,
        unusable=unusable / runs,
    )


def simulate_at(p: Decimal, s: Scenario) -> Simulated:
    """The plan simulated at a win rate (rounded to 0.1 %: one cached run each)."""
    return simulate(round(float(p), 3), float(s.rr), roadmap=s.roadmap_trades)
