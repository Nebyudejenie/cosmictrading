"""Shared constants, outcome type and pure state mapping for the execution authority."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from cosmic.audit.service import Actor
from cosmic.core.logs import get_logger
from cosmic.core.money import ZERO
from cosmic.identity.capabilities import ServiceIdentity
from cosmic.trading import orders
from cosmic.trading.brokers.base import (
    VenueOrder,
    VenueOrderStatus,
)
from cosmic.trading.orders import TERMINAL, OrderStatus

log = get_logger("cosmic.trading.execution")

S = OrderStatus
EXECUTION_LOCK_KEY = 7201002
ACTOR = Actor.system(ServiceIdentity.EXECUTION_AUTHORITY.value)
NOT_FOUND_GRACE = timedelta(seconds=60)
ACTIVE_STATES = frozenset(OrderStatus) - TERMINAL
PROTECTIVE = frozenset({"STOP_LOSS", "TAKE_PROFIT"})


@dataclass(frozen=True)
class Outcome:
    code: str  # SKIPPED | BLOCKED | ACKNOWLEDGED | REJECTED | UNKNOWN | FAILED | RACE_LOST
    detail: str = ""
    order_id: uuid.UUID | None = None


def path_to(current: OrderStatus, target: OrderStatus) -> list[OrderStatus] | None:
    """Legal path from current to target (direct, or via UNKNOWN_ORDER_STATE)."""
    if current == target and current is not S.PARTIALLY_FILLED:
        return []
    if orders.can_transition(current, target):
        return [target]
    if orders.can_transition(current, S.UNKNOWN_ORDER_STATE) and orders.can_transition(
        S.UNKNOWN_ORDER_STATE, target
    ):
        return [S.UNKNOWN_ORDER_STATE, target]
    return None


def target_from_venue(
    current: OrderStatus, venue: VenueOrder, filled: Decimal, quantity: Decimal
) -> OrderStatus:
    """Map broker-confirmed venue state + recorded fills to the internal status."""
    if venue.status is VenueOrderStatus.FILLED:
        return S.FILLED if filled >= quantity else S.PARTIALLY_FILLED
    if venue.status is VenueOrderStatus.CANCELLED:
        return S.CANCELLED
    if venue.status is VenueOrderStatus.EXPIRED:
        return S.EXPIRED
    if venue.status is VenueOrderStatus.REJECTED:
        return S.REJECTED
    if current is S.CANCEL_PENDING:
        return S.CANCEL_PENDING  # still open at the venue: the cancel is not confirmed yet
    return S.PARTIALLY_FILLED if filled > ZERO else S.ACKNOWLEDGED
