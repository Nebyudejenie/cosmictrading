"""Decision value of spending free AI quota on a research narrative (AI-20).

The free quota is finite, so when several narratives wait for AI, the ones most likely to
lead to an executable, evidence-backed proposal go first. The rule is deterministic and
deliberately simple (lower number runs first; ties go to the mission expiring soonest):

    20  entry signal whose historical simulation was positive over ≥ 10 trades
    40  entry signal with too few historical trades to judge
    60  entry signal whose historical simulation was not positive
    90  entry signal that could not be sized (the proposal could not execute)

Historical simulation is not evidence of future profitability; it only orders the queue.
"""

from __future__ import annotations

from typing import Any

MIN_TRADES = 10


def narrative_priority(quant: dict[str, Any]) -> tuple[int, str]:
    if quant.get("sizing") is None:
        return 90, "could not be sized, so a proposal could not execute"
    bt = quant.get("backtest") or {}
    trades = int(bt.get("trades") or 0)
    total_return = bt.get("total_return")
    if trades < MIN_TRADES or total_return is None:
        return 40, f"too few historical trades to judge ({trades} < {MIN_TRADES})"
    if float(total_return) > 0:
        return 20, f"positive historical simulation over {trades} trades"
    return 60, f"historical simulation not positive over {trades} trades"
