"""Gate B's boundaries shown on both sides (S115): R08's drawdown, daily loss and losing streak
refuse exactly at their limit and never a cent before it, whatever the numbers."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from cosmic.trading.risk.checks import Status, loss_limits
from tests.risk_factory import POLICY, account

D = Decimal


@settings(max_examples=300, deadline=None)
@given(
    hwm=st.decimals(min_value=D(100), max_value=D(10_000_000), places=2),
    limit=st.decimals(min_value=D(1), max_value=D(50), places=2),
)
def test_the_drawdown_refuses_at_its_limit_and_not_a_cent_before(hwm: D, limit: D) -> None:
    policy = POLICY.model_copy(update={"max_drawdown_pct": limit, "max_daily_loss": D(10**12)})
    at = hwm * (1 - limit / 100)  # equity exactly at the limit
    over = at.quantize(D("0.01"))
    if (hwm - over) / hwm * 100 < limit:
        over -= D("0.01")  # the first cent at or beyond the limit
    under = over + D("0.01")
    assert loss_limits(account(equity=over, high_water_mark=hwm), policy).status is Status.FAIL
    if (hwm - under) / hwm * 100 < limit:
        assert loss_limits(account(equity=under, high_water_mark=hwm), policy).status is (
            Status.PASS
        )


@settings(max_examples=200, deadline=None)
@given(limit=st.decimals(min_value=D(1), max_value=D(100_000), places=2))
def test_the_daily_loss_refuses_at_its_limit_and_not_a_cent_before(limit: D) -> None:
    policy = POLICY.model_copy(update={"max_daily_loss": limit})
    big = {"equity": D(10**9), "high_water_mark": D(10**9)}
    at = account(realized_pnl_today=-limit, unrealized_pnl=D(0), **big)
    before = account(realized_pnl_today=-(limit - D("0.01")), unrealized_pnl=D(0), **big)
    split = account(realized_pnl_today=-(limit / 2), unrealized_pnl=-(limit / 2), **big)
    assert loss_limits(at, policy).status is Status.FAIL
    assert loss_limits(split, policy).status is Status.FAIL  # open losses count with closed
    assert loss_limits(before, policy).status is Status.PASS


@settings(max_examples=50, deadline=None)
@given(limit=st.integers(min_value=1, max_value=100))
def test_the_losing_streak_refuses_at_its_limit_and_not_one_before(limit: int) -> None:
    policy = POLICY.model_copy(update={"max_loss_streak": limit})
    assert loss_limits(account(loss_streak=limit), policy).status is Status.FAIL
    assert loss_limits(account(loss_streak=limit - 1), policy).status is Status.PASS
