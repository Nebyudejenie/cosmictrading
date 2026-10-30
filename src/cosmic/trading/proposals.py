"""Trade candidates → immutable intents and hashed approval packets (V6 §1805–§1807).

``create_candidate`` is the only path from research (or a manual operator proposal) into
the trading engine. It validates the typed candidate, normalises precision, runs the
deterministic pre-approval checks and either stores a PROPOSED intent with packet v1
(and requests approval) or a NON_EXECUTABLE intent with reasons — never both.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, ActorType, record
from cosmic.core.clock import isoformat_utc
from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.core.ids import trade_code, uuid7
from cosmic.core.jsonutil import canonical_hash, to_jsonable
from cosmic.core.money import BPS, ZERO, canonical_str
from cosmic.db.schema_execution import approval_packets, fills, trade_intents
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_market import assets, instruments
from cosmic.db.schema_research import opportunities, strategies, strategy_versions
from cosmic.db.schema_trading import risk_check_runs
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.authority import get_state
from cosmic.trading.brokers.base import VenueQuote
from cosmic.trading.context_builder import build_context
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, account_fields, environment_of
from cosmic.trading.policy import LIMIT_SEMANTICS, RiskPolicyParams, active_policy
from cosmic.trading.precision import InstrumentRulesView, normalise
from cosmic.trading.risk.context import Effect, OrderRequest, RiskContext, Stage
from cosmic.trading.risk.engine import RiskDecision, evaluate
from cosmic.trading.venue_terms import funding_text, protection_terms, session_text

LOSS_WARNING = (
    "Actual losses can exceed this estimate. Prices can gap past a stop; a stop-limit order may "
    "not fill at all in a fast market; liquidity, partial fills, venue outages and execution "
    "delays can all make results worse. A protective order is not a guaranteed maximum loss."
)
PROHIBITED_ACTIONS = (
    "add to the position or open another position",
    "reverse the position or change its direction",
    "increase quantity, leverage or the loss envelope",
    "widen the price band, slippage tolerance or stop level",
    "substitute the instrument, account, venue or strategy",
)


class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal[
        "QUOTE", "BAR", "INDICATOR", "BACKTEST", "DOCUMENT", "MARKET_EVENT", "CLAIM", "NOTE"
    ]
    ref: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=3, max_length=500)
    source_ts: datetime | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=2, max_length=60)
    description: str = Field(min_length=5, max_length=500)


class StopLoss(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    stop_price: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _order(self) -> StopLoss:
        if self.limit_price > self.stop_price:
            raise ValueError("a sell stop's limit price must be at or below its stop price")
        return self


class ManagementPlan(BaseModel):
    """Exactly the position-management actions the operator authorises (V6 §1810)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    stop_loss: StopLoss | None = None
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    cancel_unfilled_after_seconds: int | None = Field(default=None, ge=60, le=86400)


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    account_alias: str = Field(min_length=2, max_length=42)
    instrument_key: str = Field(min_length=5, max_length=80)
    side: Literal["BUY", "SELL"]
    effect: Effect
    quantity: Decimal = Field(gt=0)
    order_type: Literal["LIMIT"] = "LIMIT"
    limit_price: Decimal = Field(gt=0)
    price_band_low: Decimal = Field(gt=0)
    price_band_high: Decimal = Field(gt=0)
    max_slippage_bps: Decimal = Field(ge=0, le=500)
    time_in_force: Literal["GTC", "GTD", "IOC"] = "GTC"
    horizon: str = Field(min_length=2, max_length=80)
    thesis: str = Field(min_length=20, max_length=4000)
    plain_summary: str = Field(min_length=20, max_length=600)
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=30)
    counterarguments: tuple[str, ...] = Field(min_length=1, max_length=20)
    red_team: str = Field(min_length=10, max_length=2000)
    scenarios: tuple[Scenario, ...] = Field(min_length=1, max_length=10)
    invalidation: tuple[str, ...] = Field(min_length=1, max_length=10)
    entry_conditions: str = Field(min_length=5, max_length=500)
    wait_or_no_trade_reasons: tuple[str, ...] = Field(default=(), max_length=10)
    management_plan: ManagementPlan = ManagementPlan()
    opportunity_id: uuid.UUID | None = None
    # An exit names the entries it closes; approving it authorises cancelling their protective
    # orders first (they would otherwise reserve the same quantity — R17).
    closes: tuple[uuid.UUID, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _consistent(self) -> Candidate:
        if not self.price_band_low <= self.limit_price <= self.price_band_high:
            raise ValueError("limit price must lie inside the price band")
        plan = self.management_plan
        if self.side == "BUY" and plan.stop_loss and plan.stop_loss.stop_price >= self.limit_price:
            raise ValueError("a long position's stop must be below the entry price")
        if (
            self.side == "BUY"
            and plan.take_profit_price
            and plan.take_profit_price <= self.limit_price
        ):
            raise ValueError("take-profit must be above the entry price")
        if self.side == "SELL" and self.effect in (Effect.OPEN, Effect.INCREASE):
            raise ValueError("short selling is not supported")
        if self.closes and not (
            self.side == "SELL" and self.effect in (Effect.REDUCE, Effect.CLOSE)
        ):
            raise ValueError("only an exit (a SELL that reduces or closes) can close entries")
        if self.closes and self.time_in_force != "IOC":
            # A resting exit would leave the position without its (cancelled) stop.
            raise ValueError(
                "an exit that replaces protective stops must be IOC: what does not fill at once "
                "expires and the stop is placed again"
            )
        if len(set(self.closes)) != len(self.closes):
            raise ValueError("an entry is listed twice in closes")
        return self


@dataclass(frozen=True)
class ProposalResult:
    intent_id: uuid.UUID
    trade_code: str
    status: str
    decision: RiskDecision | None
    problems: tuple[str, ...]
    packet_hash: str | None


def _request(c: Candidate, quantity: Decimal, price: Decimal) -> OrderRequest:
    return OrderRequest(
        account_alias=c.account_alias,
        instrument_key=c.instrument_key,
        side=c.side,
        effect=c.effect,
        quantity=quantity,
        order_type=c.order_type,
        limit_price=price,
        time_in_force=c.time_in_force,
        price_band_low=c.price_band_low,
        price_band_high=c.price_band_high,
        max_slippage_bps=c.max_slippage_bps,
        closes=tuple(str(i) for i in c.closes),
        stop_limit_price=(
            c.management_plan.stop_loss.limit_price if c.management_plan.stop_loss else None
        ),
    )


def sellable(quantity: Decimal, lot: Decimal | None) -> Decimal:
    """The part of a holding the venue can trade: whole lot steps. What is left below one step
    is dust (a base-asset fee can leave it): it is booked and reconciled, but nothing tries to
    sell or protect it (ADR-026 §7)."""
    if lot is None or lot <= ZERO or quantity <= ZERO:
        return quantity
    return (quantity // lot) * lot


async def entry_held(conn: AsyncConnection, intent_id: uuid.UUID) -> Decimal:
    """What an entry still holds: its buys minus every sell on its orders (stops, exits), and
    minus fees the venue took in the base asset (a Binance buy pays commission in the asset
    received)."""
    base = await conn.scalar(
        select(assets.c.symbol)
        .join(instruments, instruments.c.base_asset_id == assets.c.id)
        .join(trade_intents, trade_intents.c.instrument_id == instruments.c.id)
        .where(trade_intents.c.id == intent_id)
    )
    held = ZERO
    for r in await conn.execute(
        select(
            orders_t.c.side,
            func.coalesce(func.sum(fills.c.quantity), 0).label("q"),
            func.coalesce(
                func.sum(case((fills.c.fee_currency == base, fills.c.fee), else_=0)), 0
            ).label("f"),
        )
        .join(fills, fills.c.order_id == orders_t.c.id)
        .where(orders_t.c.intent_id == intent_id)
        .group_by(orders_t.c.side)
    ):
        q, f = Decimal(r.q), Decimal(r.f)
        held += q - f if r.side == "BUY" else -(q + f)
    return held


async def entry_available(
    conn: AsyncConnection, intent_id: uuid.UUID, *, exclude_order_id: uuid.UUID | None = None
) -> Decimal:
    """What an entry can still give to an exit: what it holds minus the unfilled part of every
    other exit order already working on it."""
    q = select(func.coalesce(func.sum(orders_t.c.quantity - orders_t.c.filled_qty), 0)).where(
        orders_t.c.intent_id == intent_id,
        orders_t.c.purpose == "EXIT",
        orders_t.c.status.notin_(["FILLED", "CANCELLED", "REJECTED", "EXPIRED"]),
    )
    if exclude_order_id is not None:
        q = q.where(orders_t.c.id != exclude_order_id)
    pending = Decimal(str(await conn.scalar(q) or 0))
    return await entry_held(conn, intent_id) - pending


async def open_entries(
    conn: AsyncConnection, account_id: uuid.UUID, instrument_id: uuid.UUID
) -> list[Any]:
    """Filled BUY entries of this account in this instrument that still hold at least one
    tradable lot (dust alone does not keep an entry open)."""
    lot = await conn.scalar(select(instruments.c.lot_size).where(instruments.c.id == instrument_id))
    rows = list(
        await conn.execute(
            select(trade_intents.c.id, trade_intents.c.trade_code).where(
                trade_intents.c.account_id == account_id,
                trade_intents.c.instrument_id == instrument_id,
                trade_intents.c.side == "BUY",
                trade_intents.c.status.in_(["EXECUTING", "EXECUTED"]),
            )
        )
    )
    return [r for r in rows if sellable(await entry_held(conn, r.id), lot) > ZERO]


async def closed_entries(
    conn: AsyncConnection, candidate: Candidate, account: Account, inst: md.InstrumentView
) -> list[dict[str, Any]]:
    """Validate an exit's ``closes``: this account's filled entries in this instrument,
    together holding at least the exit quantity."""
    out: list[dict[str, Any]] = []
    for intent_id in candidate.closes:
        row = (
            await conn.execute(select(trade_intents).where(trade_intents.c.id == intent_id))
        ).first()
        if (
            row is None
            or row.account_id != account.id
            or row.instrument_id != inst.id
            or row.side != "BUY"
            or row.status not in ("EXECUTING", "EXECUTED")
        ):
            raise ValidationFailedError(
                "closes must name filled entries of this account in this instrument"
            )
        held = await entry_available(conn, intent_id)
        if held <= ZERO:
            raise ValidationFailedError(
                f"{row.trade_code} holds nothing that is not already being sold by another exit"
            )
        out.append({"intent_id": intent_id, "trade_code": row.trade_code, "holds": held})
    total = sum((c["holds"] for c in out), ZERO)
    if out and candidate.quantity > total:
        raise ValidationFailedError(
            f"the exit sells {candidate.quantity} but those entries hold {total}"
        )
    return out


def _results_json(decision: RiskDecision) -> list[dict[str, Any]]:
    return [
        {
            "code": r.code,
            "name": r.name,
            "status": r.status.value,
            "message": r.message,
            "detail": to_jsonable_lenient(r.detail),
        }
        for r in decision.results
    ]


def to_jsonable_lenient(value: Any) -> Any:
    from cosmic.core.jsonutil import to_jsonable_lenient as tj

    return tj(value)


def _obj(value: Any) -> dict[str, Any]:
    result = to_jsonable(value)
    assert isinstance(result, dict)
    return result


def _context_json(ctx: RiskContext) -> dict[str, Any]:
    a, m = ctx.account, ctx.market
    return _obj(
        {
            "mode": ctx.mode.value,
            "stage": ctx.stage.value,
            "now": ctx.now,
            "account": {
                "alias": a.alias,
                "kind": a.kind,
                "reconciliation_status": a.reconciliation_status,
                "cash_available": a.cash_available,
                "equity": a.equity,
                "unknown_orders": a.unknown_orders,
                "open_orders": len(a.open_orders or ()),
            },
            "market": {
                "bid": m.bid,
                "ask": m.ask,
                "quote_ts": m.quote_ts,
                "quote_label": m.quote_label.value,
                "deviation_bps": m.deviation_bps,
                "deviation_label": m.deviation_label.value,
                "venue_status": m.venue_status,
            },
        }
    )


async def record_risk_run(
    conn: AsyncConnection,
    *,
    ctx: RiskContext,
    decision: RiskDecision,
    account_id: uuid.UUID,
    intent_id: uuid.UUID | None,
    order_id: uuid.UUID | None,
    now: datetime,
) -> uuid.UUID:
    run_id = uuid7()
    await conn.execute(
        risk_check_runs.insert().values(
            id=run_id,
            account_id=account_id,
            intent_id=intent_id,
            order_id=order_id,
            stage=ctx.stage.value,
            policy_version=ctx.policy_version,
            decision="PASS" if decision.passed else "FAIL",
            exposure_increasing=decision.exposure_increasing,
            results=_results_json(decision),
            context=_context_json(ctx),
            created_at=now,
        )
    )
    return run_id


def build_packet(
    *,
    code: str,
    intent_id: uuid.UUID,
    version: int,
    candidate: Candidate,
    account: Account,
    inst: md.InstrumentView,
    quantity: Decimal,
    price: Decimal,
    ctx: RiskContext,
    decision: RiskDecision,
    policy_version: int,
    policy: RiskPolicyParams,
    created_at: datetime,
    intent_expires_at: datetime,
    closes: list[dict[str, Any]] | None = None,
    strategy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every material term of the trade, plain language first (V6 §1807)."""
    notional = quantity * price
    m = ctx.market
    half_spread = ((m.ask - m.bid) / m.mid * BPS / 2) if (m.mid and m.ask and m.bid) else None
    fee_bps = policy.fee_bps_for(inst.venue_key)
    own_fee = any(f.venue == inst.venue_key for f in policy.venue_fees)
    est_fee = notional * fee_bps / BPS
    plan = candidate.management_plan
    stop = plan.stop_loss
    est_max_loss = None
    if stop is not None:
        est_max_loss = quantity * (price - stop.limit_price) + est_fee * 2
    post = next((r.detail for r in decision.results if r.code == "R06"), {})
    loss_pct = (
        None
        if est_max_loss is None or not ctx.account.equity or ctx.account.equity <= 0
        else est_max_loss / ctx.account.equity * 100
    )
    action = (
        f"{candidate.side} {canonical_str(quantity)} {inst.base} "
        f"at a limit of {canonical_str(price)} {inst.quote}"
    )
    env = environment_of(account.kind, account.broker_adapter)
    terms = protection_terms(account.broker_adapter)
    return _obj(
        {
            "plain_language": {
                "action": action,
                "account": f"{account.alias} ({env.label})",
                "why": candidate.plain_summary,
                "key_risk": candidate.counterarguments[0],
                "expires": isoformat_utc(intent_expires_at),
                "consequence_of_approving": (
                    (
                        "COSMICTRADING will first cancel the protective orders of "
                        + ", ".join(str(c["trade_code"]) for c in closes)
                        + " (this exit replaces them), then submit exactly this reduce-only sell "
                        "after re-checking risk. If the sell does not fill, protection is "
                        "restored. Approval does not guarantee the order is accepted or filled."
                    )
                    if closes
                    else (
                        "COSMICTRADING will submit exactly this order and carry out only the "
                        "listed management actions, after re-checking risk immediately before "
                        "submission. Approval does not guarantee the order is accepted or filled."
                    )
                ),
            },
            "trade_code": code,
            "intent_id": intent_id,
            "packet_version": version,
            "account": {
                "alias": account.alias,
                "kind": account.kind,
                "venue": account.venue_key,
                "adapter": account.broker_adapter,
                "environment": env.code,
            },
            "mode": ctx.mode.value,
            "instrument": {
                "canonical_key": inst.canonical_key,
                "venue": inst.venue_key,
                "base": inst.base,
                "quote": inst.quote,
                "asset_class": inst.asset_class,
                "session": session_text(inst.asset_class),
            },
            "order": {
                "side": candidate.side,
                "effect": candidate.effect.value,
                "quantity": quantity,
                "order_type": candidate.order_type,
                "limit_price": price,
                "price_band": {"low": candidate.price_band_low, "high": candidate.price_band_high},
                "max_slippage_bps": candidate.max_slippage_bps,
                "time_in_force": candidate.time_in_force,
                "max_notional": notional,
                **({"closes": closes} if closes else {}),
            },
            "costs": {
                "currency": inst.quote,
                "estimated_fee": est_fee,
                "fee_bps": fee_bps,
                "fee_basis": (
                    f"ASSUMPTION: the owner's fee for {inst.venue_key} in the risk policy"
                    if own_fee
                    else "ASSUMPTION: the policy's general fee (no fee set for this venue)"
                ),
                "half_spread_bps_now": half_spread,
                "max_slippage_bps": candidate.max_slippage_bps,
                "funding_borrow": funding_text(inst.asset_class, inst.metadata),
                "note": "Free AI does not make execution free: fees, spread, slippage are real.",
            },
            "strategy": strategy or {"key": None, "name": "not recorded", "version": None},
            "risk": {
                "estimated_max_loss": est_max_loss,
                "estimated_max_loss_pct_of_equity": loss_pct,
                "assumptions": f"fill at limit, {terms.stop_fill}, fees charged twice",
                "warning": LOSS_WARNING,
                "position_after": post.get("position_after"),
                "concentration_pct_after": post.get("concentration_pct"),
                "limit_semantics": LIMIT_SEMANTICS,
            },
            "market_at_proposal": {
                "bid": m.bid,
                "ask": m.ask,
                "quote_ts": m.quote_ts,
                "label": m.quote_label.value,
                "cross_venue_deviation_bps": m.deviation_bps,
            },
            "thesis": candidate.thesis,
            "horizon": candidate.horizon,
            "evidence": [e.model_dump(mode="python") for e in candidate.evidence],
            "counterarguments": list(candidate.counterarguments),
            "red_team": candidate.red_team,
            "scenarios": [s.model_dump(mode="python") for s in candidate.scenarios],
            "invalidation": list(candidate.invalidation),
            "entry_conditions": candidate.entry_conditions,
            "reasons_to_wait": list(candidate.wait_or_no_trade_reasons),
            "management_plan": {
                "stop_loss": None
                if stop is None
                else {
                    "type": terms.stop_type,
                    "stop_price": stop.stop_price,
                    "limit_price": stop.limit_price,
                },
                "take_profit": None
                if plan.take_profit_price is None
                else {
                    "type": "monitored: at target bid, cancel the stop, then sell (IOC limit)",
                    "price": plan.take_profit_price,
                },
                "cancel_unfilled_after_seconds": plan.cancel_unfilled_after_seconds,
                "atomic_bracket": terms.bracket,
            },
            "prohibited_actions": list(PROHIBITED_ACTIONS),
            "checks": [
                {"code": r.code, "name": r.name, "status": r.status.value, "message": r.message}
                for r in decision.results
            ],
            "policy_version": policy_version,
            "created_at": created_at,
            "intent_expires_at": intent_expires_at,
            "approval_ttl_seconds": policy.approval_ttl_seconds,
        }
    )


async def strategy_of(conn: AsyncConnection, opportunity_id: uuid.UUID | None) -> dict[str, Any]:
    """The rule behind a proposal, for the approval card (directive Phase 3): the strategy and
    version of its research opportunity, or the operator for a manual proposal."""
    if opportunity_id is None:
        return {"key": None, "name": "manual proposal by the operator", "version": None}
    row = (
        await conn.execute(
            select(strategies.c.key, strategies.c.name, strategy_versions.c.version)
            .select_from(opportunities)
            .join(strategy_versions, strategy_versions.c.id == opportunities.c.strategy_version_id)
            .join(strategies, strategies.c.id == strategy_versions.c.strategy_id)
            .where(opportunities.c.id == opportunity_id)
        )
    ).first()
    if row is None:
        return {
            "key": None,
            "name": "research opportunity without a strategy rule",
            "version": None,
        }
    return {"key": row.key, "name": row.name, "version": row.version}


def packet_hash(packet: dict[str, Any]) -> str:
    return canonical_hash(packet)


async def create_candidate(
    conn: AsyncConnection,
    *,
    candidate: Candidate,
    actor: Actor,
    now: datetime,
    mandate_id: uuid.UUID | None = None,
    timeframe: str | None = None,
    venue_quote: VenueQuote | None = None,
    ttl: timedelta | None = None,
) -> ProposalResult:
    """``mandate_id``: proposed by the demo auto-trader under that mandate (ADR-030). It is
    recorded on the intent and no approval is requested from the owner; the mandate decides.
    ``timeframe``: the bars its rule ran on, recorded with it. ``venue_quote``: for a LIVE
    account, the venue's own book read just before (ADR-034), its check's only bid and ask.
    ``ttl``: how long the proposal lasts, instead of the policy's (a real-money daily proposal
    lasts through the owner's day). Internal only: never part of a user's candidate."""
    account = await get_account(conn, alias=candidate.account_alias)
    try:
        inst = await md.instrument_by_key(conn, candidate.instrument_key)
    except NotFoundError:
        raise ValidationFailedError(
            "unknown or ambiguous instrument; proposal is not executable"
        ) from None
    policy_version, policy = await active_policy(conn)
    authority = await get_state(conn)
    closes = await closed_entries(conn, candidate, account, inst) if candidate.closes else []
    if candidate.side == "SELL" and not candidate.closes:
        owners = await open_entries(conn, account.id, inst.id)
        if owners:
            raise ValidationFailedError(
                "this position belongs to "
                + ", ".join(r.trade_code for r in owners)
                + "; name them in closes so their protective stops are replaced, not oversold"
            )
    rules = InstrumentRulesView(
        inst.canonical_key,
        inst.tick_size,
        inst.lot_size,
        inst.min_qty,
        inst.max_qty,
        inst.min_notional,
    )
    norm = normalise(
        rules,
        candidate.side,
        candidate.quantity,
        candidate.limit_price,
        policy.rounding_tolerance_bps,
    )
    quantity, price = norm.quantity, norm.limit_price
    if not candidate.price_band_low <= price <= candidate.price_band_high:
        price = candidate.limit_price
    request = _request(candidate, quantity if quantity > ZERO else candidate.quantity, price)
    ctx = await build_context(
        conn,
        account=account,
        inst=inst,
        request=request,
        stage=Stage.PRE_APPROVAL,
        mode=authority.mode,
        policy_version=policy_version,
        now=now,
        venue_quote=venue_quote,
    )
    decision = evaluate(ctx, policy)
    problems = tuple(norm.problems) + tuple(f"{r.code}: {r.message}" for r in decision.blocking)
    executable = decision.passed and norm.ok
    intent_id = uuid7()
    code = trade_code(now.strftime("%Y%m%d"))
    expires = now + (ttl or timedelta(seconds=policy.intent_ttl_seconds))
    details = to_jsonable(
        {
            "evidence": [e.model_dump(mode="python") for e in candidate.evidence],
            "counterarguments": list(candidate.counterarguments),
            "red_team": candidate.red_team,
            "scenarios": [s.model_dump(mode="python") for s in candidate.scenarios],
            "invalidation": list(candidate.invalidation),
            "entry_conditions": candidate.entry_conditions,
            "reasons_to_wait": list(candidate.wait_or_no_trade_reasons),
            "requested": {"quantity": candidate.quantity, "limit_price": candidate.limit_price},
            "problems": list(problems),
            **({"closes": [c["intent_id"] for c in closes]} if closes else {}),
            **({"mandate_id": str(mandate_id)} if mandate_id is not None else {}),
            **({"timeframe": timeframe} if timeframe is not None else {}),
        }
    )
    await conn.execute(
        trade_intents.insert().values(
            id=intent_id,
            trade_code=code,
            account_id=account.id,
            instrument_id=inst.id,
            opportunity_id=candidate.opportunity_id,
            side=candidate.side,
            effect=candidate.effect.value,
            quantity=request.quantity,
            order_type=candidate.order_type,
            limit_price=request.limit_price,
            price_band_low=candidate.price_band_low,
            price_band_high=candidate.price_band_high,
            max_slippage_bps=candidate.max_slippage_bps,
            time_in_force=candidate.time_in_force,
            management_plan=to_jsonable(candidate.management_plan.model_dump(mode="python")),
            thesis=candidate.thesis,
            plain_summary=candidate.plain_summary,
            horizon=candidate.horizon,
            details=details,
            intent_expires_at=expires,
            created_by_kind="USER"
            if actor.type in (ActorType.USER, ActorType.TELEGRAM)
            else "WORKER",
            created_by=f"{actor.type.value}:{actor.id}",
            status="PROPOSED" if executable else "NON_EXECUTABLE",
            status_reason=None if executable else "; ".join(problems)[:2000],
            is_simulated=account.kind in SIMULATED_ACCOUNT_KINDS,
            created_at=now,
            updated_at=now,
        )
    )
    run_id = await record_risk_run(
        conn,
        ctx=ctx,
        decision=decision,
        account_id=account.id,
        intent_id=intent_id,
        order_id=None,
        now=now,
    )
    digest: str | None = None
    if executable:
        packet = build_packet(
            code=code,
            intent_id=intent_id,
            version=1,
            candidate=candidate,
            account=account,
            inst=inst,
            quantity=request.quantity,
            price=request.limit_price,
            ctx=ctx,
            decision=decision,
            policy_version=policy_version,
            policy=policy,
            created_at=now,
            intent_expires_at=expires,
            closes=closes,
            strategy=await strategy_of(conn, candidate.opportunity_id),
        )
        digest = packet_hash(packet)
        await conn.execute(
            approval_packets.insert().values(
                id=uuid7(),
                intent_id=intent_id,
                version=1,
                packet=packet,
                packet_hash=digest,
                risk_check_run_id=run_id,
                policy_version=policy_version,
                created_at=now,
                expires_at=expires,
            )
        )
    if executable and mandate_id is None:  # under a mandate, the auto-trader reports instead
        await notify_owners(
            conn,
            kind="approval_requested",
            now=now,
            dedup_key=f"approval:{intent_id}",
            payload={
                **account_fields(account.alias, account.kind, account.broker_adapter),
                "trade_code": code,
                "intent_id": intent_id,
                "summary": packet["plain_language"],
                "expires_at": expires,
            },
        )
    await record(
        conn,
        action="trade.proposed" if executable else "trade.non_executable",
        actor=actor,
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=intent_id,
        payload={
            "trade_code": code,
            "packet_hash": digest,
            "problems": list(problems),
            "risk_check_run": run_id,
            **account_fields(account.alias, account.kind, account.broker_adapter),
        },
    )
    return ProposalResult(
        intent_id, code, "PROPOSED" if executable else "NON_EXECUTABLE", decision, problems, digest
    )


async def latest_packet(conn: AsyncConnection, intent_id: uuid.UUID) -> Any:
    row = (
        await conn.execute(
            select(approval_packets)
            .where(approval_packets.c.intent_id == intent_id)
            .order_by(approval_packets.c.version.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        raise ConflictError("this proposal has no approval packet (it is not executable)")
    return row


async def set_intent_status(
    conn: AsyncConnection,
    intent_id: uuid.UUID,
    status: str,
    reason: str | None,
    now: datetime,
    *,
    expected: tuple[str, ...] | None = None,
) -> bool:
    q = update(trade_intents).where(trade_intents.c.id == intent_id)
    if expected is not None:
        q = q.where(trade_intents.c.status.in_(expected))
    result = await conn.execute(
        q.values(status=status, status_reason=(reason or None) and reason[:2000], updated_at=now)
    )
    return result.rowcount == 1


async def get_intent(conn: AsyncConnection, intent_id: uuid.UUID, *, lock: bool = False) -> Any:
    q = select(trade_intents).where(trade_intents.c.id == intent_id)
    if lock:
        q = q.with_for_update()
    row = (await conn.execute(q)).first()
    if row is None:
        raise NotFoundError("unknown trade intent")
    return row


async def repropose(
    conn: AsyncConnection, *, intent_id: uuid.UUID, actor: Actor, now: datetime
) -> ProposalResult:
    """Re-run pre-approval checks for an intent that needs re-approval; creates packet v+1.

    The intent's material terms are immutable: if they no longer fit the market, the
    checks fail and the operator must create a new proposal instead.
    """
    intent = await get_intent(conn, intent_id, lock=True)
    if intent.status != "REQUIRES_REAPPROVAL":
        raise ConflictError(
            f"intent is {intent.status}; only REQUIRES_REAPPROVAL can be re-proposed"
        )
    if intent.intent_expires_at <= now:
        await set_intent_status(conn, intent_id, "EXPIRED", "intent expired", now)
        raise ConflictError("the trade intent has expired; create a new proposal")
    old = await latest_packet(conn, intent_id)
    candidate = Candidate.model_validate(
        {
            "account_alias": old.packet["account"]["alias"],
            "instrument_key": old.packet["instrument"]["canonical_key"],
            "side": intent.side,
            "effect": intent.effect,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "price_band_low": intent.price_band_low,
            "price_band_high": intent.price_band_high,
            "max_slippage_bps": intent.max_slippage_bps,
            "time_in_force": intent.time_in_force,
            "horizon": intent.horizon,
            "thesis": intent.thesis,
            "plain_summary": intent.plain_summary,
            "evidence": intent.details["evidence"],
            "counterarguments": intent.details["counterarguments"],
            "red_team": intent.details["red_team"],
            "scenarios": intent.details["scenarios"],
            "invalidation": intent.details["invalidation"],
            "entry_conditions": intent.details["entry_conditions"],
            "wait_or_no_trade_reasons": intent.details.get("reasons_to_wait", []),
            "management_plan": intent.management_plan,
            "closes": intent.details.get("closes", []),
        }
    )
    account = await get_account(conn, account_id=intent.account_id)
    inst = await md.instrument_by_id(conn, intent.instrument_id)
    policy_version, policy = await active_policy(conn)
    authority = await get_state(conn)
    closes = await closed_entries(conn, candidate, account, inst) if candidate.closes else []
    request = _request(candidate, intent.quantity, intent.limit_price)
    ctx = await build_context(
        conn,
        account=account,
        inst=inst,
        request=request,
        stage=Stage.PRE_APPROVAL,
        mode=authority.mode,
        policy_version=policy_version,
        now=now,
    )
    # This intent itself is pending; do not count it as a duplicate of itself.
    from dataclasses import replace

    ctx = replace(
        ctx,
        account=replace(
            ctx.account,
            active_intents_same_instrument_side=max(
                0, ctx.account.active_intents_same_instrument_side - 1
            ),
        ),
    )
    decision = evaluate(ctx, policy)
    run_id = await record_risk_run(
        conn,
        ctx=ctx,
        decision=decision,
        account_id=account.id,
        intent_id=intent_id,
        order_id=None,
        now=now,
    )
    problems = tuple(f"{r.code}: {r.message}" for r in decision.blocking)
    if not decision.passed:
        await record(
            conn,
            action="trade.repropose_blocked",
            actor=actor,
            occurred_at=now,
            entity_type="trade_intent",
            entity_id=intent_id,
            payload={
                "problems": list(problems),
                **account_fields(account.alias, account.kind, account.broker_adapter),
            },
        )
        return ProposalResult(intent_id, intent.trade_code, intent.status, decision, problems, None)
    version = int(old.version) + 1
    packet = build_packet(
        code=intent.trade_code,
        intent_id=intent_id,
        version=version,
        candidate=candidate,
        account=account,
        inst=inst,
        quantity=intent.quantity,
        price=intent.limit_price,
        ctx=ctx,
        decision=decision,
        policy_version=policy_version,
        policy=policy,
        created_at=now,
        intent_expires_at=intent.intent_expires_at,
        closes=closes,
        strategy=await strategy_of(conn, intent.opportunity_id),
    )
    digest = packet_hash(packet)
    await conn.execute(
        approval_packets.insert().values(
            id=uuid7(),
            intent_id=intent_id,
            version=version,
            packet=packet,
            packet_hash=digest,
            risk_check_run_id=run_id,
            policy_version=policy_version,
            created_at=now,
            expires_at=intent.intent_expires_at,
        )
    )
    await set_intent_status(conn, intent_id, "PROPOSED", None, now)
    await notify_owners(
        conn,
        kind="approval_requested",
        now=now,
        dedup_key=f"approval:{intent_id}:v{version}",
        payload={
            **account_fields(account.alias, account.kind, account.broker_adapter),
            "trade_code": intent.trade_code,
            "intent_id": intent_id,
            "summary": packet["plain_language"],
        },
    )
    await record(
        conn,
        action="trade.reproposed",
        actor=actor,
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=intent_id,
        payload={
            "packet_version": version,
            "packet_hash": digest,
            **account_fields(account.alias, account.kind, account.broker_adapter),
        },
    )
    return ProposalResult(intent_id, intent.trade_code, "PROPOSED", decision, (), digest)
