"""Position sizing with strict caps and uncertainty haircut (QNT-07)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cosmic.core.money import ZERO, quantize_step

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class SizeResult:
    quantity: Decimal
    binding_constraint: str
    notes: tuple[str, ...]


def size_long(
    *,
    equity: Decimal,
    entry: Decimal,
    stop: Decimal,
    risk_per_trade_pct: Decimal,
    uncertainty_haircut_pct: Decimal,
    lot_size: Decimal,
    min_qty: Decimal | None,
    min_notional: Decimal | None,
    max_order_notional: Decimal,
    position_headroom_notional: Decimal,
    cash_available: Decimal,
    fee_bps: Decimal,
) -> SizeResult:
    """Largest long quantity consistent with risk budget and every cap, rounded down.

    Risk budget: equity × risk% / (entry − stop). The stop distance is an estimate;
    gaps and slippage can make actual losses larger (stated in every packet).
    """
    notes: list[str] = []
    if entry <= ZERO or stop <= ZERO or stop >= entry:
        return SizeResult(
            ZERO, "invalid_stop", ("stop must be positive and below entry for a long",)
        )
    if equity <= ZERO:
        return SizeResult(ZERO, "no_equity", ("equity is zero or unknown",))
    candidates = {
        "risk_budget": equity * risk_per_trade_pct / HUNDRED / (entry - stop),
        "max_order_notional": max_order_notional / entry,
        "position_headroom": max(position_headroom_notional, ZERO) / entry,
        "cash_available": max(cash_available, ZERO) / (entry * (1 + fee_bps / Decimal(10_000))),
    }
    binding = min(candidates, key=lambda k: candidates[k])
    raw = candidates[binding] * (HUNDRED - uncertainty_haircut_pct) / HUNDRED
    qty = quantize_step(raw, lot_size, "down") if raw > ZERO else ZERO
    notes.append(f"uncertainty haircut {uncertainty_haircut_pct}% applied")
    if min_qty is not None and qty < min_qty:
        return SizeResult(ZERO, binding, (*notes, f"below venue minimum quantity {min_qty}"))
    if min_notional is not None and qty * entry < min_notional:
        return SizeResult(ZERO, binding, (*notes, f"below venue minimum notional {min_notional}"))
    return SizeResult(qty, binding, tuple(notes))
