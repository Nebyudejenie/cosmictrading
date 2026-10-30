"""The demo auto-trader (ADR-030): one library rule on hourly or daily bars, under a mandate.

Every few minutes, for each active demo mandate and each of its instruments, the rule is
computed on the latest completed bars of the mandate's timeframe (1-hour, or daily since
addendum 5) of the instrument's signal series. A move from flat to long proposes a buy sized so
that the loss at the stop is at most the mandate's maximum loss; a move to flat proposes
selling exactly what the rule bought. The mandate approves or refuses each proposal
(cosmic.trading.mandates): this module only proposes. Orders are then sent, protected, synced
and reconciled exactly as for an approval by the owner.

Differences from the tested rule, stated in every proposal:
- The library's rules were evaluated on daily bars; on 1-hour bars a rule is a forward test on
  demo money. On daily bars it runs as published, still a forward test, not a proven edge.
- The protective stop is 3×ATR(20) of the same bars below entry; where the venue holds the
  stop and fills it at market (HFM), there is no separate stop-limit price.
- The signal may come from a related market when the venue has no stored bars (HFM gold and
  bitcoin follow Binance PAXG/USDT and BTC/USDT).
- A daily rule decides once a day, on the bar completed at 00:00 UTC (usually within the hour
  after). Without a fresh price it waits for one rather than miss the day's signal.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
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
from cosmic.db.schema_execution import trade_intents, trading_mandates
from cosmic.db.schema_market import market_events
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.research import opportunities as opp
from cosmic.research.library import expect, store, ta
from cosmic.research.library.catalog import BY_KEY, Spec
from cosmic.research.library.engine import Market
from cosmic.research.library.signals import _exit_pending, _mission, _tick, rule_entries
from cosmic.trading import mandates, venue_terms
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.brokers.base import VenueQuote
from cosmic.trading.context_builder import LIVE_QUOTE_MAX_ROUND_TRIP, account_state
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, account_fields, friendly
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate, create_candidate
from cosmic.trading.risk import rooms

log = get_logger(__name__)
ACTOR = Actor.worker("auto_trader")
HOURLY, DAILY = mandates.HOURLY, mandates.DAILY
MIN_BARS = {HOURLY: 60, DAILY: 400}  # daily: a year's warm-up for 12-month momentum
BARS_TO_LOAD = {HOURLY: 500, DAILY: 5_000}  # daily: the whole history, as the replay uses
BAR_LENGTH = {HOURLY: timedelta(hours=1), DAILY: timedelta(days=1)}
# The newest completed bar must have closed this recently (daily bars complete at the first
# ingestion after midnight, usually within the hour).
MAX_BAR_LAG = {HOURLY: timedelta(hours=2), DAILY: timedelta(hours=26)}
STOP_ATR = Decimal(3)
BAND = Decimal("0.005")  # ±0.5 %: the mandate approves at once
CANCEL_UNFILLED_AFTER = 900  # a marketable entry that has not filled in 15 minutes is cancelled
# Venues that hold the stop with the position and fill it at market (no stop-limit price).
MARKET_STOP_ADAPTERS = frozenset({"hfm_mt5_demo"})
OPEN = ("PROPOSED", "APPROVED", "EXECUTING", "REQUIRES_REAPPROVAL")
HEADS_UP_ATR = 0.5  # "a trade may come soon": within half an ATR (of its bars) of the level


def _head(m: mandates.Mandate) -> str:
    """How the proposal introduces itself: the owner is asked first, or it is automatic."""
    if m.mode == "ASK":
        return "TIME TO TRADE (your auto-trader asks first)"
    return "AUTO (demo mandate)"


def is_breakout(spec: Spec) -> bool:
    """A channel breakout (Turtle): it buys above the highest high of the last ``entry`` bars,
    so a level to watch exists. Trend rules such as the 200-day average have none."""
    return "entry" in spec.params


def words(timeframe: str) -> tuple[str, str]:
    """('1-hour bars' | 'daily bars', 'an hourly' | 'a daily') for the owner's messages."""
    return mandates.TIMEFRAMES[timeframe], "an hourly" if timeframe == HOURLY else "a daily"


