"""Broker adapter contract (V6 §1814). A live adapter must pass the contract test suite
against its venue sandbox before it may be registered."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class BrokerAmbiguousError(Exception):
    """The request may or may not have reached the venue (timeout, lost response).
    Callers must mark the order UNKNOWN_ORDER_STATE and reconcile — never resubmit."""


class BrokerUnavailableError(Exception):
    """The request was definitively not accepted (e.g. connection refused before sending)."""


class BrokerOrderNotFoundError(Exception):
    """The venue states it has no order with this client order ID."""


class VenueOrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class SubmitRequest:
    client_order_id: str
    instrument_key: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal | None
    time_in_force: str
    expires_at: datetime | None
    # An entry's stop-loss price, sent with it where the venue holds the stop from the fill
    # (AdapterCapabilities.attached_stop); None everywhere else.
    attached_stop: Decimal | None = None


@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    venue_order_id: str | None
    reject_reason: str | None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VenueOrder:
    client_order_id: str
    venue_order_id: str
    instrument_key: str
    side: str
    status: VenueOrderStatus
    quantity: Decimal
    filled_qty: Decimal
    limit_price: Decimal
    reject_reason: str | None


@dataclass(frozen=True)
class VenueFill:
    venue_fill_id: str
    client_order_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    liquidity: str
    filled_at: datetime


@dataclass(frozen=True)
class VenueBalance:
    asset: str
    total: Decimal
    available: Decimal


@dataclass(frozen=True)
class AdapterCapabilities:
    name: str
    is_live: bool
    is_simulated: bool
    order_types: tuple[str, ...]
    time_in_force: tuple[str, ...]
    idempotent_client_ids: bool
    bracket_or_oco: bool
    partial_fills: bool
    data_freshness: str
    precision_source: str
    rate_limits: str
    fees: str
    venue_status: str
    reconciliation: str
    attached_stop: bool = False  # an entry can carry its stop-loss (SubmitRequest.attached_stop)
    # The venue keeps one stop-loss per position: every stop order of an entry sets the same
    # stop, and cancelling any one of them clears it for all (HFM via MT5).
    stops_per_position: bool = False


class BrokerAdapter(Protocol):
    def capabilities(self) -> AdapterCapabilities: ...

    async def submit_order(self, account_ref: uuid.UUID, req: SubmitRequest) -> SubmitResult: ...

    async def get_order(self, account_ref: uuid.UUID, client_order_id: str) -> VenueOrder: ...

    async def cancel_order(self, account_ref: uuid.UUID, client_order_id: str) -> None: ...

    async def list_open_orders(self, account_ref: uuid.UUID) -> list[VenueOrder]: ...

    async def get_fills(
        self, account_ref: uuid.UUID, since: datetime | None
    ) -> list[VenueFill]: ...

    async def get_balances(self, account_ref: uuid.UUID) -> list[VenueBalance]: ...

    async def venue_status(self) -> str: ...
