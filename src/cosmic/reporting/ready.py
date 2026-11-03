"""Is the simulated gold auto-trader ready? (S52)

The home page's first panel, for the owner: a plain checklist of what the two $100 rehearsals
need, how each run stands, and the one next step. Everything is read from the places that
decide: the worker's own heartbeat, the authority mode, the risk policy and the account-level
risk checks (R03, R08 and R15, shared with the engine), the auto-trader's own rules for its
daily bars, the stored quotes and the HFM bridge's report, and the runs, sorted as Start with
$100 sorts them. It changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, NotFoundError
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema import worker_heartbeats
from cosmic.db.schema_market import bars
from cosmic.db.schema_trading import bridge_state
from cosmic.market import data as md
from cosmic.research.library import auto, expect
from cosmic.trading import mandates
from cosmic.trading import rehearsal as r
from cosmic.trading.accounts import Account
from cosmic.trading.authority import get_state
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.context_builder import account_state, critical_categories
from cosmic.trading.modes import DESCRIPTIONS, Mode, Operation, mode_permits
from cosmic.trading.policy import RiskPolicyParams, active_policy
from cosmic.trading.risk.checks import Status, incidents_check, integrity, loss_limits
from cosmic.trading.risk.context import AccountState

WORKER_ALIVE = timedelta(seconds=60)  # as the home page's own attention list
# The daily bars as the auto-trader judges them (research/library/auto.py): the newest completed
# one closed at most MAX_BAR_LAG ago, and enough of them for its longest warm-up.
DAILY_FRESH = auto.BAR_LENGTH[auto.DAILY] + auto.MAX_BAR_LAG[auto.DAILY]
MIN_DAILY = auto.MIN_BARS[auto.DAILY]
FRESH = (DataLabel.LIVE, DataLabel.DELAYED)
GRACE = timedelta(minutes=10)  # a price this late is no problem yet: the auto-trader retries
BRIDGED = frozenset({"hfm-cent"})  # its prices come through the HFM bridge
HFM_GOLD = "XAUUSD"  # HFM's MetaTrader 5 symbol for gold (sources/registry.py)
STORED = timedelta(seconds=60)  # HFM's newest tick not stored this long after it: not stored
BREAK_UNTIL_NY_HOUR = 19  # HFM's gold pauses daily from its rollover, 17:00 in New York
REOPENING = timedelta(hours=1)  # the first price after the Sunday reopening may take a while


@dataclass(frozen=True)
class Check:
    ok: bool | None  # None: no problem, but not done (a rehearsal not started, a pause)
    text: str
    kind: str = ""  # what it is about: system, incident, policy, market, price, setup, run


@dataclass(frozen=True)
class Run:
    """One running rehearsal as the owner sees it."""

    key: str
    name: str
    started: bool
    rule: str | None = None
    money: Decimal | None = None
    currency: str = ""
    until: datetime | None = None
    renews: bool = False


@dataclass
class Ready:
    status: str  # RUNNING | READY TO START | NEEDS YOU | PROBLEM | ENDED
    checks: list[Check] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    next_step: str = ""
    next_link: str = ""
    next_label: str = ""  # the button that takes the owner there
    start_money: Decimal = r.START
    floor: Decimal = r.FLOOR

    def go(self, status: str, step: str, link: str, label: str) -> None:
        self.status, self.next_step, self.next_link, self.next_label = status, step, link, label


@dataclass(frozen=True)
class _State:
    """One rehearsal: its line, its run if one is active, and what it asks of the owner."""

    check: Check
    run: Run | None = None
    step: str = ""  # start | renew | ops | policy | unrenewed: the owner's step for it
    going: bool = False  # it runs, or goes on by itself


@dataclass(frozen=True)
class _Bridge:
    reporting: bool = False
    connected: bool = False
    tick_at: datetime | None = None  # HFM's newest gold tick as the bridge last saw it (UTC)


def _span(t: timedelta) -> str:
    minutes = max(int(t.total_seconds() // 60), 0)
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} days"


async def _worker_ok(conn: AsyncConnection, now: datetime) -> bool:
    """The worker itself beats (the Telegram bot's heartbeat is its own)."""
    beat = await conn.scalar(
        select(func.max(worker_heartbeats.c.last_beat_at)).where(
            worker_heartbeats.c.process == "worker"
        )
    )
    return beat is not None and now - beat <= WORKER_ALIVE


async def _bridge(conn: AsyncConnection, now: datetime) -> _Bridge:
    """The HFM bridge's last report, judged as the HFM adapter judges it."""
    row = (
        await conn.execute(
            select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                bridge_state.c.bridge == mt5_bridge.BRIDGE
            )
        )
    ).first()
    if row is None or now - row.received_at > mt5_bridge.STALE_AFTER:
        return _Bridge()
    snap: dict[str, Any] = row.snapshot or {}
    tick_at = None
    try:  # MT5 stamps ticks in the broker's server time
        ms = ((snap.get("ticks") or {}).get(HFM_GOLD) or {}).get("time_msc")
        offset = snap.get("server_utc_offset_ms")
        if ms is not None and offset is not None:
            tick_at = datetime.fromtimestamp((int(ms) - int(offset)) / 1000, tz=UTC)
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        tick_at = None
    return _Bridge(True, bool((snap.get("terminal") or {}).get("connected")), tick_at)


