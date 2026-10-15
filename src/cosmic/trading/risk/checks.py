"""Risk checks R01–R16 (doc 08 §3). Each is pure and returns PASS, FAIL or UNKNOWN."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from cosmic.core.labels import DataLabel
from cosmic.core.money import BPS, ZERO
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, Operation, mode_permits
from cosmic.trading.policy import RiskPolicyParams
from cosmic.trading.precision import InstrumentRulesView, is_on_grid
from cosmic.trading.risk.context import RiskContext, Stage

HUNDRED = Decimal(100)


class Status(StrEnum):
    PASS = "PASS"  # noqa: S105 - check status  # nosec B105
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CheckResult:
    code: str
    name: str
    status: Status
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


Check = Callable[[RiskContext, RiskPolicyParams], CheckResult]


def _r(code: str, name: str, status: Status, message: str, **detail: Any) -> CheckResult:
    return CheckResult(code, name, status, message, detail)


def r01_mode(ctx: RiskContext, _p: RiskPolicyParams) -> CheckResult:
    op = Operation.OPEN_EXPOSURE if ctx.request.exposure_increasing else Operation.REDUCE_EXPOSURE
    ok, why = mode_permits(ctx.mode, op, account_kind=ctx.account.kind)
    return _r(
        "R01",
        "Mode permits operation",
        Status.PASS if ok else Status.FAIL,
        why,
        mode=ctx.mode.value,
        operation=op.value,
    )


def r02_account(ctx: RiskContext, _p: RiskPolicyParams) -> CheckResult:
    a = ctx.account
    if a.status != "ACTIVE":
        return _r("R02", "Account", Status.FAIL, f"account {a.alias} is {a.status}")
    if a.alias != ctx.request.account_alias:
        return _r("R02", "Account", Status.FAIL, "account does not match the request")
    if a.venue_key != ctx.instrument.venue_key:
        return _r(
            "R02",
            "Account",
            Status.FAIL,
            f"account trades on {a.venue_key}, not {ctx.instrument.venue_key}",
        )
    return _r("R02", "Account", Status.PASS, f"{a.kind.lower()} account {a.alias} is active")


def r03_integrity(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    a = ctx.account
    if a.reconciliation_status != "OK":
        return _r(
            "R03",
            "Account integrity",
            Status.FAIL,
            f"reconciliation is {a.reconciliation_status}; new exposure blocked until OK",
        )
    if a.last_reconciled_at is None or ctx.now - a.last_reconciled_at > timedelta(
        seconds=p.reconciliation_max_age_seconds
    ):
        return _r(
            "R03",
            "Account integrity",
            Status.UNKNOWN,
            "last successful reconciliation is too old or missing",
            last_reconciled_at=a.last_reconciled_at,
        )
    if (
        a.unknown_orders is None
        or a.cash_available is None
        or a.positions is None
        or a.open_orders is None
    ):
        return _r(
            "R03", "Account integrity", Status.UNKNOWN, "cash, positions or open orders are unknown"
        )
    if a.unknown_orders > 0:
        return _r(
            "R03",
            "Account integrity",
            Status.FAIL,
            f"{a.unknown_orders} order(s) in UNKNOWN_ORDER_STATE must be reconciled first",
        )
    return _r("R03", "Account integrity", Status.PASS, "account reconciled; no unknown orders")


def r04_permissions(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    r, i = ctx.request, ctx.instrument
    problems = []
    if i.canonical_key not in p.allowed_instruments:
        problems.append(f"{i.canonical_key} is not on the allowed instrument list")
    if i.venue_key not in p.allowed_venues:
        problems.append(f"venue {i.venue_key} is not allowed")
    if i.quote_currency not in p.allowed_quote_currencies:
        problems.append(f"currency {i.quote_currency} is not allowed")
    if r.order_type not in p.allowed_order_types:
        problems.append(f"order type {r.order_type} is not allowed")
    if r.time_in_force not in p.allowed_time_in_force:
        problems.append(f"time in force {r.time_in_force} is not allowed")
    if r.side == "SELL" and r.exposure_increasing:
        problems.append("short selling is not allowed")
    if not i.tradable:
        problems.append(f"{i.canonical_key} is reference-only, not tradable")
    if problems:
        return _r("R04", "Instrument permissions", Status.FAIL, "; ".join(problems))
    return _r(
        "R04",
        "Instrument permissions",
        Status.PASS,
        "instrument, venue, currency and order type allowed",
    )


def r05_order_notional(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    n = ctx.request.notional
    status = Status.PASS if n <= p.max_order_notional else Status.FAIL
    return _r(
        "R05",
        "Order notional",
        status,
        f"order notional {n:.2f} vs limit {p.max_order_notional}",
        notional=n,
        limit=p.max_order_notional,
    )


def _position(ctx: RiskContext, key: str) -> Decimal:
    for pos in ctx.account.positions or ():
        if pos.instrument_key == key:
            return pos.quantity
    return ZERO


def r06_position(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    a, r = ctx.account, ctx.request
    if a.positions is None or a.equity is None:
        return _r(
            "R06", "Position and concentration", Status.UNKNOWN, "positions or equity unknown"
        )
    after = _position(ctx, r.instrument_key) + (r.quantity if r.side == "BUY" else -r.quantity)
    notional_after = after * r.limit_price
    if notional_after > p.max_position_notional:
        return _r(
            "R06",
            "Position and concentration",
            Status.FAIL,
            f"post-trade position {notional_after:.2f} exceeds {p.max_position_notional}",
            position_after=after,
            notional_after=notional_after,
        )
    if a.equity <= ZERO:
        return _r(
            "R06", "Position and concentration", Status.FAIL, "account equity is not positive"
        )
    conc = notional_after / a.equity * HUNDRED
    if conc > p.max_concentration_pct:
        return _r(
            "R06",
            "Position and concentration",
            Status.FAIL,
            f"post-trade concentration {conc:.1f}% exceeds {p.max_concentration_pct}%",
            concentration_pct=conc,
        )
    return _r(
        "R06",
        "Position and concentration",
        Status.PASS,
        f"post-trade position {notional_after:.2f} ({conc:.1f}% of equity)",
        position_after=after,
        concentration_pct=conc,
    )


def r07_exposure_cash(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    a, r = ctx.account, ctx.request
    if a.positions is None or a.open_orders is None or a.cash_available is None:
        return _r(
            "R07", "Exposure and buying power", Status.UNKNOWN, "positions, orders or cash unknown"
        )
    gross = ZERO
    for pos in a.positions:
        if pos.quantity != ZERO:
            if pos.mark_price is None:
                return _r(
                    "R07",
                    "Exposure and buying power",
                    Status.UNKNOWN,
                    f"no fresh mark price for held {pos.instrument_key}",
                )
            gross += abs(pos.quantity * pos.mark_price)
    reserved = ZERO
    for o in a.open_orders:
        if not o.reduce_only and o.side == "BUY":
            gross += o.remaining_quantity * o.limit_price
            reserved += o.remaining_quantity * o.limit_price
    gross_after = gross + r.notional
    if gross_after > p.max_gross_exposure:
        return _r(
            "R07",
            "Exposure and buying power",
            Status.FAIL,
            f"gross exposure after trade {gross_after:.2f} exceeds {p.max_gross_exposure}",
            gross_after=gross_after,
        )
    fees = r.notional * p.fee_bps_assumption / BPS
    needed = r.notional + fees if r.side == "BUY" else ZERO
    free_cash = a.cash_available - reserved
    if needed > free_cash:
        return _r(
            "R07",
            "Exposure and buying power",
            Status.FAIL,
            f"needs {needed:.2f} incl. estimated fees; free cash is {free_cash:.2f}",
            free_cash=free_cash,
        )
    return _r(
        "R07",
        "Exposure and buying power",
        Status.PASS,
        f"gross exposure after trade {gross_after:.2f}; free cash {free_cash:.2f}",
        gross_after=gross_after,
    )


def r08_loss_limits(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    a = ctx.account
    realized, unrealized = a.realized_pnl_today, a.unrealized_pnl
    equity, hwm, streak = a.equity, a.high_water_mark, a.loss_streak
    if realized is None or unrealized is None or equity is None or hwm is None or streak is None:
        return _r(
            "R08",
            "Loss limits",
            Status.UNKNOWN,
            "P&L, equity, high-water mark or loss streak unknown",
        )
    day_loss = -(realized + unrealized)
    if day_loss >= p.max_daily_loss:
        return _r(
            "R08",
            "Loss limits",
            Status.FAIL,
            f"daily loss {day_loss:.2f} reached limit {p.max_daily_loss}",
        )
    dd = (hwm - equity) / hwm * HUNDRED if hwm > ZERO else ZERO
    if dd >= p.max_drawdown_pct:
        return _r(
            "R08",
            "Loss limits",
            Status.FAIL,
            f"drawdown {dd:.2f}% reached limit {p.max_drawdown_pct}%",
        )
    if streak >= p.max_loss_streak:
        return _r(
            "R08",
            "Loss limits",
            Status.FAIL,
            f"{streak} consecutive losing trades (limit {p.max_loss_streak})",
        )
    return _r(
        "R08",
        "Loss limits",
        Status.PASS,
        f"daily loss {max(day_loss, ZERO):.2f}, drawdown {dd:.2f}%, loss streak {streak}",
    )


def r09_price_validity(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    m = ctx.market
    acceptable = {DataLabel.LIVE} | (
        {DataLabel.DELAYED}
        if p.allow_delayed_data and ctx.account.kind in SIMULATED_ACCOUNT_KINDS
        else set()
    )
    if m.quote_label not in acceptable:
        return _r(
            "R09",
            "Price validity and freshness",
            Status.FAIL,
            f"quote is {m.quote_label.value}; acceptable: {sorted(x.value for x in acceptable)}",
        )
    if m.quote_ts is None or m.mid is None:
        return _r("R09", "Price validity and freshness", Status.UNKNOWN, "no reference price")
    age = (ctx.now - m.quote_ts).total_seconds()
    if age > p.max_price_age_seconds:
        return _r(
            "R09",
            "Price validity and freshness",
            Status.FAIL,
            f"quote is {age:.0f}s old (limit {p.max_price_age_seconds}s)",
        )
    return _r(
        "R09",
        "Price validity and freshness",
        Status.PASS,
        f"{m.quote_label.value} quote, {age:.0f}s old",
        age_s=age,
    )


def r10_market_conditions(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    m, r, i = ctx.market, ctx.request, ctx.instrument
    if i.status not in ("ONLINE", "LIMIT_ONLY"):
        return _r("R10", "Market conditions", Status.FAIL, f"instrument status is {i.status}")
    if p.require_venue_status:
        if (
            m.venue_status_label is not DataLabel.DELAYED
            and m.venue_status_label is not DataLabel.LIVE
        ):
            return _r(
                "R10",
                "Market conditions",
                Status.UNKNOWN,
                f"venue status is {m.venue_status_label.value}",
            )
        if m.venue_status != "OPERATIONAL":
            return _r("R10", "Market conditions", Status.FAIL, f"venue status is {m.venue_status}")
    mid = m.mid
    if mid is None or m.bid is None or m.ask is None:
        return _r("R10", "Market conditions", Status.UNKNOWN, "no bid/ask")
    spread = (m.ask - m.bid) / mid * BPS
    if spread > p.max_spread_bps:
        return _r(
            "R10",
            "Market conditions",
            Status.FAIL,
            f"spread {spread:.1f} bps exceeds {p.max_spread_bps}",
        )
    if not (r.price_band_low <= r.limit_price <= r.price_band_high):
        return _r(
            "R10",
            "Market conditions",
            Status.FAIL,
            "limit price is outside the approved price band",
        )
    if not (r.price_band_low <= mid <= r.price_band_high):
        return _r(
            "R10",
            "Market conditions",
            Status.FAIL,
            f"mid {mid:.2f} outside approved band {r.price_band_low}–{r.price_band_high}",
        )
    if p.require_cross_source_check:
        if m.deviation_label is not DataLabel.DELAYED or m.deviation_bps is None:
            return _r(
                "R10",
                "Market conditions",
                Status.UNKNOWN,
                f"cross-venue check unavailable ({m.deviation_label.value})",
            )
        if m.deviation_bps > p.max_cross_source_deviation_bps:
            return _r(
                "R10",
                "Market conditions",
                Status.FAIL,
                f"venues differ by {m.deviation_bps:.1f} bps "
                f"(limit {p.max_cross_source_deviation_bps})",
            )
    return _r(
        "R10",
        "Market conditions",
        Status.PASS,
        f"spread {spread:.1f} bps; price inside band",
        spread_bps=spread,
    )


def r11_costs(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    m, r = ctx.market, ctx.request
    if r.max_slippage_bps > p.max_slippage_bps:
        return _r(
            "R11",
            "Costs and slippage",
            Status.FAIL,
            f"slippage tolerance {r.max_slippage_bps} bps exceeds policy {p.max_slippage_bps}",
        )
    mid = m.mid
    if mid is None or m.bid is None or m.ask is None:
        return _r("R11", "Costs and slippage", Status.UNKNOWN, "cannot estimate spread cost")
    half_spread = (m.ask - m.bid) / mid * BPS / 2
    total = p.fee_bps_assumption + r.max_slippage_bps + half_spread
    if total > p.max_cost_bps:
        return _r(
            "R11",
            "Costs and slippage",
            Status.FAIL,
            f"estimated cost {total:.1f} bps exceeds {p.max_cost_bps}",
        )
    return _r(
        "R11",
        "Costs and slippage",
        Status.PASS,
        f"est. cost {total:.1f} bps "
        f"(fee {p.fee_bps_assumption} ASSUMPTION + slippage + half spread)",
        estimated_cost_bps=total,
    )


def r12_duplicates(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    a, r = ctx.account, ctx.request
    if a.open_orders is None:
        return _r("R12", "Duplicates and conflicts", Status.UNKNOWN, "open orders unknown")
    same = [o for o in a.open_orders if o.instrument_key == r.instrument_key and not o.reduce_only]
    if any(o.side == r.side for o in same):
        return _r(
            "R12",
            "Duplicates and conflicts",
            Status.FAIL,
            "an open order on the same instrument and side exists",
        )
    if same:
        return _r(
            "R12",
            "Duplicates and conflicts",
            Status.FAIL,
            "a conflicting open order on this instrument exists",
        )
    if len(a.open_orders) >= p.max_open_orders:
        return _r(
            "R12",
            "Duplicates and conflicts",
            Status.FAIL,
            f"{len(a.open_orders)} open orders (limit {p.max_open_orders})",
        )
    if ctx.stage is Stage.PRE_APPROVAL and a.active_intents_same_instrument_side > 0:
        return _r(
            "R12",
            "Duplicates and conflicts",
            Status.FAIL,
            "another proposal for this instrument and side is pending",
        )
    return _r("R12", "Duplicates and conflicts", Status.PASS, "no duplicate or conflicting orders")


def r13_blackout(ctx: RiskContext, p: RiskPolicyParams) -> CheckResult:
    for w in p.blackout_windows:
        if w.start <= ctx.now < w.end:
            return _r("R13", "Event blackout", Status.FAIL, f"inside blackout window: {w.reason}")
    return _r("R13", "Event blackout", Status.PASS, "no blackout window active")


def r14_authorization(ctx: RiskContext, _p: RiskPolicyParams) -> CheckResult:
    if ctx.stage is Stage.PRE_APPROVAL:
        return _r(
            "R14", "Authorization", Status.PASS, "not yet required (approval is requested next)"
        )
    auth = ctx.authorization
    if auth is None:
        return _r("R14", "Authorization", Status.FAIL, "no approval record")
    problems = []
    if auth.approval_status not in ("ACTIVE", "CONSUMED"):
        problems.append(f"approval is {auth.approval_status}")
    if auth.approval_expires_at is None or auth.approval_expires_at <= ctx.now:
        problems.append("approval has expired")
    if auth.intent_expires_at is None or auth.intent_expires_at <= ctx.now:
        problems.append("trade intent has expired")
    if not auth.packet_hash_matches:
        problems.append("approved packet hash does not match the trade")
    if not auth.policy_version_matches:
        problems.append("risk policy changed since approval")
    if not auth.operator_can_approve:
        problems.append("approving operator lacks APPROVE_TRADE")
    if problems:
        return _r("R14", "Authorization", Status.FAIL, "; ".join(problems))
    return _r(
        "R14", "Authorization", Status.PASS, "valid single-use approval for this exact packet"
    )


def r15_incidents(ctx: RiskContext, _p: RiskPolicyParams) -> CheckResult:
    blocking = ctx.critical_incident_categories & {"SECURITY", "ACCOUNTING"}
    if blocking:
        return _r(
            "R15",
            "Security and accounting incidents",
            Status.FAIL,
            f"open critical incident(s): {', '.join(sorted(blocking))}",
        )
    return _r("R15", "Security and accounting incidents", Status.PASS, "no blocking incident")


def r16_precision(ctx: RiskContext, _p: RiskPolicyParams) -> CheckResult:
    i, r = ctx.instrument, ctx.request
    rules = InstrumentRulesView(
        i.canonical_key, i.tick_size, i.lot_size, i.min_qty, i.max_qty, i.min_notional
    )
    if i.tick_size is None or i.lot_size is None:
        return _r(
            "R16", "Precision and minimums", Status.UNKNOWN, "instrument tick/lot size unknown"
        )
    problems = []
    if not is_on_grid(rules, r.quantity, r.limit_price):
        problems.append("quantity or price is not on the venue's lot/tick grid")
    if i.min_qty is not None and r.quantity < i.min_qty:
        problems.append(f"quantity below minimum {i.min_qty}")
    if i.max_qty is not None and r.quantity > i.max_qty:
        problems.append(f"quantity above maximum {i.max_qty}")
    if i.min_notional is not None and r.notional < i.min_notional:
        problems.append(f"notional below minimum {i.min_notional}")
    if problems:
        return _r("R16", "Precision and minimums", Status.FAIL, "; ".join(problems))
    return _r(
        "R16",
        "Precision and minimums",
        Status.PASS,
        "quantity and price on grid and above minimums",
    )


PROTECTIVE = frozenset({"STOP_LOSS", "TAKE_PROFIT"})


def r17_reduce_bound(ctx: RiskContext, _p: RiskPolicyParams) -> CheckResult:
    """Reduce-only orders may never exceed the held quantity minus pending reduce orders.

    An approved exit replaces the protective orders of the entries it closes (they are
    cancelled before it is sent), so those orders are not counted against it.
    """
    a, r = ctx.account, ctx.request
    if a.positions is None or a.open_orders is None:
        return _r("R17", "Reduce-only bound", Status.UNKNOWN, "position or open orders unknown")
    held = _position(ctx, r.instrument_key)
    replaced = [
        o
        for o in a.open_orders
        if o.instrument_key == r.instrument_key
        and o.purpose in PROTECTIVE
        and o.intent_id is not None
        and o.intent_id in r.closes
    ]
    pending = sum(
        (
            o.remaining_quantity
            for o in a.open_orders
            if o.instrument_key == r.instrument_key
            and o.reduce_only
            and o.side == r.side
            and not any(o is x for x in replaced)
        ),
        ZERO,
    )
    if r.side != "SELL":
        return _r("R17", "Reduce-only bound", Status.FAIL, "only SELL reduces a long spot position")
    if r.quantity + pending > held:
        return _r(
            "R17",
            "Reduce-only bound",
            Status.FAIL,
            f"reduce quantity {r.quantity} + pending {pending} exceeds held {held}",
        )
    note = (
        f"; replaces {len(replaced)} protective order(s) of the entries it closes"
        if replaced
        else ""
    )
    return _r("R17", "Reduce-only bound", Status.PASS, f"within held quantity {held}{note}")


EXPOSURE_CHECKS: tuple[Check, ...] = (
    r01_mode,
    r02_account,
    r03_integrity,
    r04_permissions,
    r05_order_notional,
    r06_position,
    r07_exposure_cash,
    r08_loss_limits,
    r09_price_validity,
    r10_market_conditions,
    r11_costs,
    r12_duplicates,
    r13_blackout,
    r14_authorization,
    r15_incidents,
    r16_precision,
)
REDUCE_CHECKS: tuple[Check, ...] = (r01_mode, r02_account, r16_precision, r17_reduce_bound)
