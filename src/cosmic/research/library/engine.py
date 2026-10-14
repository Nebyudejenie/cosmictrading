"""Vectorised, cost-aware daily simulation for the strategy library.

Contract (the same for every strategy, so results are comparable):

- A strategy returns a target weight per bar in [0, 1] (long or flat — spot, no leverage,
  no shorting). Weight ``w[t]`` is decided at the CLOSE of bar t using bars 0..t only.
- It is executed at the OPEN of bar t+1. Holdings are units, so a partial weight drifts with
  the price until the next rebalance; a rebalance smaller than ``min_rebalance`` is skipped.
- Every traded amount pays the one-way cost (fee + half spread + slippage), all labelled
  ASSUMPTION until the venue's fee schedule is confirmed (B-08).
- Equity is marked at each close. Results are historical simulations, not evidence of
  future profitability.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from cosmic.research.library import stats

DISCLAIMER = (
    "Historical simulation with assumed costs, long or flat only. Not evidence of future "
    "profitability."
)


@dataclass(frozen=True)
class Costs:
    name: str
    fee_bps: float
    half_spread_bps: float
    slippage_bps: float
    basis: str = "ASSUMPTION"

    @property
    def one_way(self) -> float:
        return (self.fee_bps + self.half_spread_bps + self.slippage_bps) / 10_000

    def scaled(self, k: float) -> Costs:
        return Costs(
            f"{self.name}×{k:g}",
            self.fee_bps * k,
            self.half_spread_bps * k,
            self.slippage_bps * k,
            self.basis,
        )


# Cost scenarios. Binance spot standard tier 0.10 %; Coinbase retail taker 0.60 % — both
# ASSUMPTIONS until the owner confirms their own tier (B-08).
BINANCE_LIKE = Costs("binance_like", 10.0, 1.0, 5.0)
COINBASE_LIKE = Costs("coinbase_like", 60.0, 1.0, 5.0)


@dataclass(frozen=True)
class Market:
    """Daily OHLCV, oldest first, completed bars only."""

    key: str
    dates: tuple[date, ...]
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]
    volume: tuple[float, ...]

    def __len__(self) -> int:
        return len(self.close)

    def head(self, n: int) -> Market:
        return Market(
            self.key,
            self.dates[:n],
            self.open[:n],
            self.high[:n],
            self.low[:n],
            self.close[:n],
            self.volume[:n],
        )


@dataclass
class Simulation:
    equity: list[float]
    weights_held: list[float]  # weight at each close, after the open's trade
    round_trips: list[tuple[int, int, float]]  # (entry bar, exit bar, return incl. costs)
    turnover: float  # Σ|Δweight| traded
    costs_paid: float  # in equity units (initial equity = 1)


def simulate(
    m: Market, weights: Sequence[float | None], costs: Costs, *, min_rebalance: float = 0.05
) -> Simulation:
    if len(weights) != len(m):
        raise ValueError("one weight per bar is required")
    cash, units = 1.0, 0.0
    equity: list[float] = []
    held: list[float] = []
    trips: list[tuple[int, int, float]] = []
    entry_bar, entry_value = -1, 0.0
    turnover = paid = 0.0
    target = 0.0
    for t in range(len(m)):
        o, c = m.open[t], m.close[t]
        if t > 0:
            target = _clip(weights[t - 1])
            value = cash + units * o
            current = units * o / value if value > 0 else 0.0
            delta = target - current
            closing = target == 0.0 and units > 0
            opening = target > 0.0 and units == 0.0
            if value > 0 and (closing or opening or abs(delta) >= min_rebalance):
                if closing:
                    proceeds = units * o
                    cash += proceeds - proceeds * costs.one_way
                    paid += proceeds * costs.one_way
                    units = 0.0
                    trips.append((entry_bar, t, cash / entry_value - 1 if entry_value else 0.0))
                else:
                    if opening:
                        entry_bar, entry_value = t, value
                    bought, sold, cost = _trade(delta * value, cash, costs)
                    units += (bought - sold) / o
                    cash += sold - bought - cost
                    paid += cost
                turnover += abs(delta)
        value_close = cash + units * c
        equity.append(value_close)
        held.append(units * c / value_close if value_close > 0 else 0.0)
    if units > 0 and equity:
        trips.append((entry_bar, len(m) - 1, equity[-1] / entry_value - 1 if entry_value else 0.0))
    return Simulation(equity, held, trips, turnover, paid)


def _trade(amount: float, cash: float, costs: Costs) -> tuple[float, float, float]:
    """(bought, sold, cost) for a desired signed amount. A purchase is sized so that it and
    its cost fit in the cash available: the account is never levered, not even by fees."""
    if amount >= 0:
        bought = min(amount, max(cash, 0.0)) / (1 + costs.one_way)
        return bought, 0.0, bought * costs.one_way
    sold = -amount
    return 0.0, sold, sold * costs.one_way


def _clip(w: float | None) -> float:
    if w is None or math.isnan(w):
        return 0.0
    return min(1.0, max(0.0, float(w)))


@dataclass(frozen=True)
class Metrics:
    days: int
    total_return: float
    cagr: float
    vol: float
    sharpe: float  # annualised, risk-free rate taken as 0
    sortino: float
    max_drawdown: float
    calmar: float
    exposure: float  # average weight held
    trades: int  # completed or open round trips starting in the window
    win_rate: float | None
    turnover_per_year: float
    sr_period: float  # per-day Sharpe (for PSR/DSR)
    skew: float
    kurtosis: float

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "days": self.days,
            "total_return": round(self.total_return, 6),
            "cagr": round(self.cagr, 6),
            "vol": round(self.vol, 6),
            "sharpe": round(self.sharpe, 4),
            "sortino": round(self.sortino, 4),
            "max_drawdown": round(self.max_drawdown, 6),
            "calmar": round(self.calmar, 4),
            "exposure": round(self.exposure, 4),
            "trades": self.trades,
            "win_rate": None if self.win_rate is None else round(self.win_rate, 4),
            "turnover_per_year": round(self.turnover_per_year, 3),
        }


def window_metrics(
    sim: Simulation,
    start: int,
    end: int,
    periods_per_year: float = 365.0,
    weights: Sequence[float | None] | None = None,
) -> Metrics:
    """Metrics over bars [start, end) of one continuous simulation (positions carry over)."""
    eq = sim.equity[max(0, start - 1) : end]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    days = len(rets)
    total = eq[-1] / eq[0] - 1 if eq and eq[0] > 0 else 0.0
    years = days / periods_per_year if days else 0.0
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else -1.0
    mean, sd, skew, kurt = stats.moments(rets)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / days) if days else 0.0
    peak, mdd = -math.inf, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    trips = [tr for tr in sim.round_trips if start <= tr[0] < end]
    held = sim.weights_held[start:end]
    traded = 0.0
    if weights is not None:
        prev = _clip(weights[start - 1]) if start > 0 else 0.0
        for t in range(start, end):
            w = _clip(weights[t])
            traded += abs(w - prev)
            prev = w
    return Metrics(
        days=days,
        total_return=total,
        cagr=cagr,
        vol=sd * math.sqrt(periods_per_year),
        sharpe=(mean / sd * math.sqrt(periods_per_year)) if sd > 0 else 0.0,
        sortino=(mean / downside * math.sqrt(periods_per_year)) if downside > 0 else 0.0,
        max_drawdown=mdd,
        calmar=cagr / abs(mdd) if mdd < 0 else 0.0,
        exposure=sum(held) / len(held) if held else 0.0,
        trades=len(trips),
        win_rate=(sum(1 for tr in trips if tr[2] > 0) / len(trips)) if trips else None,
        turnover_per_year=traded / years if years > 0 else 0.0,
        sr_period=mean / sd if sd > 0 else 0.0,
        skew=skew,
        kurtosis=kurt,
    )


def daily_returns(sim: Simulation, start: int, end: int) -> list[float]:
    eq = sim.equity[max(0, start - 1) : end]
    return [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]


@dataclass
class Panel:
    """Two or more markets aligned on common dates (for rotation strategies)."""

    markets: dict[str, Market]
    dates: tuple[date, ...] = field(default_factory=tuple)


def align(markets: Sequence[Market]) -> Panel:
    common = set(markets[0].dates)
    for m in markets[1:]:
        common &= set(m.dates)
    keep = sorted(common)
    out: dict[str, Market] = {}
    for m in markets:
        idx = {d: i for i, d in enumerate(m.dates)}
        sel = [idx[d] for d in keep]
        out[m.key] = Market(
            m.key,
            tuple(keep),
            tuple(m.open[i] for i in sel),
            tuple(m.high[i] for i in sel),
            tuple(m.low[i] for i in sel),
            tuple(m.close[i] for i in sel),
            tuple(m.volume[i] for i in sel),
        )
    return Panel(out, tuple(keep))


def simulate_panel(
    p: Panel,
    weights: Mapping[str, Sequence[float | None]],
    costs: Costs,
    *,
    min_rebalance: float = 0.05,
) -> Simulation:
    """Multi-asset version: weights per asset, summing to at most 1 (rest is cash)."""
    keys = list(p.markets)
    n = len(p.dates)
    units = dict.fromkeys(keys, 0.0)
    cash = 1.0
    equity: list[float] = []
    held: list[float] = []
    trips: list[tuple[int, int, float]] = []
    turnover = paid = 0.0
    invested_since, invested_value = -1, 0.0
    for t in range(n):
        if t > 0:
            opens = {k: p.markets[k].open[t] for k in keys}
            value = cash + sum(units[k] * opens[k] for k in keys)
            targets = {k: _clip(weights[k][t - 1]) for k in keys}
            total = sum(targets.values())
            if total > 1:
                targets = {k: v / total for k, v in targets.items()}
            was_invested = any(u > 0 for u in units.values())
            orders: dict[str, float] = {}
            for k in keys:
                current = units[k] * opens[k] / value if value > 0 else 0.0
                delta = targets[k] - current
                closing = targets[k] == 0.0 and units[k] > 0
                opening = targets[k] > 0.0 and units[k] == 0.0
                if value > 0 and (closing or opening or abs(delta) >= min_rebalance):
                    orders[k] = -units[k] * opens[k] if closing else delta * value
                    turnover += abs(delta)
            # Sales first, so purchases are funded by cash that exists.
            for k in sorted(orders, key=lambda key: orders[key]):
                if orders[k] < 0 and units[k] * opens[k] + orders[k] <= 1e-12:
                    sold, cost = units[k] * opens[k], units[k] * opens[k] * costs.one_way
                    units[k] = 0.0
                    cash += sold - cost
                    paid += cost
                    continue
                bought, sold, cost = _trade(orders[k], cash, costs)
                units[k] += (bought - sold) / opens[k]
                cash += sold - bought - cost
                paid += cost
            now_invested = any(u > 0 for u in units.values())
            if now_invested and not was_invested:
                invested_since, invested_value = t, value
            elif was_invested and not now_invested:
                trips.append(
                    (invested_since, t, cash / invested_value - 1 if invested_value else 0)
                )
        closes = {k: p.markets[k].close[t] for k in keys}
        v_close = cash + sum(units[k] * closes[k] for k in keys)
        equity.append(v_close)
        held.append(sum(units[k] * closes[k] for k in keys) / v_close if v_close > 0 else 0.0)
    if any(u > 0 for u in units.values()) and equity:
        trips.append(
            (invested_since, n - 1, equity[-1] / invested_value - 1 if invested_value else 0)
        )
    return Simulation(equity, held, trips, turnover, paid)
