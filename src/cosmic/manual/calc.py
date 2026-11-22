"""The trade calculator (S113): from the owner's entry, stop-loss, target and risk, the lot size
and everything it means, on HFM's own contract for this account.

Every figure uses the contract MT5 reports (contract size, tick size, tick value, volume
minimum, step and maximum): no broker's gold is assumed to be another's. The lot size is
rounded DOWN to HFM's volume step, so the risk is never more than asked; a trade whose smallest
size would risk more than allowed, or whose margin the account cannot pay, is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from cosmic.manual.live import BUY, SELL, Quote, Spec

OK, WARN, BLOCK = "OK", "WARN", "BLOCK"
HUNDRED = Decimal(100)
CENT = Decimal("0.01")
PENDING_GAP = Decimal("0.001")  # an entry this far from the price now is a pending order


@dataclass(frozen=True)
class Line:
    level: str  # OK, WARN or BLOCK
    text: str


@dataclass(frozen=True)
class Ticket:
    side: str
    entry: Decimal
    stop: Decimal
    target: Decimal | None
    risk_pct: Decimal | None = None  # of equity
    risk_amount: Decimal | None = None  # in the account's currency; wins over risk_pct
    note: str = ""


@dataclass(frozen=True)
class Sizing:
    lots: Decimal  # 0 when refused
    wanted: Decimal  # the money the risk setting allows at the stop
    risk_cash: Decimal  # what the lot size loses at the stop, costs included
    risk_pct: Decimal  # of equity
    reward_cash: Decimal | None
    rr: Decimal | None
    margin: Decimal | None  # an estimate in the account's currency
    spread_cost: Decimal | None
    per_lot_loss: Decimal
    distance: Decimal
    lines: tuple[Line, ...]

    @property
    def blocked(self) -> bool:
        return any(x.level == BLOCK for x in self.lines)


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """``value`` rounded down to a whole number of ``step``."""
    return (value / step).to_integral_value(ROUND_DOWN) * step


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def size(
    ticket: Ticket,
    *,
    equity: Decimal,
    margin_free: Decimal | None,
    leverage: int | None,
    currency: str | None,
    spec: Spec,
    quote: Quote | None,
    max_risk_pct: Decimal,
    commission_per_lot: Decimal = Decimal(0),
    atr: Decimal | None = None,
) -> Sizing:
    """The lot size and the trade's figures, with every reason to warn or refuse."""
    lines: list[Line] = []
    side, entry, stop, target = ticket.side, ticket.entry, ticket.stop, ticket.target
    zero = Decimal(0)

    def refused(*why: str) -> Sizing:
        return Sizing(
            zero,
            zero,
            zero,
            zero,
            None,
            None,
            None,
            None,
            zero,
            abs(entry - stop),
            tuple(lines) + tuple(Line(BLOCK, w) for w in why),
        )

    if side not in (BUY, SELL):
        return refused("choose BUY or SELL")
    if entry <= 0 or stop <= 0 or (target is not None and target <= 0):
        return refused("the entry, the stop-loss and the target must be prices above 0")
    if equity <= 0:
        return refused("the account has no equity to risk")
    if side == BUY and not stop < entry:
        return refused("a BUY's stop-loss must be below its entry")
    if side == SELL and not stop > entry:
        return refused("a SELL's stop-loss must be above its entry")
    if target is not None and side == BUY and not target > entry:
        return refused("a BUY's target must be above its entry")
    if target is not None and side == SELL and not target < entry:
        return refused("a SELL's target must be below its entry")
    distance = abs(entry - stop)
    if spec.stops_level and distance < spec.stops_level * spec.point:
        return refused(
            f"the stop-loss is {distance} from the entry; HFM allows no closer than "
            f"{spec.stops_level * spec.point} on this account"
        )
    if quote is not None:
        if distance < 2 * quote.spread:
            lines.append(
                Line(
                    WARN,
                    f"the stop-loss is within two spreads ({quote.spread}) of the entry: the "
                    "spread alone could close the trade",
                )
            )
        market = quote.ask if side == BUY else quote.bid
        gap = abs(entry - market)
        if gap > max(3 * quote.spread, market * PENDING_GAP):
            lines.append(
                Line(
                    WARN,
                    f"the entry is {gap} from the price now ({market}): in MT5 that is a pending "
                    "order (limit or stop), not a market order",
                )
            )
    if atr is not None and atr > 0:
        if distance < atr / 2:
            lines.append(
                Line(WARN, f"the stop-loss is tighter than half the usual 15-minute move ({atr})")
            )
        elif distance > 8 * atr:
            lines.append(
                Line(WARN, f"the stop-loss is wider than 8 times the usual 15-minute move ({atr})")
            )
    if distance > entry * Decimal("0.05"):
        lines.append(Line(WARN, "the stop-loss is more than 5 % of the price away: unusually wide"))
    # The money at risk the owner allows.
    if ticket.risk_amount is not None:
        wanted = ticket.risk_amount
    elif ticket.risk_pct is not None:
        wanted = equity * ticket.risk_pct / HUNDRED
    else:
        return refused("give the risk as a percentage of equity or as an amount")
    if wanted <= 0:
        return refused("the risk must be more than 0")
    wanted_pct = wanted / equity * HUNDRED
    if wanted_pct > max_risk_pct:
        return refused(
            f"{money(wanted)} is {wanted_pct.quantize(CENT)} % of equity: above your limit of "
            f"{max_risk_pct.normalize():f} % a trade"
        )
    vpp = spec.value_per_price
    per_lot_loss = distance * vpp + commission_per_lot
    raw = wanted / per_lot_loss
    lots = floor_to(raw, spec.volume_step)
    if lots < spec.volume_min:
        smallest = spec.volume_min * per_lot_loss
        return refused(
            f"HFM's smallest trade ({spec.volume_min.normalize():f} lot) would lose "
            f"{money(smallest)} at this stop-loss, {(smallest / equity * HUNDRED).quantize(CENT)} "
            f"% of equity: more than the {money(wanted)} you allow. A closer stop-loss or a "
            "larger risk (within your limit) is needed"
        )
    if lots > spec.volume_max:
        lots = floor_to(spec.volume_max, spec.volume_step)
        lines.append(Line(WARN, f"capped at HFM's largest trade, {lots.normalize():f} lot"))
    risk_cash = lots * per_lot_loss
    reward = rr = None
    if target is not None:
        reward = lots * (abs(target - entry) * vpp - commission_per_lot)
        rr = abs(target - entry) / distance
        if rr < 1:
            lines.append(Line(WARN, f"the reward is smaller than the risk (1:{rr.quantize(CENT)})"))
        elif rr < Decimal("1.5"):
            lines.append(Line(WARN, f"1:{rr.quantize(CENT)} is below your plan's lowest, 1:1.5"))
    else:
        lines.append(Line(WARN, "no target: the reward-to-risk cannot be judged"))
    margin = None
    if leverage:
        margin = lots * spec.contract_size * entry / Decimal(leverage)
        if currency == "USC":  # a Cent account counts in US cents
            margin *= HUNDRED
        if margin_free is not None and margin > margin_free:
            return refused(
                f"not affordable: it needs about {money(margin)} of margin and "
                f"{money(margin_free)} is free"
            )
    spread_cost = lots * quote.spread * vpp if quote is not None else None
    lines.append(
        Line(
            OK,
            f"{lots.normalize():f} lot (HFM's step {spec.volume_step.normalize():f}): "
            f"{money(risk_cash)} at the stop-loss, {(risk_cash / equity * HUNDRED).quantize(CENT)} "
            "% of equity",
        )
    )
    return Sizing(
        lots=lots,
        wanted=wanted,
        risk_cash=risk_cash,
        risk_pct=risk_cash / equity * HUNDRED,
        reward_cash=reward,
        rr=rr,
        margin=margin,
        spread_cost=spread_cost,
        per_lot_loss=per_lot_loss,
        distance=distance,
        lines=tuple(lines),
    )
