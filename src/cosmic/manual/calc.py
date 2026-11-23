"""The trade calculator (S113): from the owner's entry, stop-loss, target and risk, the lot size
and everything it means, on HFM's own contract for this account.

Every figure uses the contract MT5 reports (contract size, tick size, tick value, volume
minimum, step and maximum, swap terms): no broker's gold is assumed to be another's. The lot
size is rounded DOWN to a size HFM accepts (its minimum plus whole steps), so the risk is never
more than asked; a lot size the owner types is checked the same way. A trade whose smallest size
would risk more than allowed is NO TRADE: the risk is never raised to make it fit. A margin the
account cannot pay is refused.

Costs (S114): the spread is paid at the entry (a BUY fills at the ask, its stop closes at the
bid), so the loss at the stop is the price distance times the value of 1.00 a lot, plus the
commission measured from HFM's own deals on the account; the swap is a cost per night held, from
HFM's swap terms. Slippage is not modelled: a stop can fill worse in a fast market.

S115: the margin is HFM's own figure when its calculator has been read (bridge 13), otherwise
an estimate from the leverage, and says which. A trade is refused when a loss to its stop-loss
would bring the margin level to HFM's stop-out, and warned when the spread is far above its
usual or when there is no current price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from cosmic.manual.live import BUY, SELL, Deal, Quote, Spec

OK, WARN, BLOCK = "OK", "WARN", "BLOCK"
HUNDRED = Decimal(100)
CENT = Decimal("0.01")
PENDING_GAP = Decimal("0.001")  # an entry this far from the price now is a pending order
# MT5's ENUM_DAY_OF_WEEK, for the night HFM charges three times.
DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


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
    lots: Decimal | None = None  # a size the owner wants checked; wins over both


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
    commission: Decimal = Decimal(0)  # both sides, for this size (a cost: 0 or more)
    swap_night: Decimal | None = None  # money a night held (negative: paid); None: not known
    margin_source: str = ""  # HFM's own calculator, or an estimate from the leverage
    margin_level_at_stop: Decimal | None = None  # the account's, if the stop-loss is hit

    @property
    def blocked(self) -> bool:
        return any(x.level == BLOCK for x in self.lines)


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """``value`` rounded down to a whole number of ``step``."""
    return (value / step).to_integral_value(ROUND_DOWN) * step


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def valid_lots(raw: Decimal, spec: Spec) -> Decimal:
    """The largest size HFM accepts that is not above ``raw``: its minimum plus whole steps
    (0 when even the minimum is above it)."""
    if raw < spec.volume_min:
        return Decimal(0)
    return spec.volume_min + floor_to(raw - spec.volume_min, spec.volume_step)


def on_grid(lots: Decimal, spec: Spec) -> bool:
    return lots >= spec.volume_min and (lots - spec.volume_min) % spec.volume_step == 0


@dataclass(frozen=True)
class Commission:
    """HFM's commission and fees for one lot, both sides, as its own deals on the account show
    (``deals`` of them); None when the account has no gold deals yet."""

    per_lot: Decimal | None
    deals: int


def commission_of(deals: tuple[Deal, ...] | list[Deal]) -> Commission:
    trades = [d for d in deals if d.side is not None and d.lots > 0]
    volume = sum((d.lots for d in trades), Decimal(0))
    if not trades or volume <= 0:
        return Commission(None, 0)
    paid = -sum((d.commission + d.fee for d in trades), Decimal(0))  # MT5 reports costs < 0
    return Commission(max(paid, Decimal(0)) / volume * 2, len(trades))


def swap_per_lot(spec: Spec, side: str, price: Decimal, currency: str | None) -> Decimal | None:
    """HFM's swap for one lot held one night, in the account's currency (negative: paid), from
    its own terms; None when the terms are missing or in a form not converted here."""
    raw = spec.swap_long if side == BUY else spec.swap_short
    mode = spec.swap_mode
    if raw is None or mode is None:
        return None
    if mode == 0:
        return Decimal(0)
    if mode == 1:  # points: each point is worth the value of ``point`` a lot
        return raw * spec.point * spec.value_per_price
    if mode == 4 or (mode == 3 and spec.currency_margin == currency):  # money a lot
        return raw
    if mode in (5, 6) and spec.currency_profit == currency:  # a yearly % of the position's value
        return price * spec.contract_size * raw / HUNDRED / 360
    return None


def triple_night(spec: Spec) -> str | None:
    day = spec.swap_rollover3days
    return DAYS[day] if day is not None and 0 <= day <= 6 else None


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
    normal_spread: Decimal | None = None,
    margin_used: Decimal | None = None,
    stop_out_pct: Decimal | None = None,
) -> Sizing:
    """The lot size and the trade's figures, with every reason to warn or refuse.

    ``normal_spread``: HFM's usual spread (S115), to warn when it is wide now. ``margin_used``
    and ``stop_out_pct``: the account's margin in use and HFM's stop-out level in percent, to
    refuse a trade whose loss at the stop-loss would let HFM close positions itself first."""
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
    if quote is None:
        lines.append(
            Line(
                WARN,
                "no current HFM price (the market is closed or the terminal is silent): the "
                "spread and whether this is a market order are not checked, and no spread is "
                "counted in the costs",
            )
        )
    else:
        if normal_spread is not None and normal_spread > 0 and quote.spread > 2 * normal_spread:
            trigger = "a SELL's stop-loss is hit by the ask" if side == SELL else "costs rise"
            lines.append(
                Line(
                    WARN,
                    f"the spread is {quote.spread} now, {(quote.spread / normal_spread):.1f} "
                    f"times its usual {normal_spread}: {trigger}; wait for it to narrow",
                )
            )
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
    if spec.freeze_level and distance <= spec.freeze_level * spec.point:
        lines.append(
            Line(
                WARN,
                f"HFM's freeze level is {spec.freeze_level * spec.point}: this stop-loss could not "
                "be moved once the price is that near it",
            )
        )
    if distance > entry * Decimal("0.05"):
        lines.append(Line(WARN, "the stop-loss is more than 5 % of the price away: unusually wide"))
    vpp = spec.value_per_price
    per_lot_loss = distance * vpp + commission_per_lot
    max_cash = equity * max_risk_pct / HUNDRED
    if ticket.lots is not None:  # the owner's own size, checked
        lots = ticket.lots
        if not on_grid(lots, spec) or lots > spec.volume_max:
            return refused(
                f"{lots.normalize():f} lot is not a size HFM accepts: from "
                f"{spec.volume_min.normalize():f} up to {spec.volume_max.normalize():f}, in steps "
                f"of {spec.volume_step.normalize():f}"
            )
        wanted = lots * per_lot_loss
        if wanted > max_cash:
            return refused(
                f"{lots.normalize():f} lot would lose {money(wanted)} at this stop-loss, "
                f"{(wanted / equity * HUNDRED).quantize(CENT)} % of equity: above your limit of "
                f"{max_risk_pct.normalize():f} % a trade"
            )
    else:
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
                f"{money(wanted)} is {wanted_pct.quantize(CENT)} % of equity: above your limit "
                f"of {max_risk_pct.normalize():f} % a trade"
            )
        lots = valid_lots(wanted / per_lot_loss, spec)
        if lots == 0:
            smallest = spec.volume_min * per_lot_loss
            fits = ""
            if ticket.risk_amount is None and ticket.risk_pct:
                need = (smallest / wanted * equity).quantize(Decimal(1), ROUND_CEILING)
                fits = (
                    f" (at {ticket.risk_pct.normalize():f} % it fits from about {need:,} of equity)"
                )
            return refused(
                f"HFM's smallest trade ({spec.volume_min.normalize():f} lot) would lose "
                f"{money(smallest)} at this stop-loss, "
                f"{(smallest / equity * HUNDRED).quantize(CENT)} % of equity: more than the "
                f"{money(wanted)} you allow. NO TRADE: the account is too small for this "
                f"stop-loss within your rule{fits}. Never raise the risk to make it fit"
            )
        if lots > spec.volume_max:
            lots = valid_lots(spec.volume_max, spec)
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
    margin_source = ""
    own = spec.margin_per_lot(side, entry)
    if own is not None:  # HFM's own calculator (bridge 13), in the account's currency
        margin = lots * own
        margin_source = "HFM's own margin calculator"
    elif leverage:
        margin = lots * spec.contract_size * entry / Decimal(leverage)
        if currency == "USC":  # a Cent account counts in US cents
            margin *= HUNDRED
        margin_source = f"estimated from the leverage 1:{leverage} (HFM's own figure not read)"
    if margin is not None and margin_free is not None and margin > margin_free:
        return refused(
            f"not affordable: it needs about {money(margin)} of margin and "
            f"{money(margin_free)} is free"
        )
    level_at_stop = None
    if margin is not None and margin > 0:
        in_use = (margin_used or Decimal(0)) + margin
        level_at_stop = (equity - lots * per_lot_loss) / in_use * HUNDRED
        if stop_out_pct is not None and level_at_stop <= stop_out_pct:
            return refused(
                f"if the stop-loss is hit the margin level falls to {level_at_stop:.0f} %, at or "
                f"below HFM's stop-out of {stop_out_pct.normalize():f} %: HFM would close "
                "positions itself before your stop-loss"
            )
    spread_cost = lots * quote.spread * vpp if quote is not None else None
    per_night = swap_per_lot(spec, side, entry, currency)
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
        commission=lots * commission_per_lot,
        swap_night=None if per_night is None else lots * per_night,
        margin_source=margin_source,
        margin_level_at_stop=level_at_stop,
    )
