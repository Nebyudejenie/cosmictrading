"""The road to real money (S43): what is done, what is left, and an honest pace.

Everything here is read from the same places the go-live page and the locks read: the gates as
evaluated, the locks of ADR-034, the paper period's closed simulated trades and the fills
compared with Binance's real order book (S41). The estimates are arithmetic on the pace of the
last 90 days (or of the series' own life if shorter, a week at least), labelled as estimates. A
pace of zero, or a fill comparison that fails for a reason other than its count, gives no date,
never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.config import Settings
from cosmic.db.schema import incidents
from cosmic.db.schema_execution import fill_book_checks, post_trade_reviews
from cosmic.reporting.edge import UNUSUAL
from cosmic.reporting.replay_watch import PREFIX as OFF_REPLAY
from cosmic.reporting.rule_watch import PREFIX as UNUSUAL_RULE
from cosmic.trading import golive, live, mandates, real_book

PACE_WINDOW = timedelta(days=90)
UNUSUAL_SHARE = Decimal(str(UNUSUAL))  # an alert above it is still watched, no longer unusual
MIN_PACE_WINDOW = timedelta(days=7)  # a younger series is judged over a week at least
FILL_VENUE = "BINANCE"


@dataclass(frozen=True)
class Progress:
    """One count toward a target, with its pace."""

    done: int
    target: int
    recent: int  # within the pace window
    eta: datetime | None  # at that pace, when the target is reached (None: no pace)

    @property
    def met(self) -> bool:
        return self.done >= self.target


def pace_window(first: datetime | None, now: datetime) -> timedelta:
    """The last 90 days, or the series' own life if shorter (a week at least)."""
    if first is None:
        return PACE_WINDOW
    return max(MIN_PACE_WINDOW, min(PACE_WINDOW, now - first))


def estimate(
    done: int, target: int, recent: int, now: datetime, window: timedelta = PACE_WINDOW
) -> datetime | None:
    """When ``target`` is reached at the pace of ``recent`` over ``window``."""
    if done >= target:
        return now
    if recent <= 0:
        return None
    return now + window * (target - done) / recent


def day(at: datetime) -> str:
    """A date rounded up to the whole day, in UTC: never sooner than the arithmetic."""
    start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    if start < at:
        start += timedelta(days=1)
    return f"{start:%d %b %Y} UTC"


@dataclass
class Road:
    now: datetime
    gates_passed: list[str]
    gates_left: list[golive.GateStatus]
    locks_open: int
    locks_total: int
    locks_left: list[live.Lock]
    trades: Progress
    first_trade: datetime | None  # the first closed simulated trade
    days: int  # days since then
    fills: Progress
    fill_problems: list[str] = field(default_factory=list)  # the comparison's other shortfalls
    # (rehearsal, latest day) of a run that parted from its replay, not yet acknowledged
    off_replay: list[tuple[str, str]] = field(default_factory=list)
    # rules doing worse than their history (S48), not yet acknowledged: (name, still watched)
    unusual_rules: list[tuple[str, bool]] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)  # the owner's next steps, in order

    @property
    def g_passed(self) -> bool:
        return "G" in self.gates_passed

    @property
    def paper_ready(self) -> datetime | None:
        """When gate G's evidence could hold at the recent pace: both counts reached, 30 days
        since the first closed trade, nothing else wrong with the fill comparison, and no
        rehearsal parted from its replay unexplained. None without a pace, or while either
        stands."""
        if self.fill_problems or self.off_replay or self.first_trade is None:
            return None
        if self.trades.eta is None or self.fills.eta is None:
            return None
        thirty = self.first_trade + timedelta(days=golive.PAPER_MIN_DAYS)
        return max(self.trades.eta, self.fills.eta, thirty)

    @property
    def evidence_holds(self) -> bool:
        """Gate G's evidence holds now (it still needs recording to pass)."""
        ready = self.paper_ready
        return ready is not None and ready <= self.now

    @property
    def ready_day(self) -> str | None:
        ready = self.paper_ready
        return None if ready is None else day(ready)


