"""The HFM gold home page (S55): the owner's HFM account at a glance, for a beginner.

It answers five questions first: is the system working, is the broker connected, is automatic
trading running, is the money protected, and does anything need the owner. Then the account:

- **from HFM's own report** through the bridge: whether it is a DEMO or a REAL account, its
  balance, equity and currency, every open position with its stop-loss, and the deals that give
  today's and the total result;
- from our records, reconciled against HFM every five minutes: the auto-trader's latest decision
  and what came of it (read from the trade it proposed), its limits and the checks.

An answer is "yes" only on fresh facts: without a fresh report from HFM, protection is unknown.
The paper rehearsals have their own panel; nothing from the paper simulator is shown here. It
changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, NotFoundError
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema import incidents, notifications, worker_heartbeats
from cosmic.db.schema_execution import approvals, orders, reconciliation_runs, trade_intents
from cosmic.db.schema_market import market_events
from cosmic.db.schema_trading import bridge_state, ledger_entries, ledger_txns, positions
from cosmic.market import data as md
from cosmic.reporting import autotrader
from cosmic.research.library import auto, expect
from cosmic.research.library.catalog import BY_KEY
from cosmic.trading import hfm_ready, hfm_test_trade, mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.authority import get_state
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.modes import Operation, mode_permits

ACCOUNT = "hfm-demo"  # the HFM account the bridge trades (a SANDBOX account on HFM's demo)
SIMULATION_ACCOUNT = "hfm-sim"  # the internal simulation's account (S60, hfm_settings)
GOLD = "CFD:XAU/USD@HFM"
SYMBOL = "XAUUSD"
CURRENCY = "USD"  # the money the server understands from this bridge (a Cent account is not yet)
SIGNAL = "Binance's PAXG/USDT"  # the series the HFM gold auto-trader's rule reads
WORKER_ALIVE = timedelta(seconds=60)
CHECKED = timedelta(minutes=15)  # the account is reconciled with HFM every five minutes
RECENT = timedelta(days=2)  # an older signal is no longer the "latest decision"
UNTRACKED = Decimal("0.05")  # equity less balance further than this from the positions' sum
NEW_YORK = ZoneInfo("America/New_York")
ADDIS = ZoneInfo("Africa/Addis_Ababa")
DEAL_TRADES = (0, 1)  # MT5 deal types BUY and SELL; others are deposits, credits and the like
WAITING = ("PROPOSED", "REQUIRES_REAPPROVAL")  # a trade the owner has to decide on


@dataclass(frozen=True)
class Answer:
    question: str
    ok: bool | None  # True: yes; False: no, it needs the owner; None: unknown or not now
    text: str


@dataclass(frozen=True)
class Position:
    """An open position as HFM reports it."""

    symbol: str
    lots: Decimal
    units: Decimal  # ounces for gold: lots × the symbol's contract size
    price_open: Decimal
    price_now: Decimal | None
    stop: Decimal | None  # HFM's stop-loss on it; None when it has none
    result: Decimal  # HFM's floating profit, with its swap
    opened_at: datetime | None
    own: bool = False  # made by hand in MT5: the owner's, outside the system's money (S59)
    side: str = "BUY"  # the system only buys; a trade by hand may sell short


@dataclass
class HfmHome:
    status: str  # ALL GOOD | NEEDS YOU | PROBLEM
    answers: list[Answer]
    attention: list[str]
    mode: str  # DEMO | REAL | NOT CONNECTED
    fresh: bool = False  # HFM's report is current (the figures below are as of it otherwise)
    reported_at: datetime | None = None
    currency: str | None = None
    balance: Decimal | None = None
    equity: Decimal | None = None
    today: Decimal | None = None
    total: Decimal | None = None
    results_since: datetime | None = None  # the oldest deal in HFM's report
    positions: list[Position] = field(default_factory=list)
    closed: int = 0
    wins: int = 0
    bid: Decimal | None = None
    ask: Decimal | None = None
    price_at: datetime | None = None
    market: str = ""
    decision: str = "WAIT"  # BUY | SELL | WAIT
    decision_text: str = ""
    decision_at: datetime | None = None
    protection: str = ""
    last_check: datetime | None = None  # the newest check with HFM that passed
    next_decision: datetime | None = None  # the start of the window it next decides in
    next_decision_end: datetime | None = None
    rule: str = ""
    bars: str = ""
    account: str = ACCOUNT
    gold: str = GOLD
    gold_held: bool = False  # ours, open at HFM, and the broker can be traded: offer the close
    # The owner's own trades by hand (S59): open now, and the deals booked as theirs so far.
    own_open: int = 0
    own_deals: int = 0
    own_result: Decimal = ZERO
    closing: bool = False  # a close (any sale on the account) waits for approval or is being sent
    real_passed: int = 0  # the real-account checklist (S58): checks passed, of real_total
    real_total: int = 0
    # Why the test trade (S57) cannot be proposed now; None when the home page offers it.
    test_trade: str | None = "there is no HFM account here yet"

    @property
    def floating(self) -> Decimal | None:
        if self.balance is None or self.equity is None:
            return None
        return self.equity - self.balance

    @property
    def spread(self) -> Decimal | None:
        return None if self.bid is None or self.ask is None else self.ask - self.bid


def _d(value: Any) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _server_time(ms: Any, offset: Any) -> datetime | None:
    """MT5 stamps in the broker's server time; the bridge reports its offset from UTC (None
    while it is not known: no time is guessed)."""
    if offset is None:
        return None
    try:
        return datetime.fromtimestamp((int(ms) - int(offset)) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def market_state(now: datetime) -> str:
    """Gold at HFM: closed from Friday 17:00 to Sunday 18:00 New York, with a short daily break
    from the 17:00 rollover."""
    if expect.weekend_closed_at(now):
        return "closed for the weekend (reopens Sunday 18:00 in New York)"
    if now.astimezone(NEW_YORK).hour == 17:
        return "in HFM's daily break (about an hour from 17:00 in New York)"
    return "open"


HOURLY_WINDOW = timedelta(minutes=10)  # hourly bars are stored within minutes of the hour
DAILY_WINDOW = timedelta(hours=1)  # daily bars are fetched once an hour


def next_decision(m: mandates.Mandate | None, now: datetime) -> tuple[datetime, datetime] | None:
    """The window in which the auto-trader next decides, the current one while it lasts. It runs
    every five minutes once the bar it reads is complete and stored: an hourly bar from about
    :05 to :15, a daily bar from 00:05 to 01:05 UTC; daily gold decides after the weekend on
    Monday."""
    if m is None:
        return None
    if m.timeframe == mandates.DAILY:
        at, window = mandates.day_start(now) + timedelta(minutes=5), DAILY_WINDOW
        if now >= at + window:
            at += timedelta(days=1)
        while expect.weekend_closed_at(at - timedelta(minutes=5)):
            at += timedelta(days=1)
        return at, at + window
    at, window = now.replace(minute=5, second=0, microsecond=0), HOURLY_WINDOW
    if now >= at + window:
        at += timedelta(hours=1)
    # Its gold signals wait while HFM's gold is closed (the weekend, the daily break).
    while market_state(at) != "open":
        at += timedelta(hours=1)
    return at, at + window


async def _report(conn: AsyncConnection) -> tuple[datetime | None, dict[str, Any]]:
    row = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == mt5_bridge.BRIDGE
            )
        )
    ).first()
    if row is None:
        return None, {}
    return row.received_at, dict(row.snapshot or {})


def _positions(snap: dict[str, Any]) -> list[Position]:
    """Every open position HFM reports, gold or not."""
    offset = snap.get("server_utc_offset_ms")
    symbols = snap.get("symbols") or {}
    out: list[Position] = []
    for p in snap.get("positions") or ():
        symbol = str(p.get("symbol") or "")
        lots = _d(p.get("volume")) or ZERO
        contract = _d((symbols.get(symbol) or {}).get("contract_size"))
        stop = _d(p.get("sl"))
        out.append(
            Position(
                symbol=symbol,
                lots=lots,
                units=lots * contract if contract else lots,
                price_open=_d(p.get("price_open")) or ZERO,
                price_now=_d(p.get("price_current")),
                stop=stop if stop else None,
                result=(_d(p.get("profit")) or ZERO) + (_d(p.get("swap")) or ZERO),
                opened_at=_server_time(p.get("time_msc"), offset),
                own=p.get("magic") == mt5_bridge.HAND,
                side="SELL" if p.get("type") == 1 else "BUY",
            )
        )
    return out


def _results(
    snap: dict[str, Any], now: datetime
) -> tuple[Decimal | None, Decimal, datetime | None]:
    """(today's deals, UTC, after costs; the deals in HFM's report, after costs; the oldest of
    them). Every trade deal on the account counts, an entry's commission among them."""
    offset = snap.get("server_utc_offset_ms")
    start = mandates.day_start(now)
    today: Decimal | None = ZERO if offset is not None else None
    total = ZERO
    oldest: datetime | None = None
    for d in snap.get("deals") or ():
        if d.get("type") not in DEAL_TRADES:
            continue
        value = sum(((_d(d.get(k)) or ZERO) for k in ("profit", "commission", "swap", "fee")), ZERO)
        total += value
        at = _server_time(d.get("time_msc"), offset)
        if at is not None:
            oldest = at if oldest is None else min(oldest, at)
            if today is not None and at >= start:
                today += value
    return today, total, oldest