async def _daily(conn: AsyncConnection, series: str, now: datetime) -> str | None:
    """What keeps the auto-trader from deciding on ``series``'s daily bars, or None."""
    try:
        sig = await md.instrument_by_key(conn, series)
    except NotFoundError:
        return "its daily gold prices are not set up here"
    newest = await md.complete_bars(conn, sig.id, mandates.DAILY, limit=1)
    if not newest or now - newest[-1].open_time > DAILY_FRESH:
        return "daily gold prices are not up to date"
    days = await conn.scalar(
        select(func.count())
        .select_from(bars)
        .where(
            bars.c.instrument_id == sig.id,
            bars.c.timeframe == mandates.DAILY,
            bars.c.is_complete.is_(True),
        )
    )
    if (days or 0) < MIN_DAILY:
        return f"only {days or 0} of the {MIN_DAILY} days of gold prices the auto-trader needs"
    return None


def _quote_fault(name: str, q: md.QuoteView) -> Check | None:
    if q.label is DataLabel.UNKNOWN:
        text = "its latest price carries a time in the future, so it is not used"
        return Check(False, f"{name}: {text}", "price")
    if q.label is DataLabel.UNAVAILABLE and q.source_ts is not None:
        return Check(False, f"{name}: its price source is switched off", "price")
    return None


def _binance(name: str, q: md.QuoteView, now: datetime) -> Check:
    if q.label in FRESH:
        return Check(True, f"{name}: daily gold prices up to date, live prices arriving", "price")
    if (fault := _quote_fault(name, q)) is not None:
        return fault
    if q.source_ts is None:
        return Check(False, f"{name}: no live price has arrived from Binance yet", "price")
    late = now - q.source_ts
    if late <= GRACE:
        text = f"the latest live price came {_span(late)} ago; the auto-trader waits for the next"
        return Check(None, f"{name}: {text}", "price")
    text = f"live prices are not arriving from Binance (the latest {_span(late)} ago)"
    return Check(False, f"{name}: {text}", "price")


def _hfm(name: str, q: md.QuoteView, b: _Bridge, now: datetime) -> Check:
    """HFM's gold closes at weekends and pauses daily from its rollover; what HFM does not send
    is not a problem here, but a bridge that does not report or prices it does not store are."""
    if q.label in FRESH:
        return Check(True, f"{name}: daily gold prices up to date, live prices arriving", "price")
    if (fault := _quote_fault(name, q)) is not None:
        return fault
    closed = expect.weekend_closed_at(now)
    if not b.reporting:
        text = "the HFM bridge is not reporting" + (
            " (HFM's gold is closed for the weekend, but the bridge must run before it reopens)"
            if closed
            else ""
        )
        return Check(False, f"{name}: {text}", "price")
    if closed:
        text = "daily gold prices up to date; HFM's gold is closed for the weekend"
        return Check(True, f"{name}: {text}", "price")
    if not b.connected:
        return Check(False, f"{name}: the HFM bridge's terminal is not connected to HFM", "price")
    if b.tick_at is not None and (q.source_ts is None or b.tick_at - q.source_ts > STORED):
        text = "HFM's gold prices reach the bridge but are not stored here"
        return Check(False, f"{name}: {text}", "price")
    last = b.tick_at or q.source_ts
    if last is None:
        return Check(False, f"{name}: no HFM gold price has arrived yet", "price")
    quiet = max(now - last, timedelta(0))
    if expect.weekend_closed_at(now - REOPENING):
        text = "HFM's gold has just reopened after the weekend: waiting for its first price"
        return Check(None, f"{name}: {text}", "price")
    if expect.ROLLOVER_NY_HOUR <= now.astimezone(expect.NEW_YORK).hour < BREAK_UNTIL_NY_HOUR:
        text = "HFM's daily gold break (from 17:00 in New York): prices resume after it"
        return Check(None, f"{name}: {text}", "price")
    if quiet <= GRACE:
        text = f"no new HFM gold price for {_span(quiet)}; the bridge is connected"
        return Check(None, f"{name}: {text}", "price")
    text = (
        f"HFM has sent no new gold price for {_span(quiet)} although its terminal is connected: "
        "a market holiday, or a frozen terminal"
    )
    return Check(False, f"{name}: {text}", "price")


