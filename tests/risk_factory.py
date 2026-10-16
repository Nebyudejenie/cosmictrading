"""A baseline RiskContext in which every check passes; tests mutate one aspect at a time."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from cosmic.core.labels import DataLabel
from cosmic.trading.modes import Mode
from cosmic.trading.policy import DEFAULT_POLICY
from cosmic.trading.risk.context import (
    AccountState,
    AuthorizationState,
    Effect,
    InstrumentState,
    MarketState,
    OpenOrderState,
    OrderRequest,
    PositionState,
    RiskContext,
    Stage,
)

D = Decimal
NOW = datetime(2026, 10, 10, 21, 6, tzinfo=UTC)
KEY = "SPOT:BTC/USD@COINBASE"
POLICY = DEFAULT_POLICY


def request(**kw: Any) -> OrderRequest:
    base: dict[str, Any] = dict(
        account_alias="paper-main",
        instrument_key=KEY,
        side="BUY",
        effect=Effect.OPEN,
        quantity=D("0.004"),
        order_type="LIMIT",
        limit_price=D("83000.00"),
        time_in_force="GTC",
        price_band_low=D("82500.00"),
        price_band_high=D("83500.00"),
        max_slippage_bps=D("10"),
        stop_limit_price=D("80900.00"),
    )
    base.update(kw)
    return OrderRequest(**base)


def account(**kw: Any) -> AccountState:
    base: dict[str, Any] = dict(
        account_id="a1",
        alias="paper-main",
        kind="PAPER",
        status="ACTIVE",
        venue_key="COINBASE",
        reconciliation_status="OK",
        last_reconciled_at=NOW - timedelta(minutes=2),
        cash_available=D("10000"),
        positions=(),
        open_orders=(),
        unknown_orders=0,
        equity=D("10000"),
        high_water_mark=D("10000"),
        realized_pnl_today=D("0"),
        unrealized_pnl=D("0"),
        loss_streak=0,
    )
    base.update(kw)
    return AccountState(**base)


def instrument(**kw: Any) -> InstrumentState:
    base: dict[str, Any] = dict(
        canonical_key=KEY,
        venue_key="COINBASE",
        quote_currency="USD",
        status="ONLINE",
        tradable=True,
        tick_size=D("0.01"),
        lot_size=D("0.00000001"),
        min_qty=D("0.00000001"),
        max_qty=None,
        min_notional=D("1"),
    )
    base.update(kw)
    return InstrumentState(**base)


def market(**kw: Any) -> MarketState:
    base: dict[str, Any] = dict(
        bid=D("82995.00"),
        ask=D("83000.00"),
        last=D("82998.00"),
        quote_ts=NOW - timedelta(seconds=20),
        quote_label=DataLabel.DELAYED,
        deviation_bps=D("5"),
        deviation_label=DataLabel.DELAYED,
        venue_status="OPERATIONAL",
        venue_status_label=DataLabel.DELAYED,
    )
    base.update(kw)
    return MarketState(**base)


def authorization(**kw: Any) -> AuthorizationState:
    base: dict[str, Any] = dict(
        approval_status="ACTIVE",
        approval_expires_at=NOW + timedelta(minutes=10),
        intent_expires_at=NOW + timedelta(minutes=50),
        packet_hash_matches=True,
        policy_version_matches=True,
        operator_can_approve=True,
    )
    base.update(kw)
    return AuthorizationState(**base)


def context(stage: Stage = Stage.PRE_APPROVAL, **kw: Any) -> RiskContext:
    base: dict[str, Any] = dict(
        now=NOW,
        stage=stage,
        mode=Mode.PAPER_TRADING,
        request=request(),
        account=account(),
        instrument=instrument(),
        market=market(),
        policy_version=1,
        authorization=authorization() if stage is Stage.PRE_SUBMISSION else None,
    )
    base.update(kw)
    return RiskContext(**base)


def held(qty: str, mark: str | None = "83000") -> tuple[PositionState, ...]:
    return (PositionState(KEY, D(qty), None if mark is None else D(mark)),)


def open_order(
    side: str = "BUY",
    qty: str = "0.001",
    price: str = "82000",
    reduce_only: bool = False,
    key: str = KEY,
) -> OpenOrderState:
    return OpenOrderState(key, side, D(qty), D(price), reduce_only)


__all__ = [
    "NOW",
    "POLICY",
    "account",
    "authorization",
    "context",
    "held",
    "instrument",
    "market",
    "open_order",
    "replace",
    "request",
]