async def _worker_ok(conn: AsyncConnection, now: datetime) -> bool:
    beat = await conn.scalar(
        select(func.max(worker_heartbeats.c.last_beat_at)).where(
            worker_heartbeats.c.process == "worker"
        )
    )
    return beat is not None and now - beat <= WORKER_ALIVE


async def _checks(conn: AsyncConnection, account: Account) -> tuple[Any, datetime | None]:
    """(the newest finished check of the account with HFM, the newest one that passed); a check
    still running is not a result yet."""
    latest = (
        await conn.execute(
            select(reconciliation_runs.c.status, reconciliation_runs.c.started_at)
            .where(
                reconciliation_runs.c.account_id == account.id,
                reconciliation_runs.c.status != "RUNNING",
            )
            .order_by(desc(reconciliation_runs.c.started_at))
            .limit(1)
        )
    ).first()
    passed: datetime | None = await conn.scalar(
        select(func.max(reconciliation_runs.c.started_at)).where(
            reconciliation_runs.c.account_id == account.id,
            reconciliation_runs.c.status == "PASSED",
        )
    )
    return latest, passed


async def _rule_long(conn: AsyncConnection, m: mandates.Mandate) -> bool | None:
    """Whether the auto-trader's rule holds a long on its latest completed bar."""
    spec = BY_KEY.get(m.strategy_key)
    if spec is None or spec.fn is None:
        return None
    market = await auto.load_bars(conn, mandates.AUTO_SIGNAL_SERIES[GOLD], m.timeframe)
    if market is None:
        return None
    weights = spec.fn(market, spec.params)
    return bool(weights) and weights[-1] > 0


