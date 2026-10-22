"""The demo auto-trader (ADR-030): one library rule on 1-hour bars, under an active mandate.

Every few minutes, for each active demo mandate and each of its instruments, the rule is
computed on the latest completed hourly bars of the instrument's signal series. A move from
flat to long proposes a buy sized so that the loss at the stop is at most the mandate's
maximum loss; a move to flat proposes selling exactly what the rule bought. The mandate
approves or refuses each proposal (cosmic.trading.mandates): this module only proposes. Orders
are then sent, protected, synced and reconciled exactly as for an approval by the owner.

Differences from the tested rule, stated in every proposal:
- The rule was evaluated on daily bars; on 1-hour bars it is a forward test on demo money.
- The protective stop is 3×ATR(20) of the hourly bars below entry; where the venue holds the
  stop and fills it at market (HFM), there is no separate stop-limit price.
- The signal may come from a related market when the venue has no stored bars (HFM gold and
  bitcoin follow Binance PAXG/USDT and BTC/USDT).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.clock import Clock
from cosmic.core.errors import NotFoundError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable_lenient
from cosmic.core.labels import DataLabel
from cosmic.core.logs import get_logger
from cosmic.core.money import BPS, ZERO
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_market import market_events
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.research import opportunities as opp
from cosmic.research.library import store, ta
from cosmic.research.library.catalog import BY_KEY, Spec
from cosmic.research.library.engine import Market
from cosmic.research.library.signals import _exit_pending, _mission, _tick, rule_entries
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.modes import account_fields, friendly
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate, create_candidate

log = get_logger(__name__)
ACTOR = Actor.worker("auto_trader")
MIN_BARS = 60
MAX_BAR_LAG = timedelta(hours=2)  # the newest completed hourly bar must be this recent
STOP_ATR = Decimal(3)
BAND = Decimal("0.005")  # ±0.5 %: the mandate approves at once
CANCEL_UNFILLED_AFTER = 900  # a marketable entry that has not filled in 15 minutes is cancelled
# Venues that hold the stop with the position and fill it at market (no stop-limit price).
MARKET_STOP_ADAPTERS = frozenset({"hfm_mt5_demo"})
OPEN = ("PROPOSED", "APPROVED", "EXECUTING", "REQUIRES_REAPPROVAL")
HEADS_UP_ATR = 0.5  # "a trade may come soon": within half an hourly ATR of the breakout level


def _head(m: mandates.Mandate) -> str:
    """How the proposal introduces itself: the owner is asked first, or it is automatic."""
    if m.mode == "ASK":
        return "TIME TO TRADE (your auto-trader asks first)"
    return "AUTO (demo mandate)"


def watch(spec: Spec, market: Market) -> tuple[float, float, float] | None:
    """(breakout level for the next bar, last close, ATR) of a breakout rule, or None.

    The rule buys when a close goes above the highest high of the last ``entry`` bars."""
    n = int(spec.params.get("entry", 20))
    if len(market.high) < n + 1:
        return None
    atr = ta.atr(market.high, market.low, market.close, 20)[-1]
    if atr is None or atr <= 0:
        return None
    return max(market.high[-n:]), market.close[-1], atr


async def _heads_up(
    conn: AsyncConnection,
    m: mandates.Mandate,
    spec: Spec,
    key: str,
    account: Account,
    market: Market,
    bar_open: datetime,
    now: datetime,
) -> None:
    """Tell the owner when a market comes within half an hour's typical move (half an ATR) of
    its breakout level: a trade may come with the next hourly close. At most once per market
    every six hours."""
    w = watch(spec, market)
    if w is None:
        return
    level, close, atr = w
    distance = level - close
    if not 0 < distance <= HEADS_UP_ATR * atr:
        return
    bucket = f"{bar_open:%Y%m%d}{bar_open.hour // 6}"
    then = "it will ask you to trade" if m.mode == "ASK" else "your auto-trader will buy"
    await notify_owners(
        conn,
        kind="auto_trade_soon",
        now=now,
        dedup_key=f"auto_soon:{m.id}:{key}:{bucket}",
        payload={
            **account_fields(account.alias, account.kind, account.broker_adapter),
            "text": (
                f"Heads-up: {friendly(key)} is {distance:,.2f} below its breakout "
                f"level ({level:,.2f}). If an hourly close goes above it, {then} (loss at the "
                f"stop at most {m.max_loss_per_trade:,.2f})."
            ),
        },
    )


async def load_hourly(conn: AsyncConnection, key: str, limit: int = 500) -> Market | None:
    """The latest ``limit`` completed 1-hour bars of one instrument, oldest first (analytics
    only, ADR-007)."""
    try:
        inst = await md.instrument_by_key(conn, key)
    except NotFoundError:
        return None
    bars = await md.complete_bars(conn, inst.id, "1h", limit=limit)
    if len(bars) < MIN_BARS:
        return None
    return Market(
        key,
        tuple(b.open_time for b in bars),
        tuple(float(b.open) for b in bars),
        tuple(float(b.high) for b in bars),
        tuple(float(b.low) for b in bars),
        tuple(float(b.close) for b in bars),
        tuple(float(b.volume) for b in bars),
    )


def size_for_max_loss(
    *,
    max_loss: Decimal,
    entry: Decimal,
    stop_limit: Decimal,
    fee_bps: Decimal,
    lot_size: Decimal,
    min_qty: Decimal | None,
    max_order_notional: Decimal,
) -> tuple[Decimal, Decimal]:
    """(quantity, loss per unit) so that quantity × (entry − stop-limit) plus fees on both sides
    stays within ``max_loss`` — the same estimate the approval packet shows. The quantity is
    zero when even the smallest trade would risk more."""
    per_unit = entry - stop_limit + 2 * entry * fee_bps / BPS
    if per_unit <= ZERO:
        return ZERO, per_unit
    qty = (max_loss / per_unit / lot_size).to_integral_value(ROUND_DOWN) * lot_size
    cap = (max_order_notional / entry / lot_size).to_integral_value(ROUND_DOWN) * lot_size
    qty = min(qty, cap)
    if min_qty is not None and qty < min_qty:
        return ZERO, per_unit
    return qty, per_unit


async def run(db: Database, clock: Clock) -> list[str]:
    now = clock.now()
    async with db.transaction() as conn:
        await mandates.expire_due(conn, now)
        await mandates.stop_due(conn, now)
    async with db.connect() as conn:
        active = await mandates.active_mandates(conn, now)
    done: list[str] = []
    for m in active:
        spec = BY_KEY.get(m.strategy_key)
        if spec is None or spec.fn is None:
            continue
        for key in m.instruments:
            try:  # one instrument's failure never stops the others
                async with db.transaction() as conn:
                    outcome = await _one(conn, m, spec, key, now)
            except Exception as exc:
                log.warning(
                    "auto_trader_failed",
                    mandate=str(m.id),
                    instrument=key,
                    error=scrub_text(str(exc))[:300],
                )
                continue
            if outcome:
                done.append(f"{key}:{outcome}")
    return done


async def _one(
    conn: AsyncConnection, m: mandates.Mandate, spec: Spec, key: str, now: datetime
) -> str | None:
    inst = await md.instrument_by_key(conn, key)
    series = mandates.AUTO_SIGNAL_SERIES[key]
    market = await load_hourly(conn, series)
    if market is None:
        return None
    bar_open = market.dates[-1]
    if not isinstance(bar_open, datetime) or now - (bar_open + timedelta(hours=1)) > MAX_BAR_LAG:
        return None  # stale series: never act on old data
    assert spec.fn is not None
    weights = spec.fn(market, spec.params)
    if len(weights) < 2:
        return None
    prev, last = weights[-2], weights[-1]
    account = await get_account(conn, account_id=m.account_id)
    entries = await rule_entries(conn, spec, account.id, inst.id)
    if prev == 0.0 and last > 0.0:
        if entries or await _entry_open(conn, account.id, inst.id, m.id):
            return None  # already in, or an entry is on its way
        action = "ENTER"
    elif last == 0.0 and entries:
        if await _exit_pending(conn, entries):
            return None
        action = "EXIT"
    else:
        if last == 0.0 and not entries:
            await _heads_up(conn, m, spec, key, account, market, bar_open, now)
        return None
    bar = bar_open.isoformat()
    event_id = await _record_event(conn, m, spec, inst, action, bar_open, now)
    if event_id is None:
        return None  # this bar was already handled
    version_id = await store.ensure_spec(conn, spec, now)
    oid = await _mission(conn, spec, inst, event_id, version_id, now)
    await opp.advance(
        conn,
        oid,
        "RESEARCH",
        f"auto-trader: rule {spec.key} on 1-hour bars signalled {action}",
        now,
        direction="LONG" if action == "ENTER" else None,
    )
    await opp.advance(
        conn, oid, "CHALLENGED", "forward test on 1-hour bars (evaluated on daily bars)", now
    )
    q = await md.latest_quote(conn, inst.id, now)
    candidate: Candidate | None = None
    if q.label not in (DataLabel.LIVE, DataLabel.DELAYED) or q.bid is None or q.ask is None:
        why = f"no fresh price for {key} ({q.label.value})"
    elif action == "ENTER":
        atr = ta.atr(market.high, market.low, market.close, 20)[-1]
        candidate, why = await _entry(
            conn, m, spec, inst, account, series, q.bid, q.ask, atr, bar, oid, now
        )
    else:
        candidate, why = await _exit(conn, m, spec, inst, account, q.bid, bar, oid, now)
    if candidate is None:
        await opp.advance(conn, oid, "NO_TRADE", why[:400], now)
        await _skipped(conn, account, key, action, why, event_id, now)
        return "NO_TRADE"
    await opp.advance(conn, oid, "QUALIFIED", "auto-trader candidate under the demo mandate", now)
    result = await create_candidate(
        conn, candidate=candidate, actor=ACTOR, now=now, mandate_id=m.id
    )
    if result.status != "PROPOSED":
        problems = "; ".join(result.problems)
        await opp.advance(
            conn, oid, "NON_EXECUTABLE", problems[:400], now, intent_id=result.intent_id
        )
        await _skipped(conn, account, key, action, f"risk checks: {problems}", event_id, now)
        return "NON_EXECUTABLE"
    await opp.advance(
        conn,
        oid,
        "APPROVAL_REQUIRED",
        "deterministic pre-checks passed; the demo mandate decides",
        now,
        intent_id=result.intent_id,
    )
    decide = mandates.ask if m.mode == "ASK" else mandates.approve
    approved, reason = await decide(conn, intent_id=result.intent_id, mandate=m, now=now)
    await record(
        conn,
        action="research.auto_signal",
        actor=ACTOR,
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=result.intent_id,
        payload={
            **account_fields(account.alias, account.kind, account.broker_adapter),
            "mandate_id": m.id,
            "strategy": spec.key,
            "timeframe": "1h",
            "bar": bar,
            "action": action,
            "instrument": key,
            "signal_series": series,
            "approved": approved,
            "reason": reason,
            "mode": m.mode,
        },
    )
    if not approved:
        return "REFUSED"
    return "ASKED" if m.mode == "ASK" else "APPROVED"


async def _entry_open(
    conn: AsyncConnection, account_id: uuid.UUID, instrument_id: uuid.UUID, mandate_id: uuid.UUID
) -> bool:
    """An auto-trader buy under this mandate is proposed, approved or executing."""
    rows = await conn.execute(
        select(trade_intents.c.details).where(
            trade_intents.c.account_id == account_id,
            trade_intents.c.instrument_id == instrument_id,
            trade_intents.c.side == "BUY",
            trade_intents.c.status.in_(OPEN),
        )
    )
    return any((r.details or {}).get("mandate_id") == str(mandate_id) for r in rows)


async def _record_event(
    conn: AsyncConnection,
    m: mandates.Mandate,
    spec: Spec,
    inst: md.InstrumentView,
    action: str,
    bar_open: datetime,
    now: datetime,
) -> uuid.UUID | None:
    eid = uuid7()
    result = await conn.execute(
        pg_insert(market_events)
        .values(
            id=eid,
            instrument_id=inst.id,
            kind="AUTO_SIGNAL",
            detected_at=now,
            bar_time=bar_open,
            detail=to_jsonable_lenient(
                {"strategy": spec.key, "action": action, "timeframe": "1h", "mandate": str(m.id)}
            ),
            dedup_key=f"AUTO_SIGNAL:{m.id}:{spec.key}:{inst.canonical_key}:{bar_open.isoformat()}",
            status="PROCESSED",  # handled here, not by the AI research missions
        )
        .on_conflict_do_nothing(index_elements=[market_events.c.dedup_key])
    )
    return eid if result.rowcount else None


async def _skipped(
    conn: AsyncConnection,
    account: Account,
    key: str,
    action: str,
    why: str,
    event_id: uuid.UUID,
    now: datetime,
) -> None:
    await notify_owners(
        conn,
        kind="auto_trade_skipped",
        now=now,
        dedup_key=f"auto_skip_event:{event_id}",
        payload={
            **account_fields(account.alias, account.kind, account.broker_adapter),
            "action": f"{'buy' if action == 'ENTER' else 'sell'} {key}",
            "reason": why[:500],
        },
    )


def _text(spec: Spec, series: str, bar: str) -> dict[str, Any]:
    return {
        "counterarguments": [
            "Breakouts on 1-hour bars often fail and reverse quickly (whipsaw).",
            "The rule was evaluated on daily bars, not on hourly ones.",
            f"The signal comes from {series}, which can differ from the venue's own price.",
        ],
        "red_team": (
            "Trend breakouts give back gains in sideways markets; a stop 3×ATR on hourly bars "
            "is hit often. Demo money: results show behaviour, not a proven edge."
        ),
        "scenarios": [
            {"name": "Breakout holds", "description": "Price keeps rising after the signal."},
            {"name": "Whipsaw", "description": "Price falls back and the stop is hit."},
        ],
        "invalidation": [f"'{spec.name}' turns flat on 1-hour bars", "Protective stop is hit"],
        "evidence_indicator": {
            "kind": "INDICATOR",
            "ref": f"auto:{spec.key}:1h:{bar}",
            "summary": f"{spec.name} on 1-hour bars of {series}, bar opened {bar}",
        },
    }


async def _entry(
    conn: AsyncConnection,
    m: mandates.Mandate,
    spec: Spec,
    inst: md.InstrumentView,
    account: Account,
    series: str,
    bid: Decimal,
    ask: Decimal,
    atr_value: float | None,
    bar: str,
    oid: uuid.UUID,
    now: datetime,
) -> tuple[Candidate | None, str]:
    if inst.tick_size is None or inst.lot_size is None:
        return None, "the instrument's price or size precision is unknown"
    if atr_value is None or atr_value <= 0:
        return None, "volatility (ATR) is unavailable for the protective stop"
    tick = inst.tick_size
    _, policy = await active_policy(conn)
    entry = _tick(ask * Decimal("1.001"), tick)  # marketable limit: ask + 0.1 %
    stop = _tick(entry - STOP_ATR * Decimal(str(round(atr_value, 8))), tick)
    market_stop = account.broker_adapter in MARKET_STOP_ADAPTERS
    stop_limit = stop if market_stop else _tick(stop * Decimal("0.995"), tick)
    if stop_limit <= ZERO:
        return None, "the protective stop would be at or below zero"
    room = policy.max_order_notional
    if m.capital is not None:  # never more than the owner's money, held fully paid
        free = m.capital - (await mandates.usage(conn, m, now)).open_value
        if free <= ZERO:
            return None, "all of your money for the auto-trader is in open trades"
        room = min(room, free)
    qty, per_unit = size_for_max_loss(
        max_loss=m.max_loss_per_trade,
        entry=entry,
        stop_limit=stop_limit,
        fee_bps=policy.fee_bps_for(inst.venue_key),
        lot_size=inst.lot_size,
        min_qty=inst.min_qty,
        max_order_notional=room,
    )
    if qty <= ZERO and (inst.min_qty or inst.lot_size) * entry > room:
        return None, (
            f"the smallest trade ({inst.min_qty or inst.lot_size}) is worth about "
            f"{(inst.min_qty or inst.lot_size) * entry:,.2f}, more than the {room:,.2f} of your "
            "money that is free"
        )
    if qty <= ZERO:
        smallest = (inst.min_qty or inst.lot_size) * per_unit
        return None, (
            f"the smallest trade ({inst.min_qty or inst.lot_size}) would risk about "
            f"{smallest:.2f}, more than the {m.max_loss_per_trade:.2f} allowed per trade"
        )
    if inst.min_notional is not None and qty * entry < inst.min_notional:
        return None, "the trade is below the venue's minimum order value"
    held = await conn.scalar(
        select(positions.c.quantity).where(
            positions.c.account_id == account.id, positions.c.instrument_id == inst.id
        )
    )
    t = _text(spec, series, bar)
    stop_text = (
        "held by the venue and filled at market"
        if market_stop
        else f"stop-limit {stop_limit} (0.5 % below)"
    )
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
            "horizon": "hours to days",
            "thesis": (
                f"Auto-trader under the demo mandate. Rule {spec.number} '{spec.name}': "
                f"{spec.rule} On the 1-hour bars of {series} it turned long on the bar opened "
                f"{bar}. The rule was evaluated on daily bars; on 1-hour bars this is a forward "
                f"test on demo money, not a proven edge. Size {qty}: the estimated loss at the "
                f"stop is {qty * per_unit:.2f}, within the mandate's {m.max_loss_per_trade:.2f}. "
                f"The protective stop is {STOP_ATR}×ATR(20) of the hourly bars below entry, "
                f"{stop_text}."
            )[:4000],
            "plain_summary": (
                f"{_head(m)}: buy {qty} {friendly(inst.canonical_key)} because "
                f"'{spec.name}' broke out on 1-hour bars. Loss at the stop at most "
                f"{m.max_loss_per_trade:.2f}. Forward test, not a proven edge."
            )[:600],
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{inst.canonical_key}",
                    "summary": f"bid {bid} / ask {ask}",
                    "source_ts": now,
                },
                t["evidence_indicator"],
            ],
            "counterarguments": t["counterarguments"],
            "red_team": t["red_team"],
            "scenarios": t["scenarios"],
            "invalidation": t["invalidation"],
            "entry_conditions": f"Limit buy at or below {entry} (ask + 0.1 %)",
            "opportunity_id": oid,
            "management_plan": {
                "stop_loss": {"stop_price": stop, "limit_price": stop_limit},
                "cancel_unfilled_after_seconds": CANCEL_UNFILLED_AFTER,
            },
        }
    ), ""


async def _exit(
    conn: AsyncConnection,
    m: mandates.Mandate,
    spec: Spec,
    inst: md.InstrumentView,
    account: Account,
    bid: Decimal,
    bar: str,
    oid: uuid.UUID,
    now: datetime,
) -> tuple[Candidate | None, str]:
    if inst.tick_size is None:
        return None, "the instrument's price precision is unknown"
    entries = await rule_entries(conn, spec, account.id, inst.id)
    qty = sum((h for _, h in entries), ZERO)
    held = Decimal(
        str(
            await conn.scalar(
                select(positions.c.quantity).where(
                    positions.c.account_id == account.id, positions.c.instrument_id == inst.id
                )
            )
            or 0
        )
    )
    if qty <= ZERO or qty > held:
        return None, "nothing held by the rule, or the books disagree (never oversell)"
    tick = inst.tick_size
    limit = _tick(bid * Decimal("0.999"), tick)  # marketable: bid − 0.1 %
    series = mandates.AUTO_SIGNAL_SERIES[inst.canonical_key]
    t = _text(spec, series, bar)
    return (
        Candidate.model_validate(
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
                "horizon": "hours to days",
                "thesis": (
                    f"Auto-trader exit under the demo mandate: rule {spec.number} '{spec.name}' "
                    f"turned flat on the 1-hour bar of {series} opened {bar}. This sells only what "
                    f"the rule bought ({qty})."
                ),
                "plain_summary": (
                    f"{_head(m)}: sell {qty} {friendly(inst.canonical_key)} because "
                    f"'{spec.name}' turned flat on 1-hour bars."
                )[:600],
                "evidence": [
                    {
                        "kind": "QUOTE",
                        "ref": f"quote:{inst.canonical_key}",
                        "summary": f"bid {bid}",
                        "source_ts": now,
                    },
                    t["evidence_indicator"],
                ],
                "counterarguments": ["The move may continue after the exit."],
                "red_team": "Exiting on a rule signal can sell near a short-term low.",
                "scenarios": t["scenarios"],
                "invalidation": ["Not applicable to an exit"],
                "entry_conditions": f"Limit sell at or above {limit} (bid − 0.1 %)",
                "opportunity_id": oid,
                "closes": [
                    intent_id for intent_id, _ in entries
                ],  # their stops are cancelled first
            }
        ),
        "",
    )