async def _prices(conn: AsyncConnection, now: datetime) -> list[Check]:
    out: list[Check] = []
    daily: dict[str, str | None] = {}
    bridge: _Bridge | None = None
    for key, v in r.REHEARSALS.items():
        try:
            inst = await md.instrument_by_key(conn, v.market)
        except NotFoundError:
            out.append(Check(False, f"{v.name}: its market is not set up here", "market"))
            continue
        if not inst.tradable:
            out.append(Check(False, f"{v.name}: its market is not tradable here", "market"))
            continue
        series = mandates.AUTO_SIGNAL_SERIES.get(v.market, v.market)
        if series not in daily:
            daily[series] = await _daily(conn, series, now)
        if (why := daily[series]) is not None:
            out.append(Check(False, f"{v.name}: {why}", "price"))
            continue
        quote = await md.latest_quote(conn, inst.id, now)
        if key in BRIDGED:
            if bridge is None:
                bridge = await _bridge(conn, now)
            out.append(_hfm(v.name, quote, bridge, now))
        else:
            out.append(_binance(v.name, quote, now))
    return out


def _refusals(
    acct: Account, st: AccountState | None, policy: RiskPolicyParams | None, now: datetime
) -> tuple[list[str], str]:
    """What the account-level risk checks refuse its new trades for, and where that is set."""
    out: list[str] = []
    where = ""
    if acct.status != "ACTIVE":
        out.append(f"its account is {acct.status} (R02)")
        where = "ops"
    if st is not None and policy is not None:
        r03 = integrity(st, now, policy)
        if r03.status is Status.FAIL and st.reconciliation_status != "UNKNOWN":  # see _state
            out.append(f"{r03.message} (R03)")
            where = where or "ops"
        r08 = loss_limits(st, policy)
        if r08.status is Status.FAIL:  # unknown while a price is missing: the prices say so
            out.append(f"{r08.message} (R08)")
            where = where or "policy"
    return out, where


async def _ended(
    conn: AsyncConnection, key: str, name: str, last: mandates.Mandate, now: datetime
) -> _State:
    """A rehearsal whose last run is not active, as Start with $100 tells it."""
    if last.status == "STOPPED":
        text = (
            f"stopped for good before one more loss could take it below {r.FLOOR:,.2f}: that "
            "is its result"
        )
        return _State(Check(None, f"{name}: {text}", "run"))
    renews = (
        last.auto_renew
        and last.status in ("ACTIVE", "EXPIRED")
        and not await mandates.renewal_stopped(conn, last.id)
    )
    if last.status == "ACTIVE":  # past its end: the worker ends it within minutes, then says
        text = "its run has just ended; " + (
            "it renews itself within minutes" if renews else "you may renew it in a few minutes"
        )
        return _State(Check(None, f"{name}: {text}", "run"), going=True)
    if renews:
        why = await r.renewal_blocker(conn, key, now)
        if why is not None and why.startswith(r.NEEDS_YOU):
            text = f"its run ended and could not renew itself: {why.removeprefix(r.NEEDS_YOU)}"
            return _State(Check(False, f"{name}: {text}", "run"), step="unrenewed")
        text = f"its run has ended; {why or 'it renews itself within minutes'}"
        return _State(Check(None, f"{name}: {text}", "run"), going=True)
    ended = "you ended its run" if last.status == "REVOKED" else "its run has ended"
    text = f"{ended}; you may renew it from the money it holds now"
    return _State(Check(None, f"{name}: {text}", "run"), step="renew")


