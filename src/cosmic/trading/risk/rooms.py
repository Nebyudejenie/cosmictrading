"""How big a new buy may be before a risk check would refuse it (the $100 Gold Plan, S36).

The auto-trader sizes within these rooms, so a proposal is not sized and then refused by:
- R05 (the largest order);
- R06 (the largest position, and the share of equity in one market);
- R07 (the gross exposure limit, and the free cash with fees);
- R19 (the loss if every stop is hit, as a share of equity).

Each room uses the check's own arithmetic at the proposal's limit price. The checks still run
on the proposal; this only avoids sizing a trade that they would refuse. A room that cannot be
computed (an unknown position, a missing mark) is left out, and the check itself fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cosmic.core.money import BPS, ZERO
from cosmic.trading.policy import RiskPolicyParams
from cosmic.trading.risk.context import AccountState

HUNDRED = Decimal(100)


def open_risk(a: AccountState) -> Decimal | None:
    """R19's loss if every stop is hit, without the new trade: stopped quantity down to its
    stop-limit, unstopped quantity at its full value, resting entries at their own stops (full
    value without one). None when positions, orders or a mark are unknown."""
    if a.positions is None or a.open_orders is None:
        return None
    risk = ZERO
    for pos in a.positions:
        if pos.mark_price is None:
            return None
        stops = [
            o
            for o in a.open_orders
            if o.instrument_key == pos.instrument_key and o.purpose == "STOP_LOSS"
        ]
        covered = min(pos.quantity, sum((o.remaining_quantity for o in stops), ZERO))
        floor = min((o.limit_price for o in stops), default=ZERO)
        risk += covered * max(pos.mark_price - floor, ZERO)
        risk += (pos.quantity - covered) * pos.mark_price
    for o in a.open_orders:
        if o.purpose == "ENTRY" and o.side == "BUY" and not o.reduce_only:
            floor = o.stop_limit_price if o.stop_limit_price is not None else ZERO
            risk += o.remaining_quantity * max(o.limit_price - floor, ZERO)
    return risk


@dataclass(frozen=True)
class Room:
    notional: Decimal | None  # the most the new buy may be worth; None: no room is known
    binding: str  # what sets ``notional``, in the owner's words
    loss: Decimal | None  # the most it may lose at its stop-limit before fees (R19)


def buy_room(
    a: AccountState,
    p: RiskPolicyParams,
    *,
    instrument_key: str,
    venue_key: str,
    limit_price: Decimal,
) -> Room:
    """The rooms R05, R06, R07 and R19 leave for a new buy of ``instrument_key`` at
    ``limit_price``; the smallest value room binds."""
    rooms: dict[str, Decimal] = {"the risk policy's largest order": p.max_order_notional}
    if a.positions is not None:
        held = sum(
            (pos.quantity for pos in a.positions if pos.instrument_key == instrument_key), ZERO
        )
        rooms["the risk policy's largest position"] = p.max_position_notional - held * limit_price
        if a.equity is not None and a.equity > ZERO:
            rooms[f"the risk policy's {p.max_concentration_pct} % of equity in one market"] = (
                a.equity * p.max_concentration_pct / HUNDRED - held * limit_price
            )
        marks = all(pos.mark_price is not None for pos in a.positions if pos.quantity != ZERO)
        if a.open_orders is not None and marks:
            gross = sum(
                (
                    abs(pos.quantity * pos.mark_price)
                    for pos in a.positions
                    if pos.quantity != ZERO and pos.mark_price is not None
                ),
                ZERO,
            )
            reserved = sum(
                (
                    o.remaining_quantity * o.limit_price
                    for o in a.open_orders
                    if not o.reduce_only and o.side == "BUY"
                ),
                ZERO,
            )
            rooms["the risk policy's gross exposure limit"] = (
                p.max_gross_exposure - gross - reserved
            )
            if a.cash_available is not None:
                rooms["the free cash, fees included"] = (a.cash_available - reserved) / (
                    1 + p.fee_bps_for(venue_key) / BPS
                )
    binding = min(rooms, key=lambda k: rooms[k])
    loss = None
    risk = open_risk(a)
    if risk is not None and a.equity is not None and a.equity > ZERO:
        loss = max(a.equity * p.max_open_risk_pct / HUNDRED - risk, ZERO)
    return Room(max(rooms[binding], ZERO), binding, loss)
