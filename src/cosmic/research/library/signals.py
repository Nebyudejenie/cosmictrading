"""Apply switched-on library rules: their entries and exits become proposals for approval.

Once a day, after the daily bar closes, every rule the owner switched on is computed on its
research series. When its position turns from flat to long it proposes a paper BUY on the
trading instrument; when it turns flat it proposes a SELL of exactly what this rule bought.
Nothing executes without the owner's approval of the exact terms.

Differences from the tested rule, stated in every proposal:
- The risk policy requires a protective stop on every entry. The library rules have none, so
  the stop is a disaster stop 3×ATR(20) below entry — it can make results differ from the
  backtest.
- Partial-weight rules propose entries and exits only, sized by the risk policy times the
  rule's weight; their in-between rebalances are not proposed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.clock import Clock
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable_lenient
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_market import market_events
from cosmic.db.schema_research import missions, opportunities, strategies, strategy_versions
from cosmic.db.schema_trading import accounts, positions
from cosmic.market import data as md
from cosmic.research import opportunities as opp
from cosmic.research.library import store, ta
from cosmic.research.library.catalog import BY_KEY, Spec
from cosmic.research.library.engine import Market, align
from cosmic.trading.accounts import get_account
from cosmic.trading.context_builder import account_state
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate, create_candidate, entry_available
from cosmic.trading.sizing import size_long

ACTOR = Actor.worker("strategy_library")
STOP_ATR = Decimal(3)
MAX_BAR_AGE = timedelta(days=2)
BAND = Decimal("0.01")  # ±1 %: room for a human-paced approval


@dataclass(frozen=True)
class Signal:
    action: str  # ENTER | EXIT
    previous: float
    weight: float
    bar_date: str
    atr_fraction: float | None


def compute(spec: Spec, asset: store.ResearchAsset, markets: dict[str, Market]) -> Signal | None:
    m = markets[asset.research_key]
    if spec.kind == "PANEL":
        panel = align([markets[a.research_key] for a in store.ASSETS])
        assert spec.panel_fn is not None
        weights = spec.panel_fn(panel, spec.params)[asset.research_key]
        m = panel.markets[asset.research_key]
    else:
        assert spec.fn is not None
        weights = spec.fn(m, spec.params)
    if len(weights) < 2:
        return None
    prev, last = weights[-2], weights[-1]
    a = ta.atr(m.high, m.low, m.close, 20)[-1]
    atr_fraction = a / m.close[-1] if a is not None and m.close[-1] > 0 else None
    if prev == 0.0 and last > 0.0:
        return Signal("ENTER", prev, last, m.dates[-1].isoformat(), atr_fraction)
    if last == 0.0:
        # Flat — on the day it turns flat, and on later days too (a failed or unapproved exit
        # is proposed again, once per daily bar, while the rule still holds something).
        return Signal("EXIT", prev, last, m.dates[-1].isoformat(), atr_fraction)
    return None


async def run(db: Database, clock: Clock) -> list[str]:
    now = clock.now()
    done: list[str] = []
    async with db.connect() as conn:
        rules = await store.active(conn)
        if not rules:
            return done
        markets: dict[str, Market] = {}
        for a in store.ASSETS:
            mk = await store.load_market(conn, a.research_key)
            if mk is not None:
                markets[a.research_key] = mk
    for row in rules:
        spec = BY_KEY.get(row.strategy_key)
        if spec is None:
            continue
        async with db.transaction() as conn:
            inst = await md.instrument_by_id(conn, row.instrument_id)
            asset = store.asset_for_trade(inst.canonical_key)
            needed = (
                [a.research_key for a in store.ASSETS]
                if spec.kind == "PANEL"
                else [asset.research_key]
            )
            if any(k not in markets for k in needed):
                continue
            last_bar = max(markets[k].dates[-1] for k in needed)
            if now.date() - last_bar > MAX_BAR_AGE:
                continue  # research series stale: never act on old data
            sig = compute(spec, asset, markets)
            if sig is None:
                continue
            if sig.action == "EXIT":
                acct = await _paper_account(conn)
                entries = await rule_entries(conn, spec, acct, inst.id) if acct is not None else []
                if not entries or await _exit_pending(conn, entries):
                    continue  # nothing held by this rule, or its exit is already in progress
            event_id = await _record_event(conn, spec, inst, sig, now)
            if event_id is None:
                continue  # already handled for this bar
            outcome = await _propose(conn, spec, inst, asset, sig, event_id, now)
            done.append(f"{spec.key}:{inst.canonical_key}:{sig.action}:{outcome}")
    return done


async def _paper_account(conn: AsyncConnection) -> uuid.UUID | None:
    row = (
        await conn.execute(
            select(accounts.c.id)
            .where(accounts.c.status == "ACTIVE", accounts.c.kind == "PAPER")
            .order_by(accounts.c.created_at)
            .limit(1)
        )
    ).first()
    return None if row is None else uuid.UUID(str(row.id))


async def _exit_pending(conn: AsyncConnection, entries: list[tuple[uuid.UUID, Decimal]]) -> bool:
    """An exit proposal for any of these entries is open, approved or executing."""
    ids = {str(e) for e, _ in entries}
    rows = await conn.execute(
        select(trade_intents.c.details).where(
            trade_intents.c.side == "SELL",
            trade_intents.c.status.in_(
                ["PROPOSED", "APPROVED", "EXECUTING", "REQUIRES_REAPPROVAL"]
            ),
        )
    )
    return any(ids & set((r.details or {}).get("closes", [])) for r in rows)


async def _record_event(
    conn: AsyncConnection, spec: Spec, inst: md.InstrumentView, sig: Signal, now: datetime
) -> uuid.UUID | None:
    eid = uuid7()
    result = await conn.execute(
        pg_insert(market_events)
        .values(
            id=eid,
            instrument_id=inst.id,
            kind="LIBRARY_SIGNAL",
            detected_at=now,
            bar_time=datetime.fromisoformat(sig.bar_date + "T00:00:00+00:00"),
            detail=to_jsonable_lenient(
                {
                    "strategy": spec.key,
                    "action": sig.action,
                    "weight_before": sig.previous,
                    "weight_after": sig.weight,
                    "bar_date": sig.bar_date,
                }
            ),
            dedup_key=f"LIBRARY_SIGNAL:{spec.key}:{inst.canonical_key}:{sig.bar_date}",
            status="PROCESSED",  # handled here, not by the AI research missions
        )
        .on_conflict_do_nothing(index_elements=[market_events.c.dedup_key])
    )
    return eid if result.rowcount else None


async def _mission(
    conn: AsyncConnection,
    spec: Spec,
    inst: md.InstrumentView,
    event_id: uuid.UUID,
    version_id: uuid.UUID,
    now: datetime,
) -> uuid.UUID:
    mid = uuid7()
    await conn.execute(
        missions.insert().values(
            id=mid,
            kind="library_signal",
            trigger_event_id=event_id,
            instrument_id=inst.id,
            status="COMPLETED",
            status_reason=f"deterministic signal from library rule {spec.key}",
            idempotency_key=f"library:{event_id}",
            ai_call_budget=0,
            expires_at=now + timedelta(days=1),
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
    )
    oid = await opp.create(
        conn, mission_id=mid, instrument_id=inst.id, now=now, strategy_version_id=version_id
    )
    return oid


async def _propose(
    conn: AsyncConnection,
    spec: Spec,
    inst: md.InstrumentView,
    asset: store.ResearchAsset,
    sig: Signal,
    event_id: uuid.UUID,
    now: datetime,
) -> str:
    version_id = await store.ensure_spec(conn, spec, now)
    evaluation = await store.evaluation_for(conn, spec, inst.canonical_key)
    oid = await _mission(conn, spec, inst, event_id, version_id, now)
    ev = (evaluation.result if evaluation is not None else {}) or {}
    tier = evaluation.tier if evaluation is not None else "UNKNOWN"
    await opp.advance(
        conn,
        oid,
        "RESEARCH",
        f"library rule {spec.key} signalled {sig.action}",
        now,
        direction="LONG" if sig.action == "ENTER" else None,
    )
    await opp.advance(
        conn,
        oid,
        "CHALLENGED",
        f"evaluation tier {tier}; " + "; ".join(ev.get("reasons", [])[:3])[:400],
        now,
    )
    acct_row = (
        await conn.execute(
            select(accounts.c.id)
            .where(accounts.c.status == "ACTIVE", accounts.c.kind == "PAPER")
            .order_by(accounts.c.created_at)
            .limit(1)
        )
    ).first()
    q = await md.latest_quote(conn, inst.id, now)
    problem = None
    if acct_row is None:
        problem = "no active paper account"
    elif q.label is not DataLabel.DELAYED or q.bid is None or q.ask is None:
        problem = f"no fresh quote for {inst.canonical_key} ({q.label.value})"
    elif sig.action == "ENTER" and sig.atr_fraction is None:
        problem = "volatility unavailable for the protective stop"
    if problem:
        await opp.advance(conn, oid, "NO_TRADE", problem, now)
        return "NO_TRADE"
    assert acct_row is not None and q.bid is not None and q.ask is not None
    account = await get_account(conn, account_id=acct_row.id)
    if sig.action == "ENTER":
        candidate = await _entry(
            conn, spec, inst, asset, sig, ev, tier, account, q.bid, q.ask, oid, now
        )
    else:
        candidate = await _exit(conn, spec, inst, sig, ev, tier, account, q.bid, oid, now)
    if candidate is None:
        await opp.advance(
            conn,
            oid,
            "NO_TRADE",
            "nothing to trade (size rounds to zero or no position held by this rule)",
            now,
        )
        return "NO_TRADE"
    await opp.advance(
        conn, oid, "QUALIFIED", "typed candidate built from the library evaluation", now
    )
    result = await create_candidate(conn, candidate=candidate, actor=ACTOR, now=now)
    if result.status == "PROPOSED":
        await opp.advance(
            conn,
            oid,
            "APPROVAL_REQUIRED",
            "deterministic pre-checks passed",
            now,
            intent_id=result.intent_id,
        )
    else:
        await opp.advance(
            conn,
            oid,
            "NON_EXECUTABLE",
            "; ".join(result.problems)[:400],
            now,
            intent_id=result.intent_id,
        )
    await record(
        conn,
        action="research.library_signal",
        actor=ACTOR,
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=result.intent_id,
        payload={
            "strategy": spec.key,
            "action": sig.action,
            "tier": tier,
            "instrument": inst.canonical_key,
            "status": result.status,
        },
    )
    return result.status


def _tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(ROUND_DOWN) * tick


def _evidence_text(spec: Spec, ev: dict[str, Any], tier: str) -> str:
    full, val = ev.get("full", {}), ev.get("validation", {})
    return (
        f"Rule {spec.number} '{spec.name}': {spec.rule} Source: {spec.source}. "
        f"Tested once with published parameters on {ev.get('series', '?')} under "
        f"{ev.get('costs', {}).get('name', '?')} costs (ASSUMPTION): tier {tier}; timing Sharpe "
        f"{ev.get('timing_sharpe_annualised')} (validation {ev.get('timing_sharpe_validation')}); "
        f"deflated-Sharpe probability {ev.get('deflated_sharpe_probability')}; CAGR "
        f"{full.get('cagr')}, max drawdown {full.get('max_drawdown')}, "
        f"{full.get('trades')} trades; "
        f"validation Sharpe {val.get('sharpe')}. Historical simulation, not evidence of future "
        "profit."
    )


def _common(spec: Spec, ev: dict[str, Any], tier: str) -> dict[str, Any]:
    reasons = [r for r in ev.get("reasons", []) if r != "passed every test"]
    counter = [
        *reasons[:4],
        "The rule was one of 100 tested; the best of many backtests looks "
        "better than it will perform.",
    ]
    return {
        "counterarguments": [c[:300] for c in counter],
        "red_team": (
            "Trend and momentum rules give back gains in sideways markets and react late to "
            "reversals; mean-reversion rules suffer in strong trends. Paper fills are simulated."
        ),
        "scenarios": [
            {"name": "Rule works", "description": "Price moves in the signalled direction."},
            {"name": "Whipsaw", "description": "The signal reverses quickly at a small loss."},
        ],
        "invalidation": [f"The rule '{spec.name}' changes its signal", "Protective stop is hit"],
        "horizon": "weeks to months"
        if "monthly" in str(spec.params) or "MOMENTUM" in spec.family
        else "days to weeks",
        "tier_note": (
            "WATCH: this rule did not pass the significance test; this is a forward test, not a "
            "proven edge."
            if tier == "WATCH"
            else f"Tier {tier}."
        ),
    }


async def _entry(
    conn: AsyncConnection,
    spec: Spec,
    inst: md.InstrumentView,
    asset: store.ResearchAsset,
    sig: Signal,
    ev: dict[str, Any],
    tier: str,
    account: Any,
    bid: Decimal,
    ask: Decimal,
    oid: uuid.UUID,
    now: datetime,
) -> Candidate | None:
    if inst.tick_size is None or inst.lot_size is None or sig.atr_fraction is None:
        return None
    tick = inst.tick_size
    state = await account_state(conn, account, now)
    _, policy = await active_policy(conn)
    if state.equity is None or state.cash_available is None:
        return None
    entry = _tick(ask * Decimal("1.001"), tick)  # marketable limit: ask + 0.1 %
    atr = Decimal(str(round(sig.atr_fraction, 8)))
    stop = _tick(entry * (1 - STOP_ATR * atr), tick)
    stop_limit = _tick(stop * Decimal("0.995"), tick)
    if stop <= 0 or stop_limit <= 0:
        return None
    size = size_long(
        equity=state.equity,
        entry=entry,
        stop=stop,
        risk_per_trade_pct=policy.risk_per_trade_pct,
        uncertainty_haircut_pct=policy.uncertainty_haircut_pct,
        lot_size=inst.lot_size,
        min_qty=inst.min_qty,
        min_notional=inst.min_notional,
        max_order_notional=policy.max_order_notional,
        position_headroom_notional=policy.max_position_notional,
        cash_available=state.cash_available,
        fee_bps=policy.fee_bps_assumption,
    )
    qty = (size.quantity * Decimal(str(round(sig.weight, 4))) / inst.lot_size).to_integral_value(
        ROUND_DOWN
    ) * inst.lot_size
    if qty <= ZERO or (inst.min_qty is not None and qty < inst.min_qty):
        return None
    held = await conn.scalar(
        select(positions.c.quantity).where(
            positions.c.account_id == account.id, positions.c.instrument_id == inst.id
        )
    )
    c = _common(spec, ev, tier)
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": inst.canonical_key,
            "side": "BUY",
            "effect": "INCREASE" if held and held > 0 else "OPEN",
            "quantity": qty,
            "limit_price": entry,
            "price_band_low": _tick(bid * (1 - BAND), tick),
            "price_band_high": _tick(ask * (1 + BAND), tick),
            "max_slippage_bps": Decimal(15),
            "time_in_force": "GTC",
            "horizon": c["horizon"],
            "thesis": (
                f"{_evidence_text(spec, ev, tier)} Signal: at the close of {sig.bar_date} the rule "
                f"moved from {sig.previous:.0%} to {sig.weight:.0%} of the position. The "
                f"protective stop ({STOP_ATR}×ATR(20) below entry) is a risk-policy "
                "requirement and "
                "is not part of the tested rule."
            )[:4000],
            "plain_summary": (
                f"Paper buy of {asset.asset} because rule {spec.number} '{spec.name}' turned long. "
                f"{c['tier_note']}"
            )[:600],
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{inst.canonical_key}",
                    "summary": f"bid {bid} / ask {ask} (DELAYED)",
                    "source_ts": now,
                },
                {
                    "kind": "BACKTEST",
                    "ref": f"strategy_evaluation:{ev.get('strategy')}:{tier}",
                    "summary": "library evaluation: historical simulation with assumed costs",
                },
                {
                    "kind": "INDICATOR",
                    "ref": f"library:{spec.key}:{sig.bar_date}",
                    "summary": f"{spec.name}: position {sig.previous:.0%} → {sig.weight:.0%}",
                },
            ],
            "counterarguments": c["counterarguments"],
            "red_team": c["red_team"],
            "scenarios": c["scenarios"],
            "invalidation": c["invalidation"],
            "entry_conditions": f"Limit buy at or below {entry} (ask + 0.1 %)",
            "opportunity_id": oid,
            "management_plan": {
                "stop_loss": {"stop_price": stop, "limit_price": stop_limit},
                "cancel_unfilled_after_seconds": 21600,
            },
        }
    )


async def rule_entries(
    conn: AsyncConnection, spec: Spec, account_id: uuid.UUID, instrument_id: uuid.UUID
) -> list[tuple[uuid.UUID, Decimal]]:
    """This rule's filled entries that still hold something, oldest first.

    Each entry's holding nets its own stop, take-profit and exit orders, so what the rule
    holds is exactly what it bought and has not sold.
    """
    rows = await conn.execute(
        select(trade_intents.c.id)
        .join(opportunities, opportunities.c.id == trade_intents.c.opportunity_id)
        .join(strategy_versions, strategy_versions.c.id == opportunities.c.strategy_version_id)
        .join(strategies, strategies.c.id == strategy_versions.c.strategy_id)
        .where(
            strategies.c.key == spec.key,
            trade_intents.c.account_id == account_id,
            trade_intents.c.instrument_id == instrument_id,
            trade_intents.c.side == "BUY",
            trade_intents.c.status.in_(["EXECUTING", "EXECUTED"]),
        )
        .order_by(trade_intents.c.created_at)
    )
    out = []
    for r in list(rows):
        held = await entry_available(conn, r.id)
        if held > ZERO:
            out.append((r.id, held))
    return out


async def rule_holding(
    conn: AsyncConnection, spec: Spec, account_id: uuid.UUID, instrument_id: uuid.UUID
) -> Decimal:
    """Quantity this rule bought and has not yet sold."""
    return sum((h for _, h in await rule_entries(conn, spec, account_id, instrument_id)), ZERO)


async def _exit(
    conn: AsyncConnection,
    spec: Spec,
    inst: md.InstrumentView,
    sig: Signal,
    ev: dict[str, Any],
    tier: str,
    account: Any,
    bid: Decimal,
    oid: uuid.UUID,
    now: datetime,
) -> Candidate | None:
    if inst.tick_size is None:
        return None
    entries = await rule_entries(conn, spec, account.id, inst.id)
    qty = sum((h for _, h in entries), ZERO)
    held = await conn.scalar(
        select(positions.c.quantity).where(
            positions.c.account_id == account.id, positions.c.instrument_id == inst.id
        )
    )
    held = Decimal(str(held or 0))
    if qty <= ZERO or qty > held:
        return None  # nothing held by this rule, or the books disagree: never oversell
    tick = inst.tick_size
    limit = _tick(bid * Decimal("0.999"), tick)  # marketable: bid − 0.1 %
    c = _common(spec, ev, tier)
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": inst.canonical_key,
            "side": "SELL",
            "effect": "CLOSE" if qty >= held else "REDUCE",
            "quantity": qty,
            "limit_price": limit,
            "price_band_low": _tick(bid * (1 - BAND), tick),
            "price_band_high": _tick(bid * (1 + BAND), tick),
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",  # replaces the stop: fill now or the stop returns
            "horizon": c["horizon"],
            "thesis": (
                f"Exit: at the close of {sig.bar_date} rule {spec.number} '{spec.name}' turned "
                f"flat. This sells only what the rule bought ({qty}). "
                f"{_evidence_text(spec, ev, tier)}"
            )[:4000],
            "plain_summary": (
                f"Paper sale of {qty} because rule {spec.number} '{spec.name}' turned flat."
            )[:600],
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{inst.canonical_key}",
                    "summary": f"bid {bid} (DELAYED)",
                    "source_ts": now,
                },
                {
                    "kind": "INDICATOR",
                    "ref": f"library:{spec.key}:{sig.bar_date}",
                    "summary": f"{spec.name}: position {sig.previous:.0%} → 0%",
                },
            ],
            "counterarguments": [
                "The move may continue after the exit.",
                *c["counterarguments"][:2],
            ],
            "red_team": "Exiting on a rule signal can sell near a short-term low.",
            "scenarios": c["scenarios"],
            "invalidation": ["Not applicable to an exit"],
            "entry_conditions": f"Limit sell at or above {limit} (bid − 0.1 %)",
            "opportunity_id": oid,
            # Approving this exit cancels these entries' protective stops first (R17).
            "closes": [intent_id for intent_id, _ in entries],
        }
    )