async def _state(
    conn: AsyncConnection,
    key: str,
    active: dict[Any, mandates.Mandate],
    policy: RiskPolicyParams | None,
    now: datetime,
) -> _State:
    v = r.REHEARSALS[key]
    acct = await r.account(conn, key)
    recent = (
        [] if acct is None else await mandates.recent_mandates(conn, limit=1, account_id=acct.id)
    )
    if acct is None or not recent:
        return _State(Check(None, f"{v.name}: not started yet", "run"), step="start")
    m = active.get(acct.id)
    if m is None:
        return await _ended(conn, key, v.name, recent[0], now)
    try:
        st: AccountState | None = await account_state(conn, acct, now)
    except ConflictError:
        st = None
    renews = m.auto_renew and not await mandates.renewal_stopped(conn, m.id)
    rule = mandates.AUTO_RULES.get(m.strategy_key, m.strategy_key)
    money = None if st is None else st.equity
    run = Run(key, v.name, True, rule, money, acct.base_currency, m.expires_at, renews)
    refusals, where = _refusals(acct, st, policy, now)
    if refusals:
        text = f"running, but the risk checks refuse its new trades: {'; '.join(refusals)}"
        return _State(Check(False, f"{v.name}: {text}", "run"), run, step=where)
    if acct.reconciliation_status == "UNKNOWN":  # a new account: reconciled at once (S37)
        text = "started; the worker reconciles its new account within minutes, then it may trade"
        return _State(Check(None, f"{v.name}: {text}", "run"), run, going=True)
    u = await mandates.usage(conn, m, now)
    lost = -u.realized_total
    kept = (  # HFM's overnight swap (S54); no valid policy is told above, not raised here
        ZERO if policy is None else await mandates.swap_reserve(conn, m, now, policy)
    )
    if lost + m.max_loss_per_trade + kept > m.total_limit:  # no room for another full loss
        text = (
            "its last trade is open: when it closes, the run stops for good"
            if u.open_positions
            else "it stops for good now"
        ) + f" (lost {max(lost, ZERO):,.2f} of the {m.total_limit:,.2f} it may lose)"
        return _State(Check(None, f"{v.name}: {text}", "run"), run, going=True)
    text = f"running, rule {rule}" + (", renews itself" if renews else "")
    return _State(Check(True, f"{v.name}: {text}", "run"), run, going=True)


def _names(states: dict[str, _State], step: str) -> str:
    return " and ".join(r.REHEARSALS[k].name for k, s in states.items() if s.step == step)


