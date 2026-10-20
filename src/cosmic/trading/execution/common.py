"""Shared constants, outcome type and pure state mapping for the execution authority."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from cosmic.audit.service import Actor
from cosmic.core.logs import get_logger
from cosmic.core.money import ZERO
from cosmic.core.redaction import scrub_text
from cosmic.identity.capabilities import ServiceIdentity
from cosmic.trading import orders
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.base import (
    VenueOrder,
    VenueOrderStatus,
)
from cosmic.trading.modes import account_fields
from cosmic.trading.orders import TERMINAL, OrderStatus

log = get_logger("cosmic.trading.execution")

S = OrderStatus
EXECUTION_LOCK_KEY = 7201002
ACTOR = Actor.system(ServiceIdentity.EXECUTION_AUTHORITY.value)
NOT_FOUND_GRACE = timedelta(seconds=60)
ACTIVE_STATES = frozenset(OrderStatus) - TERMINAL
PROTECTIVE = frozenset({"STOP_LOSS", "TAKE_PROFIT"})
# Acknowledged by the venue: if it later has no record of the order, the order is gone.
GONE_IF_MISSING = frozenset({S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.CANCEL_PENDING})


class StepErrors:
    """One failing step, order or intent must not stop the rest of an execution cycle: the
    emergency stop's cancels and protective orders cannot wait on an unrelated error (a corrupt
    policy, one account's missing adapter). Each failure is logged and the first is raised at the
    end. Job health can miss that (a run that is not the leader returns normally), so the cycle
    also raises its own incident for a step failing three times in a row."""

    def __init__(self) -> None:
        self.first: Exception | None = None

    def note(self, what: str, exc: Exception, **ids: str) -> None:
        log.error(
            "execution_step_failed",
            what=what,
            error=type(exc).__name__,
            detail=scrub_text(str(exc))[:200],
            **ids,
        )
        if self.first is None:
            self.first = exc

    def raise_first(self) -> None:
        if self.first is not None:
            raise self.first


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


async def account_audit(conn: Any, account_id: uuid.UUID) -> dict[str, object]:
    """The account and its environment for an audit payload, so the audit trail says whose money
    an order or trade concerns and whether it is real (DIR-02)."""
    a = await get_account(conn, account_id=account_id)
    return account_fields(a.alias, a.kind, a.broker_adapter)
