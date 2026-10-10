"""Exact decimal arithmetic for prices, quantities and money (ADR-007).

Floats are rejected: converting a binary float silently introduces rounding
error into financial truth.
"""

from __future__ import annotations

import decimal
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal
from typing import Literal

CONTEXT = decimal.Context(prec=50, rounding=ROUND_HALF_EVEN, traps=[decimal.InvalidOperation])
decimal.setcontext(CONTEXT)

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10_000)

Rounding = Literal["down", "up", "half_even"]
_MODES = {"down": ROUND_DOWN, "up": ROUND_UP, "half_even": ROUND_HALF_EVEN}


def to_decimal(value: object) -> Decimal:
    """Convert str/int/Decimal to Decimal. Floats and bools are refused."""
    if isinstance(value, bool):
        raise TypeError("bool is not a numeric amount")
    if isinstance(value, float):
        raise TypeError("float is not allowed for financial values; pass a string")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except decimal.InvalidOperation as exc:
            raise ValueError(f"not a decimal number: {value!r}") from exc
    else:
        raise TypeError(f"unsupported numeric type: {type(value).__name__}")
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def quantize_step(value: Decimal, step: Decimal, rounding: Rounding) -> Decimal:
    """Round ``value`` to a multiple of ``step`` in the given direction."""
    if step <= ZERO:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=_MODES[rounding])
    return units * step


def is_multiple(value: Decimal, step: Decimal) -> bool:
    if step <= ZERO:
        raise ValueError("step must be positive")
    return (value % step) == ZERO


def bps_of(part: Decimal, whole: Decimal) -> Decimal:
    """``part / whole`` in basis points."""
    if whole == ZERO:
        raise ZeroDivisionError("whole is zero")
    return part / whole * BPS


def apply_bps(value: Decimal, bps: Decimal) -> Decimal:
    return value * (ONE + bps / BPS)


def canonical_str(value: Decimal) -> str:
    """Stable string form: no exponent, no trailing zeros (``1.50`` → ``1.5``)."""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(ONE))
    return format(normalized, "f")