async def build(conn: AsyncConnection, now: datetime) -> Ready:
    authority = await get_state(conn)
    paper_ok, _ = mode_permits(authority.mode, Operation.OPEN_EXPOSURE, account_kind="PAPER")
    worker_ok = await _worker_ok(conn, now)
    if not worker_ok:
        system = Check(False, "The worker that runs the auto-trader is not running", "system")
    elif not paper_ok:
        system = Check(
            False,
            f"New trades are not allowed now: the mode is {authority.mode.value}. "
            f"{DESCRIPTIONS[authority.mode]}",
            "system",
        )
    else:
        system = Check(True, "The system is running and may trade simulated money", "system")
    checks = [system]
    incident = incidents_check(await critical_categories(conn))
    if incident.status is not Status.PASS:
        text = f"Every new trade is refused: {incident.message} (see Alerts)"
        checks.append(Check(False, text, "incident"))
    policy: RiskPolicyParams | None
    try:
        _, policy = await active_policy(conn)
    except ConflictError:
        policy = None
        checks.append(
            Check(False, "There is no valid risk policy: every trade is refused", "policy")
        )
    checks += await _prices(conn, now)
    set_up = policy is not None
    if policy is not None:
        for key, v in r.REHEARSALS.items():
            fee, market = await r.fee_set(conn, key), await r.allowed(conn, key)
            set_up = set_up and fee and market
            text = (
                "fee and market set"
                if fee and market
                else ("its fee is not set" if not fee else "its market is not allowed yet")
            )
            checks.append(Check(fee and market, f"{v.name}: {text}", "setup"))
    active = {m.account_id: m for m in await mandates.active_mandates(conn, now)}
    states = {key: await _state(conn, key, active, policy, now) for key in r.REHEARSALS}
    checks += [s.check for s in states.values()]
    out = Ready("RUNNING", checks, [s.run for s in states.values() if s.run is not None])
    wrong = {c.kind for c in checks if c.ok is False}
    steps = {s.step for s in states.values()}
    if not worker_ok:
        out.go(
            "PROBLEM",
            "Nothing can trade until the worker runs again: see Operations (the server may "
            "need a restart).",
            "/ops",
            "Open Operations",
        )
    elif not paper_ok:
        out.go(
            "NEEDS YOU",
            "The emergency stop is on: on Operations, follow its recovery checklist, with your "
            "code."
            if authority.mode is Mode.EMERGENCY_STOP
            else f"The mode is {authority.mode.value}: on Operations, switch it to "
            "PAPER_TRADING with your code when you want trades again.",
            "/ops",
            "Open Operations",
        )
    elif "incident" in wrong:
        out.go(
            "NEEDS YOU",
            "A critical incident is open, and every new trade is refused until it is resolved: "
            "under Alerts, each says what happened and what to do.",
            "/alerts",
            "Open Alerts",
        )
    elif "policy" in wrong:
        out.go(
            "NEEDS YOU",
            "Set a valid risk policy under Risk policy, with your code.",
            "/settings/risk",
            "Open Risk policy",
        )
    elif "market" in wrong:
        out.go(
            "PROBLEM",
            "A rehearsal's market is missing or not tradable here (see the checks below): see "
            "Sources.",
            "/sources",
            "Open Sources",
        )
    elif "ops" in steps:
        out.go(
            "NEEDS YOU",
            f"The risk checks refuse new trades on {_names(states, 'ops')} (see the checks "
            "below): see Operations.",
            "/ops",
            "Open Operations",
        )
    elif "policy" in steps:
        out.go(
            "NEEDS YOU",
            f"Your risk policy refuses new trades on {_names(states, 'policy')} (see the checks "
            "below): if you agree, raise that limit under Risk policy with your code; otherwise "
            "end the run on Start with $100.",
            "/settings/risk",
            "Open Risk policy",
        )
    elif "unrenewed" in steps:
        out.go(
            "NEEDS YOU",
            f"{_names(states, 'unrenewed')} could not renew itself (see the checks below): "
            "Start with $100 says why.",
            "/rehearsal#go-title",
            "See why",
        )
    elif not set_up:
        out.go(
            "NEEDS YOU",
            "On Start with $100, press Apply with my code in “Get them ready in one step”.",
            "/rehearsal#ready-title",
            "Get them ready",
        )
    elif steps & {"start", "renew"}:
        parts = []
        if "start" in steps:
            parts.append(
                f"start {_names(states, 'start')}: choose a daily rule, tick “Renew it by "
                "itself” and press Start this rehearsal"
            )
        if "renew" in steps:
            parts.append(f"renew {_names(states, 'renew')} from the money it holds now")
        out.go(
            "READY TO START",
            "On Start with $100, " + "; ".join(parts) + ", with your code.",
            "/rehearsal#go-title",
            "Start them" if "start" in steps else "Renew",
        )
    elif "price" in wrong:
        out.go(
            "PROBLEM",
            "Prices are missing (see the checks below): the auto-trader waits for them. Sources "
            "shows each price feed and when it last arrived.",
            "/sources",
            "Open Sources",
        )
    elif not any(s.going for s in states.values()):
        out.go(
            "ENDED",
            f"Each rehearsal stopped for good before one more loss could take it below "
            f"{r.FLOOR:,.2f}: that is its result. Start with $100 shows how it went.",
            "/rehearsal",
            "See how it went",
        )
    else:
        out.go(
            "RUNNING",
            "Nothing to do: each rehearsal decides once a day, just after 00:00 UTC (03:00 in "
            "Addis Ababa).",
            "/rehearsal",
            "See the rehearsals",
        )
    return out