def _waiting_text(watch: autotrader.Watch | None, bars: str, held: bool, long: bool | None) -> str:
    if held:
        return "in a trade: it sells when its rule turns, or HFM's stop-loss closes it"
    if long:
        return (
            "its rule already holds a long that was not traded; it buys at the rule's next new "
            f"signal on {bars}"
        )
    if watch is not None and watch.level is not None:
        return f"no buy signal: it buys when {SIGNAL} closes above {watch.level:,.2f} on {bars}"
    return f"no buy signal: it buys when its rule turns long on {SIGNAL}, {bars}"


def _outcome(intent: Any, order: Any) -> tuple[str, bool]:
    """What came of the trade a signal proposed, and whether it waits for the owner."""
    reason = (intent.status_reason or "").strip()
    if intent.status in WAITING:
        return f"waiting for your approval until {intent.intent_expires_at:%d %b %H:%M} UTC", True
    if intent.status in ("APPROVED", "EXECUTING"):
        return "approved; being sent to HFM", False
    if intent.status == "EXECUTED":
        if order is not None and order.avg_fill_price is not None:
            return f"sent to HFM and filled at {order.avg_fill_price:,.2f}", False
        return "sent to HFM", False
    if intent.status == "EXECUTION_FAILED":
        return f"not filled{': ' + reason if reason else ''}", False
    if intent.status in ("REJECTED", "CHANGES_REQUESTED"):
        return "not traded: you declined it", False
    if intent.status == "EXPIRED":
        return "not traded: it expired", False
    return f"not traded{': ' + reason if reason else ''}", False


