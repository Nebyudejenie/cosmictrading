from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cosmic.core.money import (
    apply_bps,
    bps_of,
    canonical_str,
    is_multiple,
    quantize_step,
    to_decimal,
)

steps = st.sampled_from(
    [Decimal("0.01"), Decimal("0.00000001"), Decimal("0.5"), Decimal("1"), Decimal("25")]
)
amounts = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1e9"),
    places=10,
    allow_nan=False,
    allow_infinity=False,
)


def test_to_decimal_rejects_float_and_bool() -> None:
    with pytest.raises(TypeError):
        to_decimal(0.1)
    with pytest.raises(TypeError):
        to_decimal(True)
    with pytest.raises(ValueError):
        to_decimal("abc")
    with pytest.raises(ValueError):
        to_decimal("NaN")
    assert to_decimal(" 83004.61 ") == Decimal("83004.61")
    assert to_decimal(3) == Decimal(3)


def test_quantize_examples() -> None:
    assert quantize_step(Decimal("0.123456789"), Decimal("0.00000001"), "down") == Decimal(
        "0.12345678"
    )
    assert quantize_step(Decimal("83004.615"), Decimal("0.01"), "down") == Decimal("83004.61")
    assert quantize_step(Decimal("83004.611"), Decimal("0.01"), "up") == Decimal("83004.62")
    with pytest.raises(ValueError):
        quantize_step(Decimal(1), Decimal(0), "down")


@given(amounts, steps)
def test_round_down_never_increases_and_is_on_grid(value: Decimal, step: Decimal) -> None:
    rounded = quantize_step(value, step, "down")
    assert rounded <= value
    assert value - rounded < step
    assert is_multiple(rounded, step)


@given(amounts, steps)
def test_round_up_never_decreases(value: Decimal, step: Decimal) -> None:
    rounded = quantize_step(value, step, "up")
    assert rounded >= value
    assert rounded - value < step
    assert is_multiple(rounded, step)


def test_bps_helpers() -> None:
    assert bps_of(Decimal("1"), Decimal("100")) == Decimal("100")
    assert apply_bps(Decimal("100"), Decimal("25")) == Decimal("100.25")


def test_canonical_str() -> None:
    assert canonical_str(Decimal("1.500")) == "1.5"
    assert canonical_str(Decimal("100")) == "100"
    assert canonical_str(Decimal("1E+2")) == "100"
    assert canonical_str(Decimal("0.00000001")) == "0.00000001"
