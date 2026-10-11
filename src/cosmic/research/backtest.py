"""Event-driven, cost-aware backtester with walk-forward evaluation (QNT-08/09/10, doc 07 §6).

Decisions use bars ≤ t only; orders fill at the NEXT bar's open adjusted for half spread
and slippage; fees on every fill; stop is checked before take-profit inside a bar
(conservative); gaps fill at the open. Results are historical simulations with assumed
costs and are not evidence of future profitability.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cosmic.market.data import Bar
from cosmic.market.indicators import max_drawdown
from cosmic.research.strategy import Strategy

DISCLAIMER = "Historical simulation with assumed costs. Not evidence of future profitability."


@dataclass(frozen=True)
class CostModel:
    fee_bps: float = 60.0  # ASSUMPTION (B-08)
    half_spread_bps: float = 1.0
    slippage_bps: float = 5.0

    def buy_price(self, open_: float) -> float:
        return open_ * (1 + (self.half_spread_bps + self.slippage_bps) / 10_000)

    def sell_price(self, price: float) -> float:
        return price * (1 - (self.half_spread_bps + self.slippage_bps) / 10_000)

    def fee(self, notional: float) -> float:
        return notional * self.fee_bps / 10_000


@dataclass(frozen=True)
class Trade:
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    quantity: float
    pnl: float
    fees: float
    exit_reason: str


@dataclass
class BacktestResult:
    bars: int
    start: str
    end: str
    trades: list[Trade]
    equity_curve: list[float]
    total_return: float
    max_drawdown: float
    sharpe_annualised: float | None
    win_rate: float | None
    exposure: float
    fees_paid: float
    assumptions: dict[str, Any]
    disclaimer: str = DISCLAIMER
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "bars": self.bars,
            "start": self.start,
            "end": self.end,
            "trades": len(self.trades),
            "total_return": round(self.total_return, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "sharpe_annualised": None
            if self.sharpe_annualised is None
            else round(self.sharpe_annualised, 4),
            "win_rate": None if self.win_rate is None else round(self.win_rate, 4),
            "exposure": round(self.exposure, 4),
            "fees_paid": round(self.fees_paid, 4),
            "assumptions": self.assumptions,
            "disclaimer": self.disclaimer,
            "notes": self.notes,
        }


def run_backtest(
    bars: Sequence[Bar],
    strategy: Strategy,
    *,
    costs: CostModel | None = None,
    initial_equity: float = 10_000.0,
    risk_fraction: float = 0.1,
    periods_per_year: float = 8760.0,
) -> BacktestResult:
    costs = costs or CostModel()
    if any(not b.is_complete for b in bars):
        raise ValueError("backtests accept completed bars only")
    cash, qty = initial_equity, 0.0
    entry_price = entry_fee = 0.0
    entry_time = ""
    stop = tp = None
    trades: list[Trade] = []
    equity: list[float] = []
    exposed = 0
    fees_total = 0.0
    pending: str | None = None
    pending_levels: tuple[float | None, float | None] = (None, None)
    warm = strategy.warmup()
    for i, bar in enumerate(bars):
        o, h, low, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
        t = bar.open_time.isoformat()
        # 1) execute the decision made at the previous bar's close, at this bar's open
        if pending == "BUY" and qty == 0.0:
            price = costs.buy_price(o)
            notional = cash * risk_fraction
            qty = notional / price
            entry_fee = costs.fee(notional)
            cash -= notional + entry_fee
            fees_total += entry_fee
            entry_price, entry_time = price, t
            stop, tp = pending_levels
        elif pending == "SELL" and qty > 0.0:
            cash, trade = _close(
                cash,
                qty,
                entry_price,
                entry_fee,
                entry_time,
                costs.sell_price(o),
                t,
                "signal_exit",
                costs,
            )
            fees_total += trade.fees - entry_fee
            trades.append(trade)
            qty = 0.0
        pending = None
        # 2) protective exits inside this bar (stop first: conservative)
        if qty > 0.0:
            if stop is not None and low <= stop:
                fill = costs.sell_price(min(o, stop))  # gap below the stop fills at the open
                cash, trade = _close(
                    cash, qty, entry_price, entry_fee, entry_time, fill, t, "stop", costs
                )
                fees_total += trade.fees - entry_fee
                trades.append(trade)
                qty = 0.0
            elif tp is not None and h >= tp:
                cash, trade = _close(
                    cash,
                    qty,
                    entry_price,
                    entry_fee,
                    entry_time,
                    costs.sell_price(max(o, tp)),
                    t,
                    "take_profit",
                    costs,
                )
                fees_total += trade.fees - entry_fee
                trades.append(trade)
                qty = 0.0
        if qty > 0.0:
            exposed += 1
        equity.append(cash + qty * c)
        # 3) decide at the close using bars[: i + 1] only
        if i + 1 >= warm and i < len(bars) - 1:
            sig = strategy.signal(bars[: i + 1])
            if sig.action == "ENTER_LONG" and qty == 0.0:
                pending, pending_levels = "BUY", (sig.stop, sig.take_profit)
            elif sig.action == "EXIT" and qty > 0.0:
                pending = "SELL"
    if qty > 0.0 and bars:
        last = bars[-1]
        cash, trade = _close(
            cash,
            qty,
            entry_price,
            entry_fee,
            entry_time,
            costs.sell_price(float(last.close)),
            last.open_time.isoformat(),
            "end_of_data",
            costs,
        )
        fees_total += trade.fees - entry_fee
        trades.append(trade)
        equity[-1] = cash
    rets = [math.log(equity[k] / equity[k - 1]) for k in range(1, len(equity)) if equity[k - 1] > 0]
    sharpe = None
    if len(rets) > 30:
        m = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = (m / sd * math.sqrt(periods_per_year)) if sd > 0 else None
    wins = [tr for tr in trades if tr.pnl > 0]
    notes = []
    if len(trades) < 30:
        notes.append(f"only {len(trades)} trades: statistically weak sample")
    return BacktestResult(
        bars=len(bars),
        start=bars[0].open_time.isoformat() if bars else "",
        end=bars[-1].open_time.isoformat() if bars else "",
        trades=trades,
        equity_curve=equity,
        total_return=(equity[-1] / initial_equity - 1) if equity else 0.0,
        max_drawdown=max_drawdown(equity).max_drawdown if equity else 0.0,
        sharpe_annualised=sharpe,
        win_rate=(len(wins) / len(trades)) if trades else None,
        exposure=exposed / len(bars) if bars else 0.0,
        fees_paid=fees_total,
        notes=notes,
        assumptions={
            "fee_bps": costs.fee_bps,
            "half_spread_bps": costs.half_spread_bps,
            "slippage_bps": costs.slippage_bps,
            "risk_fraction": risk_fraction,
            "fills": "next bar open; stop before take-profit within a bar",
            "basis": "ASSUMPTION",
        },
    )


def _close(
    cash: float,
    qty: float,
    entry_price: float,
    entry_fee: float,
    entry_time: str,
    price: float,
    t: str,
    reason: str,
    costs: CostModel,
) -> tuple[float, Trade]:
    notional = qty * price
    fee = costs.fee(notional)
    pnl = notional - fee - qty * entry_price - entry_fee
    return cash + notional - fee, Trade(
        entry_time, entry_price, t, price, qty, pnl, entry_fee + fee, reason
    )


@dataclass
class WalkForwardResult:
    windows: list[dict[str, Any]]
    trials: int
    out_of_sample_return: float | None
    notes: list[str]
    disclaimer: str = DISCLAIMER


def walk_forward(
    bars: Sequence[Bar],
    make: Callable[[dict[str, Any]], Strategy],
    grid: list[dict[str, Any]],
    *,
    train: int,
    test: int,
    costs: CostModel | None = None,
) -> WalkForwardResult:
    """Select params on each train window; report only the following test window (out of sample)."""
    notes: list[str] = []
    if len(bars) < train + test:
        return WalkForwardResult(
            [],
            0,
            None,
            [f"insufficient data for walk-forward: {len(bars)} bars < {train + test} required"],
        )
    windows = []
    compounded = 1.0
    start = 0
    while start + train + test <= len(bars):
        tr, te = bars[start : start + train], bars[start + train : start + train + test]
        scored = [
            (run_backtest(tr, make(g), costs=costs).total_return, i) for i, g in enumerate(grid)
        ]
        best_ret, best_i = max(scored)
        oos = run_backtest(te, make(grid[best_i]), costs=costs)
        compounded *= 1 + oos.total_return
        windows.append(
            {
                "train_from": tr[0].open_time.isoformat(),
                "test_from": te[0].open_time.isoformat(),
                "test_to": te[-1].open_time.isoformat(),
                "chosen": grid[best_i],
                "in_sample_return": round(best_ret, 6),
                "out_of_sample_return": round(oos.total_return, 6),
                "oos_trades": len(oos.trades),
            }
        )
        start += test
    trials = len(grid) * len(windows)
    notes.append(
        f"{trials} parameter evaluations: in-sample results are optimistic (multiple testing)"
    )
    return WalkForwardResult(windows, trials, compounded - 1, notes)