async def build(
    conn: AsyncConnection,
    settings: Settings,
    now: datetime,
    *,
    gates: list[golive.GateStatus] | None = None,
    locks: list[live.Lock] | None = None,
) -> Road:
    """``gates`` and ``locks``: the page's own, when it has read them already."""
    if gates is None:
        gates = await golive.readiness(conn, now)
    if locks is None:
        locks = await live.locks(conn, settings, now, live_venues=settings.live_venues)
    closed, first = await golive.paper_period(conn)
    window = pace_window(first, now)
    recent_closed = int(
        await conn.scalar(
            select(func.count())
            .select_from(post_trade_reviews)
            .where(
                post_trade_reviews.c.is_simulated.is_(True),
                post_trade_reviews.c.created_at >= now - window,
            )
        )
        or 0
    )
    book = await real_book.summary(conn, now=now, venue=FILL_VENUE)
    measured = (fill_book_checks.c.venue == FILL_VENUE, fill_book_checks.c.book_price.isnot(None))
    first_fill = await conn.scalar(select(func.min(fill_book_checks.c.filled_at)).where(*measured))
    fill_window = pace_window(first_fill, now)
    recent_fills = int(
        await conn.scalar(
            select(func.count())
            .select_from(fill_book_checks)
            .where(*measured, fill_book_checks.c.filled_at >= now - fill_window)
        )
        or 0
    )
    # A rehearsal run parted from its replay (S45) and the owner has not looked yet.
    off_replay = [
        (key.removeprefix(OFF_REPLAY).rsplit(":", 1)[0], str((detail or {}).get("days", ["?"])[-1]))
        for key, detail in await conn.execute(
            select(incidents.c.dedup_key, incidents.c.detail)
            .where(
                incidents.c.dedup_key.startswith(OFF_REPLAY, autoescape=True),
                incidents.c.status == "OPEN",
            )
            .order_by(incidents.c.dedup_key)
        )
    ]
    unusual = [
        (
            mandates.AUTO_RULES.get(rule, rule),
            Decimal(str((detail or {}).get("as_bad") or 0)) >= UNUSUAL_SHARE,
        )
        for key, detail in await conn.execute(
            select(incidents.c.dedup_key, incidents.c.detail)
            .where(
                incidents.c.dedup_key.startswith(UNUSUAL_RULE, autoescape=True),
                incidents.c.status == "OPEN",
            )
            .order_by(incidents.c.dedup_key)
        )
        if (rule := key.removeprefix(UNUSUAL_RULE))
    ]
    road = Road(
        now=now,
        gates_passed=[g.key for g in gates if g.status == "PASSED"],
        gates_left=[g for g in gates if g.status != "PASSED"],
        locks_open=sum(1 for lock in locks if lock.state == "OPEN"),
        locks_total=sum(1 for lock in locks if lock.state != "EACH TRADE"),
        locks_left=[lock for lock in locks if lock.state == "LOCKED"],
        trades=Progress(
            closed,
            golive.PAPER_MIN_CLOSED_TRADES,
            recent_closed,
            estimate(closed, golive.PAPER_MIN_CLOSED_TRADES, recent_closed, now, window),
        ),
        first_trade=first,
        days=0 if first is None else (now - first).days,
        fills=Progress(
            book.checks,
            real_book.MIN_CHECKS,
            recent_fills,
            estimate(book.checks, real_book.MIN_CHECKS, recent_fills, now, fill_window),
        ),
        # The same reasons gate G gives, less the count (shown as progress above).
        fill_problems=[s for s in book.shortfalls if "fills compared" not in s],
        off_replay=off_replay,
        unusual_rules=unusual,
    )
    road.steps = _steps(road)
    return road


def _steps(road: Road) -> list[str]:
    """The owner's next steps, first things first, in plain words."""
    steps: list[str] = []
    if not road.g_passed:
        if not road.trades.met or not road.fills.met:
            steps.append(
                "Let the rehearsals run: gate G needs "
                f"{road.trades.target} closed trades (now {road.trades.done}) over "
                f"{golive.PAPER_MIN_DAYS} days and {road.fills.target} fills compared with "
                f"Binance's real book (now {road.fills.done})."
            )
        elif road.ready_day is not None and not road.evidence_holds:
            steps.append(
                f"Wait for {golive.PAPER_MIN_DAYS} days since the first closed trade: "
                f"{road.ready_day}."
            )
        steps += [f"Gate G's fill comparison does not hold yet: {p}." for p in road.fill_problems]
        steps += [
            f"Find why {alias} did not follow its replay (latest on {day}; Start with $100 → "
            "Your rehearsals), then acknowledge its alert: gate G's evidence assumes the system "
            "does what its simulation says."
            for alias, day in road.off_replay
        ]
    steps += [
        f"Rule {name} "
        + ("is still doing worse than most" if watched else "is doing worse than nearly all")
        + " of its history since it was chosen (Start with $100 → How far to trust it): weigh it "
        "before real money, then acknowledge its alert."
        for name, watched in road.unusual_rules
    ]
    record = [g.key for g in road.gates_left if g.key in "ABDEH"]
    if road.evidence_holds and not road.g_passed:
        record.append("G")
    if record:
        steps.append(
            "Record the evidence for gate"
            + ("s " if len(record) > 1 else " ")
            + ", ".join(sorted(record))
            + " (Go-live: what passes each)."
        )
    for lock in road.locks_left:
        if lock.number == 4:
            continue  # the gates: the steps above
        if lock.number == 5:
            steps.append(
                "Ask your internet provider whether the server's outgoing address is fixed "
                "(B-11): the real-money key must be locked to it."
            )
        steps.append(f"Lock {lock.number}: {lock.name}. Who opens it: {lock.who}.")
    return steps


def telegram_lines(road: Road) -> list[str]:
    """The road to real money in a few lines (Telegram /golive)."""

    def when(p: Progress) -> str:
        if p.met:
            return "reached"
        if p.eta is None:
            return "no estimate yet (none recently)"
        return f"about {day(p.eta)} at the recent pace (an estimate)"

    if road.g_passed or road.evidence_holds:
        g_line = "Gate G's evidence holds now"
    elif road.ready_day is not None:
        g_line = f"Gate G's evidence could hold about {road.ready_day} (an estimate)"
    else:
        g_line = "Gate G's evidence: no estimate yet"
    return [
        "The road to real money (live trading is DISABLED)",
        f"Gates passed: {', '.join(road.gates_passed) or 'none'}; still to pass: "
        + (", ".join(g.key for g in road.gates_left) or "none"),
        f"Locks open: {road.locks_open} of {road.locks_total}",
        f"Closed simulated trades: {road.trades.done}/{road.trades.target} "
        f"({road.days} days so far): {when(road.trades)}",
        f"Fills compared with Binance's real book: {road.fills.done}/{road.fills.target}: "
        f"{when(road.fills)}",
        g_line,
        "Next:",
        *[f"- {s}" for s in road.steps],
    ]
