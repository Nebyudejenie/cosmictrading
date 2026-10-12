from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from cosmic.trading.attribution import attribute

price = st.decimals(min_value=Decimal("1"), max_value=Decimal("200000"), places=2)
qty = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("10"), places=4)
fee = st.decimals(min_value=Decimal("0"), max_value=Decimal("100"), places=2)


@given(q=qty, d=price, a=price, x=price, ef=price, xf=price, f=fee)
def test_parts_always_add_up_to_the_realised_result(
    q: Decimal, d: Decimal, a: Decimal, x: Decimal, ef: Decimal, xf: Decimal, f: Decimal
) -> None:
    realized = q * (xf - ef) - f
    r = attribute(
        quantity=q,
        decision_mid=d,
        arrival_mid=a,
        exit_mid=x,
        entry_fill=ef,
        exit_fill=xf,
        fees=f,
        realized=realized,
    )
    assert r.complete and r.residual == 0


def test_missing_reference_is_unknown_not_estimated() -> None:
    r = attribute(
        quantity=Decimal(1),
        decision_mid=None,
        arrival_mid=Decimal(100),
        exit_mid=Decimal(110),
        entry_fill=Decimal(101),
        exit_fill=Decimal(109),
        fees=Decimal(1),
        realized=Decimal(7),
    )
    assert r.complete is False and r.residual is None
    assert r.market_move is None and r.delay is None  # both need the decision price
    assert r.entry_execution == Decimal(-1) and r.exit_execution == Decimal(-1)
    assert "cannot separate" in str(r.as_dict()["note"])