async def _decision(
    conn: AsyncConnection,
    m: mandates.Mandate | None,
    gold_id: Any,
    watch: autotrader.Watch | None,
    bars: str,
    held: bool,
    now: datetime,
) -> tuple[str, str, datetime | None, bool]:
    """(BUY | SELL | WAIT, what came of it, when, whether it waits for the owner): the latest
    signal of the auto-trader's rule on HFM gold, read from the trade it proposed."""
    if m is None:
        return "WAIT", "the auto-trader is off", None, False
    event = (
        await conn.execute(
            select(
                market_events.c.id,
                market_events.c.detail,
                market_events.c.detected_at,
                market_events.c.bar_time,
            )
            .where(
                market_events.c.kind == "AUTO_SIGNAL",
                market_events.c.instrument_id == gold_id,
                market_events.c.detail["mandate"].astext == str(m.id),
            )
            .order_by(desc(market_events.c.detected_at))
            .limit(1)
        )
    ).first()
    if event is None or now - event.detected_at > RECENT:
        long = await _rule_long(conn, m)
        return "WAIT", _waiting_text(watch, bars, held, long), None, False
    buy = event.detail.get("action") == "ENTER"
    action = "BUY" if buy else "SELL"
    intent = (
        await conn.execute(
            select(trade_intents)
            .where(
                trade_intents.c.account_id == m.account_id,
                trade_intents.c.instrument_id == gold_id,
                trade_intents.c.side == ("BUY" if buy else "SELL"),
                trade_intents.c.details["mandate_id"].astext == str(m.id),
                trade_intents.c.created_at >= event.detected_at,
            )
            .order_by(desc(trade_intents.c.created_at))
            .limit(1)
        )
    ).first()
    if intent is not None:
        # A buy's own order is its ENTRY (its stop and exits hang on the same trade); a sell's
        # EXIT order hangs on the trade it closes, through the sell's approval.
        mine = (
            orders.c.intent_id == intent.id
            if buy
            else orders.c.approval_id.in_(
                select(approvals.c.id).where(approvals.c.intent_id == intent.id)
            )
        )
        order = (
            await conn.execute(
                select(orders.c.status, orders.c.avg_fill_price)
                .where(mine, orders.c.purpose == ("ENTRY" if buy else "EXIT"))
                .order_by(desc(orders.c.created_at))
                .limit(1)
            )
        ).first()
        text, waits = _outcome(intent, order)
        if event.detail.get("waiting_for_data") and not waits:
            text += "; it tries again each hour while today's signal holds"
        return action, text, event.detected_at, waits
    bar = event.bar_time.isoformat() if event.bar_time is not None else ""
    skipped = await conn.scalar(
        select(notifications.c.payload["reason"].astext)
        .where(
            notifications.c.kind == "auto_trade_skipped",
            or_(
                notifications.c.dedup_key.like(f"auto_skip_event:{event.id}:%"),
                notifications.c.dedup_key.like(f"auto_skip_bar:{m.id}:{GOLD}:{bar}:%"),
            ),
        )
        .order_by(desc(notifications.c.created_at))
        .limit(1)
    )
    if skipped:
        return action, f"signalled, but not traded: {skipped}", event.detected_at, False
    return action, "signalled; no trade was proposed for it", event.detected_at, False