def watch(spec: Spec, market: Market) -> tuple[float, float, float] | None:
    """(breakout level for the next bar, last close, ATR) of a breakout rule, or None (also
    for a rule that is not a breakout).

    The rule buys when a close goes above the highest high of the last ``entry`` bars."""
    if not is_breakout(spec):
        return None
    n = int(spec.params["entry"])
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
    """Tell the owner when a market comes within half a bar's typical move (half an ATR) of
    its breakout level: a trade may come with the next close. At most once per market every
    six hours (hourly rules) or every day (daily rules)."""
    w = watch(spec, market)
    if w is None:
        return
    level, close, atr = w
    distance = level - close
    if not 0 < distance <= HEADS_UP_ATR * atr:
        return
    hourly = m.timeframe == HOURLY
    bucket = f"{bar_open:%Y%m%d}{bar_open.hour // 6}" if hourly else f"{bar_open:%Y%m%d}"
    then = "it will ask you to trade" if m.mode == "ASK" else "your auto-trader will buy"
    close_word = words(m.timeframe)[1]
    await notify_owners(
        conn,
        kind="auto_trade_soon",
        now=now,
        dedup_key=f"auto_soon:{m.id}:{key}:{bucket}",
        payload={
            **account_fields(account.alias, account.kind, account.broker_adapter),
            "text": (
                f"Heads-up: {friendly(key)} is {distance:,.2f} below its breakout "
                f"level ({level:,.2f}). If {close_word} close goes above it, {then} (loss at the "
                f"stop at most {m.max_loss_per_trade:,.2f})."
            ),
        },
    )


async def load_bars(
    conn: AsyncConnection, key: str, timeframe: str = HOURLY, limit: int | None = None
) -> Market | None:
    """The latest completed bars of one instrument on ``timeframe``, oldest first, with UTC
    datetimes (analytics only, ADR-007); None below the timeframe's minimum. Daily rules load
    the whole history, so a long average warms up exactly as in the replay."""
    try:
        inst = await md.instrument_by_key(conn, key)
    except NotFoundError:
        return None
    bars = await md.complete_bars(conn, inst.id, timeframe, limit=limit or BARS_TO_LOAD[timeframe])
    if len(bars) < MIN_BARS[timeframe]:
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


async def load_hourly(conn: AsyncConnection, key: str, limit: int = 500) -> Market | None:
    """The latest ``limit`` completed 1-hour bars of one instrument, oldest first."""
    return await load_bars(conn, key, HOURLY, limit)


