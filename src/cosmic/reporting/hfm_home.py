"""The HFM gold home page (S55): the owner's HFM account at a glance, for a beginner.

It answers five questions first: is the system working, is the broker connected, is automatic
trading running, is the money protected, and does anything need the owner. Then the account:

- **from HFM's own report** through the bridge: whether it is a DEMO or a REAL account, its
  balance, equity and currency, the open positions with their stop-losses, and the deals that
  give today's and the total result;
- from our records, reconciled against HFM every five minutes: the auto-trader's decisions,
  its limits and the last successful check.

Nothing simulated is shown here: the paper rehearsals have their own page. It changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, NotFoundError
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema import incidents, notifications, worker_heartbeats
from cosmic.db.schema_execution import reconciliation_runs
from cosmic.db.schema_market import market_events
from cosmic.db.schema_trading import bridge_state
from cosmic.market import data as md
from cosmic.reporting import autotrader
from cosmic.research.library import expect
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.authority import get_state
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.modes import Operation, mode_permits

ACCOUNT = "hfm-demo"  # the HFM account the bridge trades (a SANDBOX account on HFM's demo)
GOLD = "CFD:XAU/USD@HFM"
SYMBOL = "XAUUSD"
CURRENCY = "USD"  # the money the server understands from this bridge (a Cent account is not yet)
WORKER_ALIVE = timedelta(seconds=60)
CHECKED = timedelta(minutes=15)  # the account is reconciled with HFM every five minutes
NEW_YORK = ZoneInfo("America/New_York")
ADDIS = ZoneInfo("Africa/Addis_Ababa")
DEAL_TRADES = (0, 1)  # MT5 deal types BUY and SELL; others are deposits, credits and the like


@dataclass(frozen=True)
class Answer:
    question: str
    ok: bool | None  # True: yes; False: no, it needs the owner; None: not applicable now
    text: str


@dataclass(frozen=True)
class Position:
    """An open position as HFM reports it."""

    lots: Decimal
    ounces: Decimal
    price_open: Decimal
    price_now: Decimal | None
    stop: Decimal | None  # HFM's stop-loss on it; None when it has none
    result: Decimal  # HFM's floating profit, with its swap
    opened_at: datetime | None


@dataclass
class HfmHome:
    status: str  # ALL GOOD | NEEDS YOU | PROBLEM
    answers: list[Answer]
    attention: list[str]
    mode: str  # DEMO | REAL | NOT CONNECTED
    reported_at: datetime | None = None
    currency: str | None = None
    balance: Decimal | None = None
    equity: Decimal | None = None
    today: Decimal | None = None
    total: Decimal | None = None
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
    last_check: datetime | None = None
    next_decision: datetime | None = None
    rule: str = ""
    bars: str = ""

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
    """MT5 stamps in the broker's server time; the bridge reports its offset from UTC."""
    try:
        return datetime.fromtimestamp((int(ms) - int(offset or 0)) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def market_state(now: datetime) -> str:
    """Gold at HFM: closed from Friday 17:00 to Sunday 18:00 New York, with a short daily break
    from the 17:00 rollover."""
    if expect.weekend_closed_at(now):
        return "closed for the weekend (reopens Sunday 18:00 in New York)"
    ny = now.astimezone(NEW_YORK)
    if ny.hour == 17:
        return "in HFM's daily break (about an hour from 17:00 in New York)"
    return "open"


def next_decision(m: mandates.Mandate | None, now: datetime) -> datetime | None:
    """When the auto-trader next decides: a few minutes after the next bar completes (it runs
    every five minutes); a daily rule on gold after the weekend's last bar, on Monday."""
    if m is None:
        return None
    if m.timeframe == mandates.DAILY:
        at = mandates.day_start(now) + timedelta(days=1, minutes=5)
        while expect.weekend_closed_at(at - timedelta(minutes=5)):
            at += timedelta(days=1)
        return at
    at = now.replace(minute=5, second=0, microsecond=0)
    return at if at > now else at + timedelta(hours=1)


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
    offset = snap.get("server_utc_offset_ms")
    contract = _d(((snap.get("symbols") or {}).get(SYMBOL) or {}).get("contract_size"))
    out: list[Position] = []
    for p in snap.get("positions") or ():
        if p.get("symbol") != SYMBOL:
            continue
        lots = _d(p.get("volume")) or ZERO
        stop = _d(p.get("sl"))
        out.append(
            Position(
                lots=lots,
                ounces=lots * contract if contract else lots,
                price_open=_d(p.get("price_open")) or ZERO,
                price_now=_d(p.get("price_current")),
                stop=stop if stop else None,
                result=(_d(p.get("profit")) or ZERO) + (_d(p.get("swap")) or ZERO),
                opened_at=_server_time(p.get("time_msc"), offset),
            )
        )
    return out


def _results(snap: dict[str, Any], now: datetime) -> tuple[Decimal, Decimal]:
    """(today's result, the result over the report's history) of HFM's trade deals, after
    commission, swap and fees."""
    offset = snap.get("server_utc_offset_ms")
    start = mandates.day_start(now)
    today = total = ZERO
    for d in snap.get("deals") or ():
        if d.get("type") not in DEAL_TRADES:
            continue
        value = sum(((_d(d.get(k)) or ZERO) for k in ("profit", "commission", "swap", "fee")), ZERO)
        total += value
        at = _server_time(d.get("time_msc"), offset)
        if at is not None and at >= start:
            today += value
    return today, total


async def _worker_ok(conn: AsyncConnection, now: datetime) -> bool:
    beat = await conn.scalar(
        select(func.max(worker_heartbeats.c.last_beat_at)).where(
            worker_heartbeats.c.process == "worker"
        )
    )
    return beat is not None and now - beat <= WORKER_ALIVE


async def _last_check(conn: AsyncConnection, account: Account) -> datetime | None:
    at: datetime | None = await conn.scalar(
        select(func.max(reconciliation_runs.c.started_at)).where(
            reconciliation_runs.c.account_id == account.id,
            reconciliation_runs.c.status == "PASSED",
        )
    )
    return at


async def _decision(
    conn: AsyncConnection, board: autotrader.Board, account: Account, now: datetime
) -> tuple[str, str, datetime | None]:
    """The auto-trader's latest decision on gold: BUY or SELL when its rule signalled (with what
    came of it), else WAIT with what it waits for."""
    m = board.mandate
    if m is None:
        return "WAIT", "the auto-trader is off", None
    event = (
        await conn.execute(
            select(market_events.c.detail, market_events.c.detected_at)
            .where(
                market_events.c.kind == "AUTO_SIGNAL",
                market_events.c.detail["mandate"].astext == str(m.id),
            )
            .order_by(desc(market_events.c.detected_at))
            .limit(1)
        )
    ).first()
    watch = next((w for w in board.watch if w.key == GOLD), None)
    waiting = (
        f"no buy signal yet; it buys if gold closes above {watch.level:,.2f} on {board.bars}"
        if watch is not None and watch.level is not None
        else f"no buy signal yet on {board.bars}"
    )
    if event is None or now - event.detected_at > timedelta(days=2):
        return "WAIT", waiting, None
    action = "BUY" if event.detail.get("action") == "ENTER" else "SELL"
    skipped = await conn.scalar(
        select(notifications.c.payload["reason"].astext)
        .where(
            notifications.c.kind == "auto_trade_skipped",
            notifications.c.created_at >= event.detected_at,
            notifications.c.payload["account"].astext == account.alias,
        )
        .order_by(desc(notifications.c.created_at))
        .limit(1)
    )
    if skipped:
        return action, f"signalled, but not traded: {skipped}", event.detected_at
    return action, "signalled and sent to HFM", event.detected_at


async def _attention(conn: AsyncConnection) -> list[str]:
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


async def build(conn: AsyncConnection, now: datetime) -> HfmHome:
    try:
        account = await get_account(conn, alias=ACCOUNT)
    except NotFoundError:
        return HfmHome(
            "NEEDS YOU",
            [Answer("Is my broker connected?", False, "no HFM account is set up here yet")],
            ["Set up the HFM demo account on Operations."],
            "NOT CONNECTED",
        )
    reported_at, snap = await _report(conn)
    fresh = reported_at is not None and now - reported_at <= mt5_bridge.STALE_AFTER
    acct = snap.get("account") or {}
    terminal = snap.get("terminal") or {}
    trade_mode = str(acct.get("trade_mode") or "")
    currency = acct.get("currency")
    out = HfmHome(
        "ALL GOOD",
        [],
        [],
        trade_mode if trade_mode in ("DEMO", "REAL") else "NOT CONNECTED",
        reported_at=reported_at,
        currency=currency,
        balance=_d(acct.get("balance")),
        equity=_d(acct.get("equity")),
    )
    if snap:
        out.today, out.total = _results(snap, now)
        out.positions = _positions(snap)
    board = await autotrader.build(conn, now, ACCOUNT)
    m = board.mandate
    out.closed, out.wins = board.totals.closed, board.totals.wins
    out.rule = board.rule if m is not None else ""
    out.bars = board.bars if m is not None else ""
    try:
        inst = await md.instrument_by_key(conn, GOLD)
        q = await md.latest_quote(conn, inst.id, now)
        if q.label in (DataLabel.LIVE, DataLabel.DELAYED):
            out.bid, out.ask, out.price_at = q.bid, q.ask, q.source_ts
    except NotFoundError:
        pass
    out.market = market_state(now)
    out.decision, out.decision_text, out.decision_at = await _decision(conn, board, account, now)
    out.last_check = await _last_check(conn, account)
    out.next_decision = next_decision(m, now)
    attention = await _attention(conn)

    # 1. Is my system working?
    worker = await _worker_ok(conn, now)
    checked = out.last_check is not None and now - out.last_check <= CHECKED
    if worker and checked:
        system = Answer("Is my system working?", True, "yes; the last check with HFM passed")
    elif not worker:
        system = Answer("Is my system working?", False, "no: the worker has stopped reporting")
        attention.append("The worker is not running: see Operations.")
    else:
        system = Answer(
            "Is my system working?", False, "no: the account has not been checked with HFM lately"
        )
        attention.append("The check of your HFM account is late or failed: see Operations.")

    # 2. Is my broker connected?
    if not fresh:
        broker = Answer("Is my broker connected?", False, "no: the HFM bridge is not reporting")
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
            f"connected, but its money is in {currency}: every order is refused",
        )
        attention.append(
            f"The HFM account's money is in {currency}, which the server does not handle yet "
            "(a Cent account): every order is refused until it does."
        )
    else:
        broker = Answer("Is my broker connected?", True, "yes; HFM demo, virtual money")

    # 3. Is automatic trading running?
    try:
        authority = await get_state(conn)
        allowed, why = mode_permits(
            authority.mode, Operation.OPEN_EXPOSURE, account_kind=account.kind
        )
    except ConflictError:
        allowed, why = False, "the trading mode is not known"
    watch = next((w for w in board.watch if w.key == GOLD), None)
    if m is None:
        running = Answer("Is automatic trading running?", False, "no: the auto-trader is off")
        attention.append("Start the HFM auto-trader on Auto-trader, with your code.")
    elif not allowed:
        running = Answer("Is automatic trading running?", False, f"no: {why}")
        attention.append(f"New trades are not allowed now: {why}")
    elif watch is not None and watch.fits is False and watch.smallest_value is not None:
        running = Answer(
            "Is automatic trading running?",
            False,
            "on, but it cannot trade: its smallest gold trade "
            f"({watch.smallest_value:,.2f}) is more than it may use",
        )
        attention.append(
            f"The smallest gold trade on this account is about {watch.smallest_value:,.2f} "
            f"(risking about {watch.smallest_risk or ZERO:,.2f} at its stop), more than the "
            "auto-trader's money or loss per trade: it skips every signal. A Cent account "
            "trades 0.01 ounce."
        )
    else:
        running = Answer(
            "Is automatic trading running?", True, f"yes; rule {out.rule} on {out.bars}"
        )

    # 4. Is my money protected?
    unprotected = [p for p in out.positions if p.stop is None]
    if unprotected:
        protected = Answer(
            "Is my money protected?",
            False,
            f"no: {len(unprotected)} open position(s) have no stop-loss at HFM",
        )
        attention.append("An open HFM position has no stop-loss: see Portfolio now.")
    elif m is not None:
        stop_text = (
            f"each trade risks at most {m.max_loss_per_trade:,.2f} at its stop, the day at most "
            f"{m.daily_loss_limit:,.2f}, and it stops for good after losing {m.total_limit:,.2f}"
        )
        held = (
            f"{len(out.positions)} open, each with its stop-loss held by HFM; "
            if out.positions
            else "no trade open; "
        )
        protected = Answer("Is my money protected?", True, f"yes: {held}{stop_text}")
    else:
        protected = Answer(
            "Is my money protected?",
            True if not out.positions else None,
            "no trade is open" if not out.positions else "positions held; the auto-trader is off",
        )
    out.protection = protected.text

    out.attention = attention
    worst = [system, broker]
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
    if any(a.ok is False for a in worst) or unprotected:
        out.status = "PROBLEM"
    elif attention or any(a.ok is False for a in out.answers):
        out.status = "NEEDS YOU"
    return out


def addis(t: datetime | None) -> str:
    """A time for the owner: UTC and Addis Ababa's."""
    if t is None:
        return "—"
    return f"{t:%d %b %H:%M} UTC ({t.astimezone(ADDIS):%H:%M} in Addis Ababa)"