async def _own_booked(conn: AsyncConnection, account: Account) -> tuple[int, Decimal]:
    """The owner's deals by hand booked so far (S59), and what they made or lost after costs."""
    count, total = (
        await conn.execute(
            select(func.count(), func.coalesce(func.sum(ledger_entries.c.amount), 0))
            .select_from(ledger_txns)
            .join(ledger_entries, ledger_entries.c.txn_id == ledger_txns.c.id)
            .where(
                ledger_txns.c.account_id == account.id,
                ledger_txns.c.ref_type == "venue_own_trade",
                ledger_entries.c.book == "UNMANAGED",
            )
        )
    ).one()
    return int(count or 0), Decimal(str(total or 0))


async def simulation_mandate(conn: AsyncConnection, now: datetime) -> mandates.Mandate | None:
    """The gold auto-trader when it runs in the internal simulation (S60), not on HFM."""
    try:
        account = await get_account(conn, alias=SIMULATION_ACCOUNT)
    except NotFoundError:
        return None
    return next(
        (x for x in await mandates.active_mandates(conn, now) if x.account_id == account.id),
        None,
    )


async def _incidents(conn: AsyncConnection) -> list[str]:
    rows = await conn.execute(
        select(incidents.c.title, incidents.c.severity)
        .where(
            incidents.c.status.in_(("OPEN", "ACKNOWLEDGED")),
            incidents.c.severity.in_(("CRITICAL", "WARNING")),
        )
        .order_by(desc(incidents.c.opened_at))
        .limit(5)
    )
    return [f"{r.severity.title()}: {r.title}" for r in rows]


async def _waiting_trades(conn: AsyncConnection, account: Account) -> int:
    return int(
        await conn.scalar(
            select(func.count())
            .select_from(trade_intents)
            .where(trade_intents.c.account_id == account.id, trade_intents.c.status.in_(WAITING))
        )
        or 0
    )


def _no_account(now: datetime, worker: bool) -> HfmHome:
    """No HFM account here yet: a step to take, every answer said."""
    answers = [
        Answer(
            "Is my system working?",
            worker,
            "yes" if worker else "no: the worker has stopped reporting",
        ),
        Answer("Is my broker connected?", False, "no HFM account is set up here yet"),
        Answer("Is automatic trading running?", False, "no: there is no HFM account to trade"),
        Answer("Is my money protected?", None, "there is no HFM account here yet"),
        Answer("Does anything need my attention?", False, "yes: set up the HFM account"),
    ]
    return HfmHome(
        "NEEDS YOU",
        answers,
        ["Set up the HFM demo account on Operations (the bridge must report first)."],
        "NOT CONNECTED",
        market=market_state(now),
        decision_text="there is no HFM account yet",
        protection="there is no HFM account here yet",
    )


