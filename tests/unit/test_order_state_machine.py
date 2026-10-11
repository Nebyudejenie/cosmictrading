"""TRD-24: the order state machine (V6 §1812) as a table, checked exhaustively."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cosmic.core.errors import ConflictError
from cosmic.trading.execution import path_to
from cosmic.trading.orders import (
    BROKER_GROUNDED,
    TERMINAL,
    TRANSITIONS,
    OrderRow,
    Source,
    can_transition,
    transition,
)
from cosmic.trading.orders import OrderStatus as S


def test_every_state_is_in_the_table_and_terminal_states_are_final() -> None:
    assert set(TRANSITIONS) == set(S)
    for t in TERMINAL:
        assert TRANSITIONS[t] == frozenset(), t


def test_every_non_terminal_state_can_still_reach_a_terminal_state() -> None:
    for s in set(S) - TERMINAL:
        assert any(path_to(s, t) for t in TERMINAL), s


def test_no_shortcut_from_creation_to_broker_states() -> None:
    for target in BROKER_GROUNDED:
        assert not can_transition(S.CREATED, target), target
    assert not can_transition(S.AUTHORIZED, S.SUBMITTED)  # SUBMITTING is persisted first


def _order(status: S) -> OrderRow:
    now = datetime(2026, 10, 11, tzinfo=UTC)
    return OrderRow(
        id=uuid.uuid4(),
        intent_id=uuid.uuid4(),
        approval_id=None,
        parent_order_id=None,
        purpose="ENTRY",
        account_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        client_order_id="c",
        broker_order_id=None,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal(1),
        limit_price=Decimal(1),
        stop_price=None,
        time_in_force="GTC",
        expires_at=None,
        status=status,
        status_version=1,
        filled_qty=Decimal(0),
        avg_fill_price=None,
        fees_paid=Decimal(0),
        reduce_only=False,
        is_simulated=True,
        created_at=now,
        submitted_at=None,
        unknown_since=None,
    )


@pytest.mark.parametrize("target", sorted(BROKER_GROUNDED))
async def test_broker_grounded_states_need_broker_or_reconciliation_evidence(target: S) -> None:
    current = next(s for s in S if can_transition(s, target))
    with pytest.raises(ConflictError, match="requires broker or reconciliation evidence"):
        await transition(
            None,
            _order(current),
            target,
            source=Source.LOCAL,  # type: ignore[arg-type]
            now=datetime(2026, 10, 11, tzinfo=UTC),
        )


async def test_illegal_transition_refused() -> None:
    with pytest.raises(ConflictError, match="illegal order transition"):
        await transition(
            None,
            _order(S.FILLED),
            S.CANCELLED,
            source=Source.BROKER,  # type: ignore[arg-type]
            now=datetime(2026, 10, 11, tzinfo=UTC),
        )