def size_for_max_loss(
    *,
    max_loss: Decimal,
    entry: Decimal,
    stop_limit: Decimal,
    fee_bps: Decimal,
    lot_size: Decimal,
    min_qty: Decimal | None,
    max_order_notional: Decimal,
    max_quantity: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """(quantity, loss per unit) so that quantity × (entry − stop-limit) plus fees on both sides
    stays within ``max_loss`` — the same estimate the approval packet shows — and the value
    within ``max_order_notional`` (and the quantity within ``max_quantity``). The quantity is
    zero when even the smallest trade would not fit."""
    per_unit = entry - stop_limit + 2 * entry * fee_bps / BPS
    if per_unit <= ZERO:
        return ZERO, per_unit
    qty = (max_loss / per_unit / lot_size).to_integral_value(ROUND_DOWN) * lot_size
    cap = (max_order_notional / entry / lot_size).to_integral_value(ROUND_DOWN) * lot_size
    qty = min(qty, cap)
    if max_quantity is not None:
        qty = min(qty, (max_quantity / lot_size).to_integral_value(ROUND_DOWN) * lot_size)
    if min_qty is not None and qty < min_qty:
        return ZERO, per_unit
    return qty, per_unit


VenueQuotes = Callable[[Account, str], Awaitable[VenueQuote | None]]


def _under(account: Account) -> str:
    """What a proposal was made under, in its own words: never "demo" for real money."""
    if account.kind in SIMULATED_ACCOUNT_KINDS:
        return "the demo mandate"
    return "the real-money auto-trader, which only asks"


def _venue_view(q: md.QuoteView, venue_quote: VenueQuote | None) -> md.QuoteView:
    """A real-money proposal is priced only by the venue's own book read just before (ADR-034),
    labelled as the risk checks label it (``context_builder.venue_market``)."""
    if venue_quote is None:
        return replace(q, bid=None, ask=None, last=None, label=DataLabel.UNAVAILABLE)
    quick = timedelta(0) <= venue_quote.round_trip <= LIVE_QUOTE_MAX_ROUND_TRIP
    return replace(
        q,
        bid=venue_quote.bid,
        ask=venue_quote.ask,
        last=None,
        source_ts=venue_quote.received_at,
        label=DataLabel.LIVE if quick else DataLabel.STALE,
    )


async def run(db: Database, clock: Clock, venue_quotes: VenueQuotes | None = None) -> list[str]:
    """``venue_quotes`` reads a LIVE account's venue book (the worker's broker registry); without
    it a real-money mandate is skipped, never priced by a stored quote."""
    now = clock.now()
    async with db.transaction() as conn:
        await mandates.expire_due(conn, now)
        await mandates.stop_due(conn, now)
        await mandates.tell_size_changes(conn, now)
    try:  # a reminder never stops a trading cycle
        async with db.transaction() as conn:
            await mandates.remind_renewals(conn, now)
    except Exception as exc:
        log.warning("renewal_reminder_failed", error=scrub_text(str(exc))[:300])
    async with db.connect() as conn:
        active = await mandates.active_mandates(conn, now)
    done: list[str] = []
    for m in active:
        spec = BY_KEY.get(m.strategy_key)
        if spec is None or spec.fn is None:
            continue
        async with db.connect() as conn:
            account = await get_account(conn, account_id=m.account_id)
        for key in m.instruments:
            when, quote = now, None
            try:  # one instrument's failure never stops the others
                if account.kind not in SIMULATED_ACCOUNT_KINDS:
                    # Real money (ADR-034): the venue's own book, read outside any
                    # transaction, and the decision timed after it.
                    if venue_quotes is None:
                        continue
                    quote = await venue_quotes(account, key)
                    when = clock.now()
                async with db.transaction() as conn:
                    outcome = await _one(conn, m, spec, key, when, venue_quote=quote)
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
    conn: AsyncConnection,
    m: mandates.Mandate,
    spec: Spec,
    key: str,
    now: datetime,
    *,
    venue_quote: VenueQuote | None = None,
) -> str | None:
    inst = await md.instrument_by_key(conn, key)
    series = mandates.AUTO_SIGNAL_SERIES[key]
    tf = m.timeframe
    bars_words = words(tf)[0]
    market = await load_bars(conn, series, tf)
    if market is None:
        return None
    assert spec.fn is not None
    weights = spec.fn(market, spec.params)
    if len(weights) < 2:
        return None
    last_i, prev_i = len(weights) - 1, len(weights) - 2
    bar_open = market.dates[last_i]
    if not isinstance(bar_open, datetime):
        return None
    act_from = bar_open + BAR_LENGTH[tf]  # the next bar's open: where the replay fills
    if tf == DAILY and inst.base in venue_terms.METALS:
        # A market closed at weekends (gold CFDs) whose signal series is not: decide on the
        # last bar in session against the one before it, and act at the next bar in session
        # (Monday 00:00 UTC for Friday's), as the replay does.
        sessions = [
            i
            for i, d in enumerate(market.dates)
            if isinstance(d, datetime) and not expect.weekend_closed_at(d)
        ]
        if len(sessions) < 2:
            return None
        prev_i, last_i = sessions[-2], sessions[-1]
        bar_open = market.dates[last_i]
        assert isinstance(bar_open, datetime)
        act_from = bar_open + BAR_LENGTH[tf]
        while expect.weekend_closed_at(act_from):
            act_from += BAR_LENGTH[tf]
        if now < act_from:
            return None  # the venue is closed until then: the signal waits, unconsumed
    if now - act_from > MAX_BAR_LAG[tf]:
        return None  # stale series: never act on old data
    prev, last = weights[prev_i], weights[last_i]
    account = await get_account(conn, account_id=m.account_id)
    entries = await rule_entries(conn, spec, account.id, inst.id)
    if prev == 0.0 and last > 0.0:
        if entries or await _entry_open(conn, account.id, inst.id):
            return None  # already in, or an entry is on its way
        if await _bought_on_bar(conn, account.id, inst.id, spec.key, bar_open):
            return None  # this bar's buy signal was acted on (before a renewal, say)
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
    # A daily exit that did not happen (an unfilled immediate-or-cancel order, a refused
    # check) is tried again the next hour, not the next day; everything else once per bar.
    attempt = f"{now:%Y%m%d%H}" if tf == DAILY and action == "EXIT" else ""
    dedup = signal_key(m, spec, inst.canonical_key, bar_open, attempt)
    if await _handled(conn, dedup):
        return None  # this bar was already handled
    q = await md.latest_quote(conn, inst.id, now)
    if account.kind not in SIMULATED_ACCOUNT_KINDS:
        q = _venue_view(q, venue_quote)
    fresh = (
        q.label in (DataLabel.LIVE, DataLabel.DELAYED) and q.bid is not None and q.ask is not None
    )
    if not fresh and tf == DAILY:
        # A daily signal comes once a day: wait for a fresh price (every run tries again while
        # the bar is current) rather than lose the day's signal. The owner hears it once.
        await _waiting_for_price(conn, m, account, key, action, bar_open, q.label.value, now)
        return None
    event_id = await _record_event(conn, m, spec, inst, action, bar_open, now, dedup)
    if event_id is None:
        return None  # handled meanwhile
    skip_key = f"auto_skip_bar:{m.id}:{key}:{bar}:{action}" if attempt else None
    version_id = await store.ensure_spec(conn, spec, now)
    oid = await _mission(conn, spec, inst, event_id, version_id, now)
    await opp.advance(
        conn,
        oid,
        "RESEARCH",
        f"auto-trader: rule {spec.key} on {bars_words} signalled {action}",
        now,
        direction="LONG" if action == "ENTER" else None,
    )
    await opp.advance(
        conn,
        oid,
        "CHALLENGED",
        "forward test on 1-hour bars (evaluated on daily bars)"
        if tf == HOURLY
        else "forward test on daily bars, the bars it was evaluated on",
        now,
    )
    candidate: Candidate | None = None
    if not fresh or q.bid is None or q.ask is None:
        why = f"no fresh price for {key} ({q.label.value})"
    elif action == "ENTER":
        atr = ta.atr(market.high, market.low, market.close, 20)[last_i]
        candidate, why = await _entry(
            conn, m, spec, inst, account, series, q.bid, q.ask, atr, bar, oid, now
        )
    else:
        candidate, why = await _exit(conn, m, spec, inst, account, q.bid, bar, oid, now)
    if candidate is None:
        await opp.advance(conn, oid, "NO_TRADE", why[:400], now)
        await _skipped(conn, account, key, action, why, event_id, now, skip_key)
        return "NO_TRADE"
    await opp.advance(conn, oid, "QUALIFIED", f"auto-trader candidate under {_under(account)}", now)
    result = await create_candidate(
        conn,
        candidate=candidate,
        actor=ACTOR,
        now=now,
        mandate_id=m.id,
        timeframe=tf,
        venue_quote=venue_quote,
        ttl=mandates.proposal_ttl(account.kind, tf),
    )
    if result.status != "PROPOSED":
        problems = "; ".join(result.problems)
        await opp.advance(
            conn, oid, "NON_EXECUTABLE", problems[:400], now, intent_id=result.intent_id
        )
        await _skipped(
            conn, account, key, action, f"risk checks: {problems}", event_id, now, skip_key
        )
        return "NON_EXECUTABLE"
    await opp.advance(
        conn,
        oid,
        "APPROVAL_REQUIRED",
        f"deterministic pre-checks passed; {_under(account)} decides"
        if m.mode == "AUTO"
        else "deterministic pre-checks passed; the owner is asked",
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
            "timeframe": tf,
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


async def _waiting_for_price(
    conn: AsyncConnection,
    m: mandates.Mandate,
    account: Account,
    key: str,
    action: str,
    bar_open: datetime,
    label: str,
    now: datetime,
) -> None:
    """A daily rule signalled but the venue's price is not fresh: say so once per bar."""
    side = "buy" if action == "ENTER" else "sell"
    await notify_owners(
        conn,
        kind="auto_trade_soon",
        now=now,
        dedup_key=f"auto_wait:{m.id}:{key}:{bar_open:%Y%m%d}:{action}",
        payload={
            **account_fields(account.alias, account.kind, account.broker_adapter),
            "text": (
                f"Your auto-trader's daily rule says {side} {friendly(key)}, but there is no "
                f"fresh price ({label}). It tries again every few minutes today."
            ),
        },
    )


async def _bought_on_bar(
    conn: AsyncConnection,
    account_id: uuid.UUID,
    instrument_id: uuid.UUID,
    rule: str,
    bar_open: datetime,
) -> bool:
    """The account's auto-trader acted on this rule's buy signal of this bar already, under any
    of its mandates: a renewal (a new mandate) must not trade the same signal twice."""
    ids = [
        str(r.id)
        for r in await conn.execute(
            select(trading_mandates.c.id).where(trading_mandates.c.account_id == account_id)
        )
    ]
    found = await conn.scalar(
        select(market_events.c.id)
        .where(
            market_events.c.kind == "AUTO_SIGNAL",
            market_events.c.instrument_id == instrument_id,
            market_events.c.bar_time == bar_open,
            market_events.c.detail["strategy"].astext == rule,
            market_events.c.detail["action"].astext == "ENTER",
            market_events.c.detail["mandate"].astext.in_(ids),
        )
        .limit(1)
    )
    return found is not None


async def _entry_open(
    conn: AsyncConnection, account_id: uuid.UUID, instrument_id: uuid.UUID
) -> bool:
    """An auto-trader buy on this account and market is proposed, approved or executing, under
    any of its mandates (one replaced a moment ago included)."""
    rows = await conn.execute(
        select(trade_intents.c.details).where(
            trade_intents.c.account_id == account_id,
            trade_intents.c.instrument_id == instrument_id,
            trade_intents.c.side == "BUY",
            trade_intents.c.status.in_(OPEN),
        )
    )
    return any("mandate_id" in (r.details or {}) for r in rows)


def signal_key(
    m: mandates.Mandate, spec: Spec, instrument: str, bar_open: datetime, attempt: str = ""
) -> str:
    """The once-only key of a signal: per mandate, rule, market and bar (and, for a daily exit
    tried again each hour, the hour of the attempt)."""
    key = f"AUTO_SIGNAL:{m.id}:{spec.key}:{instrument}:{bar_open.isoformat()}"
    return f"{key}:{attempt}" if attempt else key


async def _handled(conn: AsyncConnection, dedup_key: str) -> bool:
    found = await conn.scalar(
        select(market_events.c.id).where(market_events.c.dedup_key == dedup_key).limit(1)
    )
    return found is not None


async def _record_event(
    conn: AsyncConnection,
    m: mandates.Mandate,
    spec: Spec,
    inst: md.InstrumentView,
    action: str,
    bar_open: datetime,
    now: datetime,
    dedup_key: str,
) -> uuid.UUID | None:
    """Mark this bar's signal handled; None if it already was."""
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
                {
                    "strategy": spec.key,
                    "action": action,
                    "timeframe": m.timeframe,
                    "mandate": str(m.id),
                }
            ),
            dedup_key=dedup_key,
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
    dedup_key: str | None = None,
) -> None:
    """``dedup_key``: one notice per bar for a daily exit tried again each hour."""
    await notify_owners(
        conn,
        kind="auto_trade_skipped",
        now=now,
        dedup_key=dedup_key or f"auto_skip_event:{event_id}",
        payload={
            **account_fields(account.alias, account.kind, account.broker_adapter),
            "action": f"{'buy' if action == 'ENTER' else 'sell'} {key}",
            "reason": why[:500],
        },
    )