async def build(conn: AsyncConnection, now: datetime, *, unread: int = 0) -> HfmHome:
    """``unread``: the viewer's unread alerts, counted among what needs attention."""
    worker = await _worker_ok(conn, now)
    try:
        account = await get_account(conn, alias=ACCOUNT)
    except NotFoundError:
        return _no_account(now, worker)
    reported_at, snap = await _report(conn)
    fresh = reported_at is not None and now - reported_at <= mt5_bridge.STALE_AFTER
    acct = snap.get("account") or {}
    terminal = snap.get("terminal") or {}
    trade_mode = str(acct.get("trade_mode") or "")
    currency = acct.get("currency")
    # HFM's word on positions, money and deals is current only from a fresh report of a
    # connected terminal with an account: MT5 closed or logged out reports empty lists.
    facts = fresh and bool(terminal.get("connected")) and bool(trade_mode)
    out = HfmHome(
        "ALL GOOD",
        [],
        [],
        trade_mode if trade_mode in ("DEMO", "REAL") else "NOT CONNECTED",
        fresh=facts,
        reported_at=reported_at,
        currency=currency,
        balance=_d(acct.get("balance")),
        equity=_d(acct.get("equity")),
    )
    if snap:
        out.today, out.total, out.results_since = _results(snap, now)
        out.positions = _positions(snap)
    board = await autotrader.build(conn, now, ACCOUNT)
    m = board.mandate
    sim = None if m is not None else await simulation_mandate(conn, now)
    out.closed, out.wins = board.totals.closed, board.totals.wins
    out.rule = board.rule if m is not None else ""
    out.bars = board.bars if m is not None else ""
    gold_id = None
    try:
        inst = await md.instrument_by_key(conn, GOLD)
        gold_id = inst.id
        q = await md.latest_quote(conn, inst.id, now)
        if q.label in (DataLabel.LIVE, DataLabel.DELAYED):
            out.bid, out.ask, out.price_at = q.bid, q.ask, q.source_ts
    except NotFoundError:
        pass
    out.market = market_state(now)
    watch = next((w for w in board.watch if w.key == GOLD), None)
    held = any(p.symbol == SYMBOL and not p.own for p in out.positions)  # the system's gold
    out.own_open = sum(p.own for p in out.positions)
    out.own_deals, out.own_result = await _own_booked(conn, account)
    out.test_trade = await hfm_test_trade.refusal(conn, now)
    real = await hfm_ready.checklist(conn, now)
    out.real_passed, out.real_total = real.passed, len(real.checks)
    out.decision, out.decision_text, out.decision_at, waits = await _decision(
        conn, m, gold_id, watch, out.bars, held, now
    )
    latest, out.last_check = await _checks(conn, account)
    window = next_decision(m, now)
    if window is not None:
        out.next_decision, out.next_decision_end = window
    attention = await _incidents(conn)
    pending = await _waiting_trades(conn, account)
    if pending or waits:
        attention.append(
            f"{max(pending, 1)} trade(s) on your HFM account wait for your approval: see Approvals."
        )
    if unread:
        attention.append(f"{unread} unread alert(s): see Alerts.")

    # 1. Is my system working? The newest check with HFM must have passed, recently.
    if not worker:
        system = Answer("Is my system working?", False, "no: the worker has stopped reporting")
        attention.append("The worker is not running: see Operations.")
    elif latest is None or latest.status != "PASSED":
        when = f" ({latest.started_at:%d %b %H:%M} UTC)" if latest is not None else ""
        system = Answer(
            "Is my system working?",
            False,
            f"no: the last check of your account with HFM failed{when}"
            if latest is not None
            else "no: your account has not been checked with HFM yet",
        )
        attention.append(
            "The check of your HFM account with HFM did not pass: new trades are blocked until "
            "it does (see Operations)."
        )
    elif now - latest.started_at > CHECKED:
        system = Answer(
            "Is my system working?", False, "no: your account has not been checked with HFM lately"
        )
        attention.append("The check of your HFM account is late: see Operations.")
    else:
        system = Answer("Is my system working?", True, "yes; the last check with HFM passed")

    # 2. Is my broker connected? As the HFM adapter judges it: fresh, connected, a demo account,
    # money in USD, the account it was opened on, Algo Trading on.
    trading_on = (
        terminal.get("trade_allowed") is True and terminal.get("tradeapi_disabled") is not True
    )
    opened_on = await mt5_bridge.opened_on(conn)
    reported_fp = acct.get("fingerprint")
    money = currency or "an unknown currency"
    if not fresh:
        since = f" since {reported_at:%d %b %H:%M} UTC" if reported_at is not None else ""
        broker = Answer(
            "Is my broker connected?", False, f"no: the HFM bridge has not reported{since}"
        )
        attention.append(
            "The HFM bridge on the Windows VM is not reporting: check that the VM and MT5 run."
        )
    elif not terminal.get("connected"):
        broker = Answer("Is my broker connected?", False, "no: MT5 has lost its link to HFM")
        attention.append("MT5 on the Windows VM is not connected to HFM: open it and log in.")
    elif trade_mode != "DEMO":
        broker = Answer(
            "Is my broker connected?",
            False,
            f"connected, but to a {trade_mode or 'unknown'} account: every order is refused",
        )
        attention.append("The bridge reports an account that is not a demo: trading is refused.")
    elif currency != CURRENCY:
        broker = Answer(
            "Is my broker connected?",
            False,
            f"connected, but its money is in {money}: every order is refused",
        )
        attention.append(
            f"The HFM account's money is in {money}"
            + (" (a Cent account)" if currency == "USC" else "")
            + ", which this server does not handle yet: every order is refused. Log MT5 back "
            "into the USD demo account."
        )
    elif opened_on is not None and reported_fp != opened_on:
        broker = Answer(
            "Is my broker connected?",
            False,
            "connected, but to a different HFM account than the one set up here: every order "
            "is refused",
        )
        attention.append(
            "MT5 on the Windows VM is logged into another HFM account: log it back into the "
            "account this one was opened on."
        )
    elif not trading_on:
        broker = Answer(
            "Is my broker connected?",
            False,
            "connected, but Algo Trading is off in MT5: every order is refused",
        )
        attention.append("Turn Algo Trading on in MT5 on the Windows VM (the toolbar button).")
    else:
        broker = Answer("Is my broker connected?", True, "yes; HFM demo, virtual money")

    # The close is offered for a gold trade our records hold, at HFM, while it can be traded,
    # and not twice: a sale already waiting or being sent is said instead.
    out.closing = bool(
        await conn.scalar(
            select(func.count())
            .select_from(trade_intents)
            .where(
                trade_intents.c.account_id == account.id,
                trade_intents.c.side == "SELL",
                trade_intents.c.status.in_(mandates.IN_FLIGHT),
            )
        )
    )
    ours = (
        await conn.scalar(
            select(func.coalesce(func.sum(positions.c.quantity), 0)).where(
                positions.c.account_id == account.id, positions.c.instrument_id == gold_id
            )
        )
        if gold_id is not None
        else 0
    )
    out.gold_held = broker.ok is True and held and (ours or 0) > 0 and not out.closing

    # 3. Is automatic trading running?
    try:
        authority = await get_state(conn)
        allowed, why = mode_permits(
            authority.mode, Operation.OPEN_EXPOSURE, account_kind=account.kind
        )
    except ConflictError:
        allowed, why = False, "the trading mode is not known"
    too_big = (
        watch is not None
        and watch.smallest_value is not None
        and m is not None
        and m.money_cap is not None
        and watch.smallest_value > m.money_cap
    )
    if m is None and sim is not None:
        running = Answer(
            "Is automatic trading running?",
            None,
            f"in the internal simulation (SIMULATED) with {sim.capital or ZERO:,.2f}, not on your "
            "HFM account: see its card below",
        )
        out.decision_text = "the gold auto-trader runs in the internal simulation (its card below)"
    elif m is None:
        running = Answer("Is automatic trading running?", False, "no: the auto-trader is off")
        attention.append(
            "Start the gold auto-trader on Gold auto-trader: choose your amount, see the trade "
            "size, then start it with your code."
        )
    elif not worker:
        running = Answer(
            "Is automatic trading running?", False, "no: the worker that runs it has stopped"
        )
    elif not allowed:
        running = Answer("Is automatic trading running?", False, f"no: {why}")
        attention.append(f"New trades are not allowed now: {why}")
    elif GOLD not in m.instruments:
        running = Answer(
            "Is automatic trading running?",
            False,
            f"on, but not on gold: it trades {', '.join(m.instruments)}",
        )
        attention.append("Your HFM auto-trader does not trade gold: change it on Auto-trader.")
    elif system.ok is False or broker.ok is False:
        running = Answer(
            "Is automatic trading running?",
            False,
            "on, but it cannot trade now: see the answers above",
        )
    elif watch is not None and watch.fits is False and watch.smallest_value is not None:
        assert m is not None
        running = Answer(
            "Is automatic trading running?",
            False,
            f"on, but it cannot trade: its smallest gold trade ({watch.smallest_value:,.2f}) "
            f"is worth more than it may hold ({m.money_cap:,.2f})"
            if too_big and m.money_cap is not None
            else "on, but it cannot trade: its smallest gold trade would lose about "
            f"{watch.smallest_risk or ZERO:,.2f} at its stop, more than the "
            f"{m.max_loss_per_trade:,.2f} it may lose a trade",
        )
        attention.append(
            f"The smallest gold trade on this account is about {watch.smallest_value:,.2f}, "
            f"risking about {watch.smallest_risk or ZERO:,.2f} at its stop: more than the "
            "auto-trader may use, so it skips every signal. Change your amount or risk on "
            "Gold auto-trader settings (it shows the trade size first), or choose the internal "
            "simulation, which trades from 0.01 ounce on HFM's prices."
        )
    else:
        running = Answer(
            "Is automatic trading running?", True, f"yes; rule {out.rule} on {out.bars}"
        )

    # 4. Is my money protected? Only on current facts; every HFM position, gold or not, and
    # none HFM holds that the bridge does not follow (its equity or margin would show them).
    unprotected = [p for p in out.positions if p.stop is None] if facts else []
    reported = sum((p.result for p in out.positions), ZERO)
    margin = _d(acct.get("margin")) or ZERO
    hidden = facts and (
        (not out.positions and margin > ZERO)
        or (out.floating is not None and abs(out.floating - reported) > UNTRACKED)
    )
    if not facts:
        protected = Answer(
            "Is my money protected?",
            None,
            "unknown: there is no current report from HFM"
            + (f" (the last, {reported_at:%d %b %H:%M} UTC)" if reported_at is not None else ""),
        )
    elif hidden:
        protected = Answer(
            "Is my money protected?",
            False,
            "unknown positions: HFM holds trades the bridge does not follow (other symbols, "
            "or opened by hand)",
        )
        attention.append(
            "HFM's equity or margin shows trades the bridge does not report: look in MT5 on the "
            "Windows VM and close or protect them."
        )
    elif unprotected:
        names = ", ".join(sorted({p.symbol for p in unprotected}))
        protected = Answer(
            "Is my money protected?",
            False,
            f"no: {len(unprotected)} open position(s) ({names}) have no stop-loss at HFM",
        )
        if any(not p.own for p in unprotected):
            attention.append("An open HFM position has no stop-loss: see Portfolio now.")
        if any(p.own for p in unprotected):
            attention.append(
                "A trade you made by hand in MT5 has no stop-loss: set one in MT5, or close it."
            )
    else:
        held_text = (
            f"{len(out.positions)} open, each with its stop-loss held by HFM"
            if out.positions
            else "no trade open"
        )
        limits = (
            f"; each new trade risks at most {m.max_loss_per_trade:,.2f} at its stop, after a "
            f"day's loss of {m.daily_loss_limit:,.2f} it opens nothing new that day, and it "
            f"stops for good after losing {m.total_limit:,.2f}"
            if m is not None
            else ""
        )
        protected = Answer("Is my money protected?", True, f"yes: {held_text}{limits}")
    out.protection = protected.text

    out.attention = attention
    out.answers = [
        system,
        broker,
        running,
        protected,
        Answer(
            "Does anything need my attention?",
            not attention,
            "nothing" if not attention else f"yes: {len(attention)} thing(s), listed below",
        ),
    ]
    if system.ok is False or broker.ok is False or unprotected or hidden:
        out.status = "PROBLEM"
    elif attention or any(a.ok is not True for a in out.answers):
        out.status = "NEEDS YOU"
    return out


def addis(t: datetime | None) -> str:
    """A time for the owner: UTC and Addis Ababa's."""
    if t is None:
        return "—"
    return f"{t:%d %b %H:%M} UTC ({t.astimezone(ADDIS):%H:%M} in Addis Ababa)"


def age(t: datetime | None, now: datetime) -> str:
    """How long ago, in words."""
    if t is None:
        return "never"
    s = int((now - t).total_seconds())
    if s < 120:
        return f"{max(s, 0)} s ago"
    if s < 7200:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"
