"""Is an HFM real account ready? The owner's separate checklist (S58), read from evidence.

The owner asked (2026-11-03) for a safe way from the HFM demo to a real account, with "a separate
real-account readiness checklist", and "do not claim production readiness until the evidence
supports it". Each check here is PASSED, NOT MET or BLOCKED from what is recorded:

- HFM's own report;
- the reconciliations and the incidents;
- the demo's own orders.

A setting that merely exists never passes a check. Nothing here opens anything. Real money at HFM
needs, besides every check:

- a real-account bridge (its own MT5 and token), which this build does not have;
- an HFM live adapter, which this build does not have (``LIVE_ADAPTERS``, ADR-034 lock 1);
- the owner's activation with their code.

The bridge program on the VM refuses to trade a real account by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import NotFoundError
from cosmic.db.schema import incidents
from cosmic.db.schema_execution import (
    fills,
    orders,
    post_trade_reviews,
    reconciliation_items,
    reconciliation_runs,
    trade_intents,
)
from cosmic.db.schema_market import quotes
from cosmic.db.schema_trading import accounts, bridge_state
from cosmic.market import data as md
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.authority import LIVE_ADAPTERS
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.brokers.hfm_trade_check import session_closed
from cosmic.trading.rehearsal import REHEARSALS

ACCOUNT = "hfm-demo"
GOLD = "CFD:XAU/USD@HFM"
PASSED, NOT_MET, BLOCKED = "PASSED", "NOT MET", "BLOCKED"
WEEK = timedelta(days=7)
MONTH = timedelta(days=30)
RELIABLE = Decimal("99")  # % of the week's checks with HFM that passed
ENOUGH_CHECKS = 288  # a day of five-minute checks: fewer is too short to judge
PRICE_FRESH = timedelta(seconds=60)
FORWARD_DAYS = 30
OUTAGES = ("data.bridge_offline:" + ACCOUNT, "accounting.venue_unreachable:" + ACCOUNT)


@dataclass(frozen=True)
class Check:
    title: str
    status: str  # PASSED | NOT MET | BLOCKED
    evidence: list[str] = field(default_factory=list)
    next_step: str = ""


@dataclass(frozen=True)
class Readiness:
    checks: list[Check]

    @property
    def passed(self) -> int:
        return sum(c.status == PASSED for c in self.checks)

    @property
    def ready(self) -> bool:
        return all(c.status == PASSED for c in self.checks)


def _pct(part: int, whole: int) -> Decimal:
    return (Decimal(100) * part / whole).quantize(Decimal("0.01")) if whole else Decimal(0)


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"


async def _connected(conn: AsyncConnection, now: datetime) -> Check:
    title = "Your HFM demo is connected"
    row = (
        await conn.execute(select(bridge_state).where(bridge_state.c.bridge == mt5_bridge.BRIDGE))
    ).first()
    if row is None:
        return Check(title, NOT_MET, ["the HFM bridge has never reported"], "Start the bridge.")
    snap = row.snapshot or {}
    problem = mt5_bridge.report_problem(
        snap, row.received_at, now, await mt5_bridge.opened_on(conn)
    ) or mt5_bridge.trading_off(snap)
    age = int((now - row.received_at).total_seconds())
    if problem is not None:
        return Check(
            title,
            NOT_MET,
            [problem],
            "See 'Is my broker connected?' on the home page for what to do.",
        )
    return Check(
        title,
        PASSED,
        [
            f"HFM's report is {age} s old: a DEMO account in USD, the one {ACCOUNT} was opened "
            "on; MT5 connected, Algo Trading on"
        ],
    )


async def _reliable(conn: AsyncConnection, account: Account | None, now: datetime) -> Check:
    title = "Checked against HFM, reliably (the last 7 days)"
    if account is None:
        return Check(title, NOT_MET, ["no HFM demo account is set up here"])
    since = now - WEEK
    finished = and_(
        reconciliation_runs.c.account_id == account.id,
        reconciliation_runs.c.started_at >= since,
        reconciliation_runs.c.status != "RUNNING",
    )
    total, passed = (
        await conn.execute(
            select(
                func.count(),
                func.count().filter(reconciliation_runs.c.status == "PASSED"),
            ).where(finished)
        )
    ).one()
    # A failed check either could not reach HFM, or found our records and HFM's apart.
    apart = int(
        await conn.scalar(
            select(func.count(func.distinct(reconciliation_runs.c.id)))
            .select_from(reconciliation_runs)
            .join(reconciliation_items, reconciliation_items.c.run_id == reconciliation_runs.c.id)
            .where(
                finished,
                reconciliation_runs.c.status != "PASSED",
                reconciliation_items.c.status == "MISMATCH",
                reconciliation_items.c.step != "ACCOUNT_STATE",
            )
        )
        or 0
    )
    last = await conn.scalar(
        select(reconciliation_runs.c.status)
        .where(finished)
        .order_by(desc(reconciliation_runs.c.started_at))
        .limit(1)
    )
    failed = total - passed
    share = _pct(passed, total)
    evidence = [
        f"{total:,} checks: {passed:,} passed ({share} %); {failed:,} failed: "
        f"{failed - apart:,} could not reach HFM, {apart:,} found our records and HFM's apart",
        f"the latest: {last or 'none yet'}",
    ]
    if apart:
        return Check(
            title,
            NOT_MET,
            evidence,
            "A disagreement with HFM must be understood and fixed first (Operations).",
        )
    if total < ENOUGH_CHECKS:
        return Check(title, NOT_MET, evidence, "Too few checks yet to judge: wait a day.")
    if share < RELIABLE or last != "PASSED":
        return Check(
            title,
            NOT_MET,
            evidence,
            f"At least {RELIABLE} % of a week's checks must pass, the latest too: the bridge "
            "and the Windows VM must stay connected.",
        )
    return Check(title, PASSED, evidence)


async def _prices(conn: AsyncConnection, now: datetime) -> Check:
    title = "Gold prices arrive from HFM"
    try:
        inst = await md.instrument_by_key(conn, GOLD)
    except NotFoundError:
        return Check(title, NOT_MET, ["HFM gold is not set up here"])
    count, latest = (
        await conn.execute(
            select(func.count(), func.max(quotes.c.source_ts)).where(
                quotes.c.instrument_id == inst.id, quotes.c.source_ts >= now - timedelta(days=1)
            )
        )
    ).one()
    if latest is None:
        latest = await conn.scalar(
            select(func.max(quotes.c.source_ts)).where(quotes.c.instrument_id == inst.id)
        )
    open_now = session_closed(now) is None
    evidence = [f"{count:,} prices in the last 24 hours"]
    if latest is not None:
        evidence.append(f"the latest {int((now - latest).total_seconds())} s old")
    if latest is None:
        return Check(title, NOT_MET, [*evidence, "no price from HFM yet"])
    if open_now and now - latest > PRICE_FRESH:
        return Check(title, NOT_MET, evidence, "Gold is open, but HFM's price is not current.")
    if not open_now:
        evidence.append("gold is closed or about to close now")
    return Check(title, PASSED, evidence)


async def _filled(conn: AsyncConnection, account: Account | None) -> Check:
    title = "An order filled at HFM, with HFM holding its stop-loss"
    todo = "Press 'Test one demo trade' on the home page and approve it with your code."
    if account is None:
        return Check(title, NOT_MET, ["no HFM demo account is set up here"], todo)
    stop = orders.alias("stop")
    row = (
        await conn.execute(
            select(
                orders.c.updated_at,
                orders.c.filled_qty,
                orders.c.avg_fill_price,
                stop.c.stop_price,
            )
            .select_from(orders)
            .join(
                stop,
                and_(
                    stop.c.intent_id == orders.c.intent_id,
                    stop.c.purpose == "STOP_LOSS",
                    stop.c.acknowledged_at.is_not(None),
                ),
            )
            .where(
                orders.c.account_id == account.id,
                orders.c.purpose == "ENTRY",
                orders.c.filled_qty > 0,
            )
            .order_by(desc(orders.c.updated_at))
            .limit(1)
        )
    ).first()
    if row is None:
        return Check(title, NOT_MET, ["no order has filled on the HFM demo yet"], todo)
    return Check(
        title,
        PASSED,
        [
            f"{row.updated_at:%d %b %Y %H:%M} UTC: {_plain(row.filled_qty)} oz bought at "
            f"{_plain(row.avg_fill_price)}; HFM held its stop-loss at {_plain(row.stop_price)}"
        ],
    )


async def _closed(conn: AsyncConnection, account: Account | None) -> Check:
    title = "A trade closed, and our records equal HFM's balance after it"
    todo = "Close the test trade from the home page and approve it; the next check follows."
    if account is None:
        return Check(title, NOT_MET, ["no HFM demo account is set up here"], todo)
    row = (
        await conn.execute(
            select(
                orders.c.id,
                orders.c.purpose,
                orders.c.avg_fill_price,
                func.max(fills.c.filled_at).label("at"),
                func.coalesce(func.sum(fills.c.realized_pnl), 0).label("result"),
            )
            .join(fills, fills.c.order_id == orders.c.id)
            .where(
                orders.c.account_id == account.id,
                orders.c.purpose.in_(("EXIT", "STOP_LOSS", "TAKE_PROFIT")),
            )
            .group_by(orders.c.id, orders.c.purpose, orders.c.avg_fill_price)
            .order_by(desc("at"))
            .limit(1)
        )
    ).first()
    if row is None:
        return Check(title, NOT_MET, ["no trade has closed on the HFM demo yet"], todo)
    how = {"EXIT": "closed", "STOP_LOSS": "stopped out", "TAKE_PROFIT": "taken profit"}
    evidence = [
        f"{row.at:%d %b %Y %H:%M} UTC: {how[row.purpose]} at {_plain(row.avg_fill_price)}; "
        f"result {Decimal(str(row.result)):,.2f} USD as HFM booked it"
    ]
    checked = await conn.scalar(
        select(func.min(reconciliation_runs.c.started_at)).where(
            reconciliation_runs.c.account_id == account.id,
            reconciliation_runs.c.status == "PASSED",
            reconciliation_runs.c.started_at > row.at,
        )
    )
    if checked is None:
        return Check(title, NOT_MET, evidence, "Wait for the next check with HFM (5 minutes).")
    evidence.append(
        f"checked against HFM at {checked:%H:%M} UTC: PASSED, our ledger equals HFM's balance "
        "to the cent"
    )
    return Check(title, PASSED, evidence)


def _lasted(d: timedelta) -> str:
    minutes = int(d.total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 120 else f"{minutes} min"


async def _recovers(conn: AsyncConnection, now: datetime) -> Check:
    title = "It recovers by itself when the bridge drops (the last 30 days)"
    rows = list(
        await conn.execute(
            select(incidents.c.opened_at, incidents.c.resolved_at, incidents.c.resolved_by).where(
                incidents.c.dedup_key.in_(OUTAGES), incidents.c.opened_at >= now - MONTH
            )
        )
    )
    still = [r for r in rows if r.resolved_at is None]
    itself = [r for r in rows if r.resolved_at is not None and r.resolved_by is None]
    by_hand = len(rows) - len(still) - len(itself)
    if still:
        return Check(
            title,
            NOT_MET,
            [f"the bridge has been offline since {still[0].opened_at:%d %b %H:%M} UTC"],
            "Check the Windows VM, MT5 and the bridge.",
        )
    if not itself:
        return Check(
            title,
            NOT_MET,
            ["no outage closed by itself yet, so recovery is not shown yet"],
            "Nothing to do: it shows after the next outage.",
        )
    week = [r.resolved_at - r.opened_at for r in itself if r.opened_at >= now - WEEK]
    evidence = [
        f"{len(itself)} warnings of HFM unreachable, each closed by itself once HFM answered "
        "again" + (f" ({by_hand} more closed by a person)" if by_hand else ""),
        f"the longest: {_lasted(max(r.resolved_at - r.opened_at for r in itself))} in 30 days"
        + (f", {_lasted(max(week))} in the last 7 days" if week else ""),
        "orders wait while HFM cannot be reached, and a check that fails blocks new trades",
    ]
    return Check(title, PASSED, evidence)


async def _cent_rehearsal(conn: AsyncConnection, now: datetime) -> str | None:
    cent = REHEARSALS["hfm-cent"]
    created = await conn.scalar(select(accounts.c.created_at).where(accounts.c.alias == cent.alias))
    if created is None:
        return None
    closed = await conn.scalar(
        select(func.count())
        .select_from(post_trade_reviews)
        .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
        .join(accounts, accounts.c.id == trade_intents.c.account_id)
        .where(accounts.c.alias == cent.alias)
    )
    return (
        f"meanwhile, the paper rehearsal on HFM Cent terms (SIMULATED, not HFM's account): "
        f"{(now - created).days} of {FORWARD_DAYS} days, {int(closed or 0)} closed trades"
    )


CENT_TODO = (
    "In myHF, open an MT5 Cent demo with $100. Send the server name and a screenshot of gold's "
    "Specification in MT5 (Market Watch, right-click XAUUSD), never a password."
)


async def checklist(conn: AsyncConnection, now: datetime) -> Readiness:
    try:
        account: Account | None = await get_account(conn, alias=ACCOUNT)
    except NotFoundError:
        account = None
    rehearsal = await _cent_rehearsal(conn, now)
    hfm_live = any(a.startswith("hfm") for a in LIVE_ADAPTERS)
    return Readiness(
        [
            await _connected(conn, now),
            await _reliable(conn, account, now),
            await _prices(conn, now),
            await _filled(conn, account),
            await _closed(conn, account),
            await _recovers(conn, now),
            Check(
                "A Cent demo account checked with a real trade cycle",
                BLOCKED,
                [
                    "Cent accounts are not handled here yet: the server refuses an account "
                    "whose money is in US cents (USC)"
                ],
                CENT_TODO,
            ),
            Check(
                f"{FORWARD_DAYS} days on the Cent demo, as if it were the real $100",
                BLOCKED,
                ["needs the Cent demo first"] + ([rehearsal] if rehearsal else []),
                "Starts once the Cent demo is connected and checked.",
            ),
            Check(
                "The real account on its own bridge, identified",
                BLOCKED,
                [
                    "no real-account bridge exists: a real account needs its own MT5 on the VM, "
                    "its own bridge and token, and its identity confirmed by you",
                    "the bridge program trades demo accounts only, and the server refuses any "
                    "account that is not a demo",
                ],
                "Built after the Cent demo has passed.",
            ),
            Check(
                "Trade sizes from the real account's own rules",
                BLOCKED,
                [
                    "read from the real account once its bridge reports: contract size, "
                    "smallest volume and step, tick value; never the demo's"
                ],
            ),
            Check(
                "Your authorization",
                NOT_MET if hfm_live else BLOCKED,
                [
                    "no HFM real-money adapter in this build (ADR-034 lock 1)"
                    if not hfm_live
                    else "no activation yet",
                    "then, with every check above passed: your activation with your code, a "
                    "money cap and an expiry; each real trade approved by you",
                ],
            ),
        ]
    )
