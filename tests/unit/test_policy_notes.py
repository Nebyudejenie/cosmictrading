"""Where the account's risk policy stops a $100 auto-trader before its own limits (S36)."""

from __future__ import annotations

from decimal import Decimal

from cosmic.reporting.autotrader import losses_to_own_stop, policy_notes
from cosmic.trading.policy import DEFAULT_POLICY

D = Decimal


def test_at_100_and_bold_the_default_policy_binds_four_times() -> None:
    notes = policy_notes(DEFAULT_POLICY, D(100), D(100), D(2))
    text = " | ".join(notes)
    assert len(notes) == 4
    assert "One market may hold at most 50.00" in text
    assert "only 1 full trade at a time; 4 % would allow 2" in text
    assert "After 4 losing trades in a row" in text and "14 or more lets its own stop" in text
    assert "own stop for good (20.0 % of the account); 25 % lets" in text


def test_the_own_stop_needs_14_full_losses_in_a_row() -> None:
    """5 full losses, then half size: 9.5 trades' worth after 14, and one more would pass 10."""
    assert losses_to_own_stop() == 14


def test_a_large_account_with_a_small_plan_has_nothing_to_say() -> None:
    relaxed = DEFAULT_POLICY.model_copy(update={"max_loss_streak": 14})
    assert policy_notes(relaxed, D(100_000), D(5_000), D(50)) == []
    assert policy_notes(DEFAULT_POLICY, None, D(100), D(2)) == []