def _text(spec: Spec, series: str, bar: str, tf: str) -> dict[str, Any]:
    bars_words = words(tf)[0]
    if tf == HOURLY:
        against = [
            "Breakouts on 1-hour bars often fail and reverse quickly (whipsaw).",
            "The rule was evaluated on daily bars, not on hourly ones.",
        ]
        red_team = (
            "Trend breakouts give back gains in sideways markets; a stop 3×ATR on hourly bars "
            "is hit often. Demo money: results show behaviour, not a proven edge."
        )
    else:
        against = [
            "Daily trend rules lose in sideways markets through repeated small stops; most of "
            "their gain comes from a few long trends.",
            "Its past results on this market are a historical simulation; the rule was chosen "
            "from several, so they flatter it.",
        ]
        red_team = (
            "A daily rule holds for weeks and gives back part of a move before it exits. "
            "Simulated money: results show behaviour, not a proven edge."
        )
    return {
        "counterarguments": [
            *against,
            f"The signal comes from {series}, which can differ from the venue's own price.",
        ],
        "red_team": red_team,
        "scenarios": [
            {"name": "Trend holds", "description": "Price keeps rising after the signal."},
            {"name": "Whipsaw", "description": "Price falls back and the stop is hit."},
        ],
        "invalidation": [f"'{spec.name}' turns flat on {bars_words}", "Protective stop is hit"],
        "evidence_indicator": {
            "kind": "INDICATOR",
            "ref": f"auto:{spec.key}:{tf}:{bar}",
            "summary": f"{spec.name} on {bars_words} of {series}, bar opened {bar}",
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
    # Size within every risk limit the proposal will be checked against (S36: R05, R06, R07,
    # R19), so a trade is made smaller instead of sized and then refused.
    limits = rooms.buy_room(
        await account_state(conn, account, now),
        policy,
        instrument_key=inst.canonical_key,
        venue_key=inst.venue_key,
        limit_price=entry,
    )
    room, binding = policy.max_order_notional, "the risk policy's largest order"
    if limits.notional is not None and limits.notional < room:
        room, binding = limits.notional, limits.binding
    if m.capital is not None:  # never more than the owner's money, held fully paid
        free = m.capital - (await mandates.usage(conn, m, now)).open_value
        if free <= ZERO:
            return None, "all of your money for the auto-trader is in open trades"
        if free < room:
            room, binding = free, "your money for the auto-trader that is free"
    # R19 bounds the loss at the stop-limit (before fees) of this trade and the open ones.
    max_qty = limits.loss / (entry - stop_limit) if limits.loss is not None else None
    size = await mandates.sizing_now(conn, m)  # half after a deep drop (addendum 4)
    max_loss = (m.max_loss_per_trade * size.factor).quantize(Decimal("0.01"), ROUND_DOWN)
    qty, per_unit = size_for_max_loss(
        max_loss=max_loss,
        entry=entry,
        stop_limit=stop_limit,
        fee_bps=policy.fee_bps_for(inst.venue_key),
        lot_size=inst.lot_size,
        min_qty=inst.min_qty,
        max_order_notional=room,
        max_quantity=max_qty,
    )
    smallest_qty = inst.min_qty or inst.lot_size
    if qty <= ZERO and smallest_qty * entry > room:
        return None, (
            f"the smallest trade ({smallest_qty}) is worth about {smallest_qty * entry:,.2f}, "
            f"more than the {room:,.2f} that {binding} allows"
        )
    if qty <= ZERO and max_qty is not None and smallest_qty > max_qty:
        return None, (
            f"the smallest trade ({smallest_qty}) would take the loss if every stop is hit past "
            f"the risk policy's {policy.max_open_risk_pct} % of equity"
        )
    if qty <= ZERO:
        smallest = (inst.min_qty or inst.lot_size) * per_unit
        return None, (
            f"the smallest trade ({inst.min_qty or inst.lot_size}) would risk about "
            f"{smallest:.2f}, more than the {max_loss:.2f} allowed per trade"
            + (" (half size after a deep drop)" if size.half else "")
        )
    if inst.min_notional is not None and qty * entry < inst.min_notional:
        return None, "the trade is below the venue's minimum order value"
    held = await conn.scalar(
        select(positions.c.quantity).where(
            positions.c.account_id == account.id, positions.c.instrument_id == inst.id
        )
    )
    tf = m.timeframe
    bars_words = words(tf)[0]
    t = _text(spec, series, bar, tf)
    tested = (
        "The rule was evaluated on daily bars; on 1-hour bars this is a forward test on demo "
        "money, not a proven edge."
        if tf == HOURLY
        else "It runs on daily bars, as evaluated: a forward test on simulated money, not a "
        "proven edge."
        if account.kind in SIMULATED_ACCOUNT_KINDS
        else "It runs on daily bars, as evaluated, with REAL MONEY: the rule is not a proven "
        "edge, and nothing is sent unless you approve it with your code."
    )
    signalled = "broke out" if is_breakout(spec) else "turned long"
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
                f"Auto-trader under {_under(account)}. Rule {spec.number} '{spec.name}': "
                f"{spec.rule} On the {bars_words} of {series} it turned long on the bar opened "
                f"{bar}. {tested} Size {qty}: the estimated loss at the "
                f"stop is {qty * per_unit:.2f}, within {max_loss:.2f}"
                + (
                    f" (half the mandate's {m.max_loss_per_trade:.2f}: the result is "
                    f"{size.drop:.2f} below its best point). "
                    if size.half
                    else ". "
                )
                + f"The protective stop is {STOP_ATR}×ATR(20) of the {bars_words} below entry, "
                f"{stop_text}."
            )[:4000],
            "plain_summary": (
                f"{_head(m)}: buy {qty} {friendly(inst.canonical_key)} because "
                f"'{spec.name}' {signalled} on {bars_words}. Loss at the stop at most "
                f"{max_loss:.2f}{' (half size after a deep drop)' if size.half else ''}. "
                "Forward test, not a proven edge."
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
    bars_words = words(m.timeframe)[0]
    t = _text(spec, series, bar, m.timeframe)
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
                    f"Auto-trader exit under {_under(account)}: rule {spec.number} '{spec.name}' "
                    f"turned flat on the {bars_words} of {series}, bar opened {bar}. This sells "
                    f"only what the rule bought ({qty})."
                ),
                "plain_summary": (
                    f"{_head(m)}: sell {qty} {friendly(inst.canonical_key)} because "
                    f"'{spec.name}' turned flat on {bars_words}."
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
