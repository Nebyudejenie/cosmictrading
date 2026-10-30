"""Immutable inputs to the risk engine. Unknown values are ``None`` — never zero."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from cosmic.core.labels import DataLabel
from cosmic.trading.modes import Mode


class Stage(StrEnum):
    PRE_APPROVAL = "PRE_APPROVAL"
    PRE_SUBMISSION = "PRE_SUBMISSION"
    PROTECTIVE = "PROTECTIVE"


class Effect(StrEnum):
    OPEN = "OPEN"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


EXPOSURE_INCREASING = frozenset({Effect.OPEN, Effect.INCREASE})


@dataclass(frozen=True)
class OrderRequest:
    account_alias: str
    instrument_key: str
    side: str  # BUY | SELL
    effect: Effect
    quantity: Decimal
    order_type: str
    limit_price: Decimal
    time_in_force: str
    price_band_low: Decimal
    price_band_high: Decimal
    max_slippage_bps: Decimal
    # An approved exit names the entries it closes; their protective orders are replaced by it.
    closes: tuple[str, ...] = ()
    # The approved plan's stop-limit for an entry (None: no stop); counted in the open risk.
    stop_limit_price: Decimal | None = None

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.limit_price

    @property
    def exposure_increasing(self) -> bool:
        return self.effect in EXPOSURE_INCREASING


@dataclass(frozen=True)
class PositionState:
    instrument_key: str
    quantity: Decimal
    mark_price: Decimal | None  # None if no fresh mark is available
    lot_size: Decimal | None = None  # below one lot a holding is dust, not a position


@dataclass(frozen=True)
class OpenOrderState:
    instrument_key: str
    side: str
    remaining_quantity: Decimal
    limit_price: Decimal
    reduce_only: bool
    purpose: str = "ENTRY"
    intent_id: str | None = None
    stop_limit_price: Decimal | None = None  # a resting entry's approved stop-limit


@dataclass(frozen=True)
class AccountState:
    account_id: str
    alias: str
    kind: str  # PAPER | SANDBOX | LIVE
    status: str
    venue_key: str
    reconciliation_status: str
    last_reconciled_at: datetime | None
    cash_available: Decimal | None
    positions: tuple[PositionState, ...] | None
    open_orders: tuple[OpenOrderState, ...] | None
    unknown_orders: int | None
    equity: Decimal | None
    high_water_mark: Decimal | None
    realized_pnl_today: Decimal | None
    unrealized_pnl: Decimal | None
    loss_streak: int | None
    active_intents_same_instrument_side: int = 0


@dataclass(frozen=True)
class InstrumentState:
    canonical_key: str
    venue_key: str
    quote_currency: str
    status: str
    tradable: bool
    tick_size: Decimal | None
    lot_size: Decimal | None
    min_qty: Decimal | None
    max_qty: Decimal | None
    min_notional: Decimal | None


@dataclass(frozen=True)
class MarketState:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    quote_ts: datetime | None
    quote_label: DataLabel
    deviation_bps: Decimal | None
    deviation_label: DataLabel
    venue_status: str
    venue_status_label: DataLabel

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class AuthorizationState:
    approval_status: str | None  # ACTIVE | CONSUMED | EXPIRED | REVOKED | SUPERSEDED | None
    approval_expires_at: datetime | None
    intent_expires_at: datetime | None
    packet_hash_matches: bool
    policy_version_matches: bool
    operator_can_approve: bool


@dataclass(frozen=True)
class LiveActivationState:
    """The owner's live activation covering a LIVE account now (ADR-034)."""

    cap: Decimal
    expires_at: datetime
    # The adapter's code as it runs now has a PASSED contract run (ADR-034): a release that
    # changed it stops real money until the contract is run again.
    contract_current: bool = False


@dataclass(frozen=True)
class RiskContext:
    now: datetime
    stage: Stage
    mode: Mode
    request: OrderRequest
    account: AccountState
    instrument: InstrumentState
    market: MarketState
    policy_version: int
    critical_incident_categories: frozenset[str] = field(default_factory=frozenset)
    authorization: AuthorizationState | None = None
    # For a LIVE account: its active live activation, or None (then R20 refuses).
    live_activation: LiveActivationState | None = None
