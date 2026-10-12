"""Performance attribution for a closed long spot trade (QNT-11).

Realised P&L is split into parts that add up exactly (before rounding):

    market_move      qty × (exit_mid − decision_mid)    thesis and luck together
    delay            qty × (decision_mid − arrival_mid) proposal → approval → fill
    entry_execution  qty × (arrival_mid − entry_fill)   spread and slippage paid on entry
    exit_execution   qty × (exit_fill − exit_mid)       spread, slippage and gaps on exit
    fees             − fees

Thesis and luck cannot be told apart on one trade; judging them needs many trades. A part
whose reference price is missing is reported as None, never estimated.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cosmic.core.money import ZERO

THESIS_AND_LUCK_NOTE = (
    "market_move mixes thesis quality and luck; one trade cannot separate them — compare "
    "across at least 20 closed trades"
)


@dataclass(frozen=True)
class Attribution:
    quantity: Decimal
    market_move: Decimal | None
    delay: Decimal | None
    entry_execution: Decimal | None
    exit_execution: Decimal | None
    fees: Decimal
    realized: Decimal
    residual: Decimal | None  # realised minus the sum of the parts (0 when complete)
    complete: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "quantity": self.quantity,
            "market_move_thesis_and_luck": self.market_move,
            "delay_proposal_to_fill": self.delay,
            "entry_execution": self.entry_execution,
            "exit_execution": self.exit_execution,
            "fees": self.fees,
            "realized_pnl_after_fees": self.realized,
            "residual": self.residual,
            "complete": self.complete,
            "note": THESIS_AND_LUCK_NOTE,
        }


def attribute(
    *,
    quantity: Decimal,
    decision_mid: Decimal | None,
    arrival_mid: Decimal | None,
    exit_mid: Decimal | None,
    entry_fill: Decimal,
    exit_fill: Decimal,
    fees: Decimal,
    realized: Decimal,
) -> Attribution:
    def part(a: Decimal | None, b: Decimal | None) -> Decimal | None:
        return None if a is None or b is None else quantity * (a - b)

    market = part(exit_mid, decision_mid)
    delay = part(decision_mid, arrival_mid)
    entry = part(arrival_mid, entry_fill)
    exit_ = part(exit_fill, exit_mid)
    parts = (market, delay, entry, exit_)
    complete = all(p is not None for p in parts)
    residual = (
        realized - (sum((p for p in parts if p is not None), ZERO) - fees) if complete else None
    )
    return Attribution(quantity, market, delay, entry, exit_, -fees, realized, residual, complete)
