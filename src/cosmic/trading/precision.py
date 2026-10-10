"""Order precision against instrument rules (V6 §1821). Never silently changes exposure."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cosmic.core.money import BPS, ZERO, is_multiple, quantize_step


@dataclass(frozen=True)
class InstrumentRulesView:
    canonical_key: str
    tick_size: Decimal | None
    lot_size: Decimal | None
    min_qty: Decimal | None
    max_qty: Decimal | None
    min_notional: Decimal | None


@dataclass(frozen=True)
class Normalised:
    quantity: Decimal
    limit_price: Decimal
    quantity_change_bps: Decimal
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def normalise(
    rules: InstrumentRulesView,
    side: str,
    quantity: Decimal,
    limit_price: Decimal,
    tolerance_bps: Decimal,
) -> Normalised:
    """Round quantity down to the lot and the price conservatively to the tick.

    Buys round the limit price down (never pay more than requested), sells round up
    (never sell for less). A change beyond ``tolerance_bps`` or a result below venue
    minimums is reported as a problem; callers must re-propose, not proceed.
    """
    problems: list[str] = []
    if rules.tick_size is None or rules.lot_size is None:
        return Normalised(quantity, limit_price, ZERO, ("instrument tick/lot size unknown",))
    if quantity <= ZERO or limit_price <= ZERO:
        return Normalised(quantity, limit_price, ZERO, ("quantity and price must be positive",))
    q = quantize_step(quantity, rules.lot_size, "down")
    p = quantize_step(limit_price, rules.tick_size, "down" if side == "BUY" else "up")
    change = (quantity - q) / quantity * BPS
    if change > tolerance_bps:
        problems.append(
            f"rounding to lot size changes quantity by {change:.2f} bps (> {tolerance_bps} bps)"
        )
    if q <= ZERO:
        problems.append("quantity rounds to zero at this lot size")
    if rules.min_qty is not None and q < rules.min_qty:
        problems.append(f"quantity {q} below venue minimum {rules.min_qty}")
    if rules.max_qty is not None and q > rules.max_qty:
        problems.append(f"quantity {q} above venue maximum {rules.max_qty}")
    if rules.min_notional is not None and q * p < rules.min_notional:
        problems.append(f"notional {q * p} below venue minimum {rules.min_notional}")
    return Normalised(q, p, change, tuple(problems))


def is_on_grid(rules: InstrumentRulesView, quantity: Decimal, price: Decimal) -> bool:
    if rules.tick_size is None or rules.lot_size is None:
        return False
    return is_multiple(quantity, rules.lot_size) and is_multiple(price, rules.tick_size)
