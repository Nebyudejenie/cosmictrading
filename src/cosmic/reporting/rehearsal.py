"""Start with $100 (the $100 Gold Plan, S37): what $100 can do in gold, in plain numbers.

One read model for the page and Telegram /rehearsal:
- **where $100 can trade gold:** each venue's smallest trade, held fully paid, and its loss at
  a daily rule's stop. Binance and HFM are read from their own rules; HFM Cent comes from its
  published terms, not read from an account;
- **what history says:** each daily rule replayed with $100 at Bold on Binance gold, with the
  venue's costs and the risk policy (``autotrader.expectation``): HISTORICAL SIMULATION;
- **what a year could bring:** the replayed trades resampled into many possible years
  (SIMULATED; trades are drawn one by one, so streaks are not kept);
- **how money grows:** the start and a monthly addition at the bad, typical and good rates,
  beside buying gold every month over the same past years;
- **before it starts:** the fee, the risk policy and the market allowed;
- **the rehearsals side by side** (S39), once started: money, trades, the costs and fill prices
  paid, the signals skipped, and each against its own replay over the same days, trade by
  trade: did the system do what its simulation says?

Read-only. Past results are no promise.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.core.logs import get_logger
from cosmic.db.schema import notifications
from cosmic.market import data as md
from cosmic.reporting import autotrader
from cosmic.research.library import auto, expect, insight, ta
from cosmic.research.library.engine import Market
from cosmic.trading import mandates, measured, real_book, venue_terms
from cosmic.trading import rehearsal as r
from cosmic.trading.accounts import Account
from cosmic.trading.context_builder import loss_history
from cosmic.trading.policy import RiskPolicyParams, active_policy

log = get_logger(__name__)
CENT = Decimal("0.01")
ZERO_BPS = Decimal(0)
# How the live side names an exit, in the replay's words: an exit order the auto-trader sent is
# its rule's exit (``autotrader._how`` says "exit"; the replay, "rule exit").
LIVE_HOW = {"exit": "rule exit"}
HFM_GOLD = "CFD:XAU/USD@HFM"
CENT_MIN_OZ = 0.01  # HFM Cent: 0.01 cent lot is 0.01 oz (published terms, read 2026-10-24)
DEFAULT_RULE = "turtle_55_20"  # the smallest worst drop in the research; the choice is the owner's
PATHS = 4_000
SEED = 7
YEARS = (1, 3, 5)


@dataclass(frozen=True)
class VenueRow:
    name: str
    smallest_value: Decimal | None  # what the smallest trade ties up, held fully paid
    smallest_risk: Decimal | None  # its loss at a daily rule's stop, with fees
    fits: bool | None  # within $100 and the $2 a trade
    source: str
    note: str = ""


@dataclass(frozen=True)
class Outcome:
    years: int
    bad: Decimal  # 1 in 10 of the possible paths ended below this
    typical: Decimal  # the middle one
    good: Decimal  # 1 in 10 ended above this
    stop_chance: float  # the share of paths that reached the floor and stopped


@dataclass(frozen=True)
class RuleResult:
    key: str
    name: str
    e: autotrader.Expectation
    outcomes: list[Outcome]


@dataclass(frozen=True)
class GrowthRow:
    years: int
    put_in: Decimal  # the start and every monthly addition
    bad: Decimal
    typical: Decimal
    good: Decimal
    held_gold: Decimal | None  # the same money put into gold over the past years, held


@dataclass(frozen=True)
class Leg:
    """One trade of the rehearsal or of its replay: when it opened, its result, how it closed."""

    opened: datetime
    pnl: Decimal  # its result; for an open trade, so far
    closed: datetime | None = None  # None: still open
    how: str | None = None  # stop-loss | rule exit | take-profit | exit | several

    @property
    def open(self) -> bool:
        return self.closed is None


def session_day(at: datetime, closed_weekends: bool) -> date:
    """The day a close counts for. On a venue closed at weekends, a close during the weekend's
    daily bars (a stop hit at Sunday evening's reopening, say) counts for Monday's, where the
    replay, which skips those bars, makes it."""
    bar = datetime.combine(at.date(), time(), tzinfo=UTC)
    while closed_weekends and expect.weekend_closed_at(bar):
        bar += timedelta(days=1)
    return bar.date()


def _merge(a: Leg, b: Leg) -> Leg:
    """Two trades opened the same day, shown as one (a daily rule opens one a day a market)."""
    still = a.open or b.open
    closed = None if still or a.closed is None or b.closed is None else max(a.closed, b.closed)
    return Leg(
        min(a.opened, b.opened), a.pnl + b.pnl, closed, a.how if a.how == b.how else "several"
    )


@dataclass(frozen=True)
class TradePair:
    """A day a trade opened, in the rehearsal or in the replay of the same days."""

    day: date
    live: Leg | None  # the rehearsal's trade; None when it did not trade that day
    replay: Leg | None  # the replay's
    # The replay cannot have it yet: it fills at the open of the bar after the signal, stored
    # only once that bar has closed. Not a difference.
    waiting: bool = False
    closed_weekends: bool = False  # a venue closed at weekends: closes compared by session

    @property
    def matched(self) -> bool:
        return self.live is not None and self.replay is not None

    @property
    def same_exit(self) -> bool:
        """Both still open, or both closed the same day the same way."""
        if self.live is None or self.replay is None:
            return False
        if self.live.open and self.replay.open:
            return True
        if self.live.closed is not None and self.replay.closed is not None:
            return (
                session_day(self.live.closed, self.closed_weekends)
                == session_day(self.replay.closed, self.closed_weekends)
                and self.live.how == self.replay.how
            )
        return self.waiting

    @property
    def differs(self) -> bool:
        return not self.waiting and not (self.matched and self.same_exit)


@dataclass
class Tracking:
    """A rehearsal against its own replay over the same days: did the system do what its
    simulation says? (S39)"""

    since: datetime  # the mandate in force (or the last) began
    until: datetime  # it ended, or now
    replay: autotrader.Expectation
    pairs: list[TradePair] = field(default_factory=list)
    mandate_id: uuid.UUID | None = None  # the mandate compared

    @property
    def days(self) -> int:
        return max(0, (self.until - self.since).days)

    @property
    def live_trades(self) -> int:
        return sum(1 for p in self.pairs if p.live is not None)

    @property
    def replay_trades(self) -> int:
        return sum(1 for p in self.pairs if p.replay is not None)

    @property
    def live_result(self) -> Decimal:
        return sum((p.live.pnl for p in self.pairs if p.live is not None), Decimal(0))

    @property
    def replay_result(self) -> Decimal:
        return sum((p.replay.pnl for p in self.pairs if p.replay is not None), Decimal(0))

    @property
    def verdict(self) -> str:
        if self.replay.summary is None:
            return self.replay.note or "The replay of these days could not run."
        live, replay = self.live_trades, self.replay_trades
        if live == 0 and replay == 0:
            return (
                "Neither has traded yet, as the replay expects: a daily rule trades about once "
                "every two months."
            )
        missed = [p for p in self.pairs if p.replay is not None and p.live is None]
        extra = [p for p in self.pairs if p.live is not None and p.replay is None and not p.waiting]
        apart = [p for p in self.pairs if p.matched and not p.same_exit]
        waiting = (
            " Its newest trade or exit is replayed once that day's bar has closed."
            if any(p.waiting for p in self.pairs)
            else ""
        )
        if not missed and not extra and not apart:
            if replay == 0:
                return f"It has opened its first trade.{waiting}"
            return (
                f"It opened its trades on the same {replay} day(s) as its replay and closed "
                "them the same way: the system did what its simulation says. The results still "
                "differ a little: the fill prices, the costs, and sizes worked out from the "
                f"account's own money and history.{waiting}"
            )

        def days(pairs: list[TradePair]) -> str:
            return ", ".join(f"{p.day:%d %b}" for p in pairs)

        parts = []
        if missed:
            parts.append(f"the replay traded on {len(missed)} day(s) it did not ({days(missed)})")
        if extra:
            parts.append(f"it traded on {len(extra)} day(s) the replay did not ({days(extra)})")
        if apart:
            parts.append(
                f"{len(apart)} trade(s) closed differently from the replay's ({days(apart)})"
            )
        return (
            "It did not follow its replay: "
            + "; ".join(parts)
            + f". Look at its skipped signals and the auto-trader page for why.{waiting}"
        )


@dataclass
class RehearsalState:
    """One venue's rehearsal: what must be ready, and how it does once started."""

    venue: r.Venue
    fee_set: bool = False
    fee_bps: Decimal = Decimal(0)
    allowed: bool = False  # the risk policy allows its market
    account: Account | None = None
    board: autotrader.Board | None = None
    stopped: bool = False  # it stopped itself for good at its floor
    costs: Decimal | None = None  # the commission and swap its fills paid (simulated)
    fills: measured.FillQuality | None = None  # its fills against the price when sent
    book: real_book.BookSummary | None = None  # its fills against the venue's real order book
    skips: int = 0  # signals it could not act on
    last_skip: str | None = None  # the latest one's reason
    tracking: Tracking | None = None


@dataclass
class StartBoard:
    venues: list[VenueRow]
    rules: list[RuleResult]
    chosen: RuleResult | None
    growth: list[GrowthRow]
    monthly: Decimal
    rehearsals: list[RehearsalState]
    notes: list[str] = field(default_factory=list)  # where the risk policy binds first
    policy_ok: bool = True
    cent: autotrader.Expectation | None = None  # the chosen rule on HFM Cent terms (S38)
    changes: list[Change] = field(default_factory=list)  # to get both ready (S46)
    policy_version: int | None = None  # the policy the changes were worked out from
    not_tradable: list[str] = field(default_factory=list)  # rehearsals left out of them


# ---- getting both rehearsals ready in one step (S46) -------------------------------------------

FEE_MAX_BPS = Decimal(100)
ADVICE_WHY = {
    "max_loss_streak": "lets the auto-trader's own stop for good decide",
    "max_drawdown_pct": "lets its own stop for good decide near the start",
    "max_open_risk_pct": f"allows {mandates.MONEY_OPEN_POSITIONS} Bold trades at a time",
}


@dataclass(frozen=True)
class Change:
    """One change to the risk policy that gets the rehearsals ready."""

    key: str  # its name in the form
    what: str  # in plain words
    needed: bool  # needed before a rehearsal can start; otherwise advised, the owner ticks it


def start_per_trade() -> Decimal:
    """What a rehearsal risks a trade at its start: its money at the owner's care level."""
    return r.START * mandates.CARE_LEVELS[r.CARE][0] / 100


async def tradable_rehearsals(conn: AsyncConnection) -> set[str]:
    """The rehearsals whose market can be traded here: one whose instrument is missing or not
    tradable (a venue retired, a registry not yet synced) cannot be allowed, and is left out
    rather than refusing the other's change."""
    out: set[str] = set()
    for key, v in r.REHEARSALS.items():
        try:
            inst = await md.instrument_by_key(conn, v.market)
        except NotFoundError:
            continue
        if inst.tradable:
            out.add(key)
    return out


def ready_changes(p: RiskPolicyParams, available: set[str]) -> list[Change]:
    """What the risk policy lacks for the rehearsals in ``available``, and what is advised at
    their money. Needed: each venue's fee (unset, the 0.6 % assumption would be charged) and its
    market, venue and currency in the lists. Advised: the limits that would stop a rehearsal
    before its own limits do. Both apply to every account, a real-money one included."""
    out: list[Change] = []
    fees = {f.venue for f in p.venue_fees}
    for key, v in r.REHEARSALS.items():
        if key not in available:
            continue
        if v.venue not in fees:
            if v.venue == "BINANCE":  # the one venue with a real-money link (ADR-034)
                what = (
                    "Binance fee a side, as you enter it below: it applies to every Binance "
                    "account, a real-money one included"
                )
            else:
                what = f"{v.venue} fee a side: {v.fee_bps} bps ({v.fee_basis})"
            out.append(Change(f"fee:{v.venue}", what, True))
        missing = [
            x
            for x, allowed in (
                (v.market, p.allowed_instruments),
                (v.venue, p.allowed_venues),
                (v.currency, p.allowed_quote_currencies),
            )
            if x not in allowed
        ]
        if missing:
            out.append(Change(f"allow:{key}", f"Allow {v.name}: {', '.join(missing)}", True))
    for name, value in autotrader.policy_advice(p, r.START, start_per_trade()).items():
        now = getattr(p, name)
        out.append(
            Change(
                f"advise:{name}",
                f"{name}: {now} → {value} ({ADVICE_WHY[name]}; for every account, a real-money "
                "one included)",
                False,
            )
        )
    return out


def ready_params(
    p: RiskPolicyParams,
    *,
    binance_bps: Decimal | None,
    advised: set[str],
    available: set[str],
) -> tuple[dict[str, Any], list[str]]:
    """The policy with what ``ready_changes`` lists applied (the advised ones only when ticked),
    and what changed in plain words. ``binance_bps``: the owner's Binance fee (10 without BNB),
    required when it is not set yet."""
    params = p.model_dump(mode="python")
    done: list[str] = []
    for c in ready_changes(p, available):
        kind, _, name = c.key.partition(":")
        if kind == "fee":
            v = next(x for x in r.REHEARSALS.values() if x.venue == name)
            bps = v.fee_bps
            if name == "BINANCE":
                if binance_bps is None:
                    raise ValidationFailedError("enter your Binance fee, in bps a side")
                if not binance_bps.is_finite() or not ZERO_BPS <= binance_bps <= FEE_MAX_BPS:
                    raise ValidationFailedError(
                        f"the Binance fee must be between 0 and {FEE_MAX_BPS} bps"
                    )
                bps = binance_bps
            params["venue_fees"] = (*params["venue_fees"], {"venue": name, "bps": bps})
            done.append(f"{name} fee {bps} bps")
        elif kind == "allow":
            v = r.REHEARSALS[name]
            for field_name, x in (
                ("allowed_instruments", v.market),
                ("allowed_venues", v.venue),
                ("allowed_quote_currencies", v.currency),
            ):
                if x not in params[field_name]:
                    params[field_name] = (*params[field_name], x)
            done.append(c.what)
        elif c.key in advised:
            value = autotrader.policy_advice(p, r.START, start_per_trade())[name]
            params[name] = value
            done.append(f"{name} {getattr(p, name)} → {value}")
    return params, done


def resample(
    pnls: list[float], per_year: float, years: int, *, start: float, floor: float, seed: int
) -> Outcome:
    """Possible money after ``years``: trades drawn at random from the replayed ones, as many as
    the replay made in that time, each scaled to the money then (Bold risks a share of it), and
    a path stops for good at ``floor``."""
    n = round(per_year * years)
    if not pnls or n <= 0:
        flat = Decimal(str(start)).quantize(CENT)
        return Outcome(years, flat, flat, flat, 0.0)
    rng = random.Random(seed * 100 + years)  # noqa: S311 - a reproducible simulation  # nosec B311
    ends: list[float] = []
    stops = 0
    for _ in range(PATHS):
        money = start
        for _ in range(n):
            money *= 1 + rng.choice(pnls) / start
            if money <= floor:
                stops += 1
                break
        ends.append(money)
    ends.sort()

    def q(p: float) -> Decimal:
        return Decimal(str(ends[int(p * (len(ends) - 1))])).quantize(CENT)

    return Outcome(years, q(0.1), q(0.5), q(0.9), stops / PATHS)


def grow(start: Decimal, monthly: Decimal, rate: float, years: int) -> Decimal:
    """``start`` and ``monthly`` added at each month's end, growing at ``rate`` a year."""
    m = (1 + rate) ** (1 / 12) - 1 if rate > -1 else -1.0
    value = float(start)
    for _ in range(12 * years):
        value = value * (1 + m) + float(monthly)
    return Decimal(str(max(value, 0.0))).quantize(CENT)


def held_gold(daily: Market | None, start: Decimal, monthly: Decimal, years: int) -> Decimal | None:
    """The start and the monthly addition put into gold over the last ``years`` years and held
    to now (a historical benchmark, after the assumed 0.1 % buying cost)."""
    if daily is None or not isinstance(daily.dates[0], datetime):
        return None
    last = daily.dates[-1]
    assert isinstance(last, datetime)
    since = last - timedelta(days=365 * years)
    if daily.dates[0] > since:
        return None
    first = next(i for i, d in enumerate(daily.dates) if isinstance(d, datetime) and d >= since)
    lump = float(start) * 0.999 * daily.close[-1] / daily.open[first]
    # the monthly buys begin the month after the start: 12 a year, as in "Put in"
    next_month = (since.replace(day=1) + timedelta(days=32)).replace(day=1)
    acc = insight.accumulate(daily, float(monthly), next_month) if monthly > 0 else None
    return Decimal(str(lump + (acc.value if acc is not None else 0.0))).quantize(CENT)


async def _venues(conn: AsyncConnection, now: datetime, per_trade: Decimal) -> list[VenueRow]:
    rows: list[VenueRow] = []
    value, risk, daily, inst = await autotrader.instrument_needs(
        conn, r.MARKET, now, mandates.DAILY, market_stop=False
    )
    if value is not None and risk is not None and inst.min_notional and value < inst.min_notional:
        scale = inst.min_notional / value  # the venue's minimum order value
        value, risk = (value * scale).quantize(CENT), (risk * scale).quantize(CENT)
    rows.append(
        VenueRow(
            "Binance gold (PAXG), paper on Binance prices",
            value,
            risk,
            None if value is None or risk is None else value <= r.START and risk <= per_trade,
            "the venue's own rules",
            "Real gold tokens, no leverage, no overnight fee; orders from 5 USDT.",
        )
    )
    try:
        hv, hr, _, _ = await autotrader.instrument_needs(
            conn, HFM_GOLD, now, mandates.DAILY, market_stop=True
        )
        rows.append(
            VenueRow(
                "HFM gold, standard account",
                hv,
                hr,
                None if hv is None or hr is None else hv <= r.START and hr <= per_trade,
                "the venue's own rules",
                "The smallest trade is 1 ounce, held fully paid.",
            )
        )
    except NotFoundError:
        pass
    cent = r.REHEARSALS["hfm-cent"].market
    try:  # read from HFM's prices with the Cent lots (S38), when the bridge has sent them
        cv_, cr_, _, _ = await autotrader.instrument_needs(
            conn, cent, now, mandates.DAILY, market_stop=False
        )
    except NotFoundError:
        cv_ = cr_ = None
    if cv_ is not None and cr_ is not None:
        rows.append(
            VenueRow(
                "HFM Cent terms (0.01 oz), paper on HFM's prices",
                cv_,
                cr_,
                cv_ <= r.START and cr_ <= per_trade,
                "HFM's own prices, with the Cent account's published lots",
                "No commission, but a nightly swap on gold held; no Cent demo is confirmed yet.",
            )
        )
    elif daily is not None and len(daily) > 21:
        atr = ta.atr(daily.high, daily.low, daily.close, 20)[-1]
        price = daily.close[-1]
        if atr:
            cv = Decimal(str(CENT_MIN_OZ * price)).quantize(CENT)
            cr = Decimal(str(CENT_MIN_OZ * float(auto.STOP_ATR) * atr)).quantize(CENT)
            rows.append(
                VenueRow(
                    "HFM Cent account (0.01 oz)",
                    cv,
                    cr,
                    cv <= r.START and cr <= per_trade,
                    "published terms, not read from an account",
                    "No commission, but a nightly swap on gold held; no Cent demo is "
                    "confirmed yet.",
                )
            )
    return rows


async def _states(conn: AsyncConnection, now: datetime) -> list[RehearsalState]:
    out: list[RehearsalState] = []
    for key, v in r.REHEARSALS.items():
        st = RehearsalState(v)
        st.account = await r.account(conn, key)
        if st.account is not None:
            st.board = await autotrader.build(conn, now, v.alias)
            last = await mandates.recent_mandates(conn, limit=1, account_id=st.account.id)
            st.stopped = bool(last) and last[0].status == "STOPPED"
            await _measure(conn, st, now)
        out.append(st)
    return out


async def _measure(conn: AsyncConnection, st: RehearsalState, now: datetime) -> None:
    """What a started rehearsal paid, how its fills landed and what it skipped."""
    assert st.account is not None
    st.costs = (await measured.costs_paid(conn, st.account.id)).quantize(CENT)
    quality = measured.fill_quality(await measured.fill_slips(conn, account_id=st.account.id))
    st.fills = quality[0] if quality else None
    if st.account.venue_key in real_book.BOOK_SOURCES:  # a venue whose real book can be read
        st.book = await real_book.summary(
            conn, now=now, venue=st.account.venue_key, account_id=st.account.id
        )
    seen: set[str] = set()
    for n in await conn.execute(
        select(notifications.c.id, notifications.c.dedup_key, notifications.c.payload)
        .where(
            notifications.c.kind == "auto_trade_skipped",
            notifications.c.channel == "PORTAL",
            notifications.c.payload["account"].astext == st.venue.alias,
        )
        .order_by(notifications.c.created_at)
    ):
        # one notice per owner: the same skip told to two owners counts once
        key = n.dedup_key.rsplit(":", 2)[0] if n.dedup_key else str(n.id)
        if key in seen:
            continue
        seen.add(key)
        st.skips += 1
        st.last_skip = str((n.payload or {}).get("reason") or "") or None


def replay_from(granted: datetime, timeframe: str, closed_weekends: bool) -> datetime:
    """Where the replay of a mandate's days starts: the open of the last bar completed when it
    began, since the live auto-trader's first decision is on that bar (for a market closed at
    weekends, the last bar in session)."""
    if timeframe == mandates.DAILY:
        step = timedelta(days=1)
        bar = granted.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        step = timedelta(hours=1)
        bar = granted.replace(minute=0, second=0, microsecond=0)
    start = bar - step
    while closed_weekends and timeframe == mandates.DAILY and expect.weekend_closed_at(start):
        start -= step
    return start


def pair_trades(
    live: list[Leg],
    replay: list[expect.Trade],
    replayed_to: datetime | None = None,
    closed_weekends: bool = False,
) -> list[TradePair]:
    """The rehearsal's trades and the replay's, matched by the day they opened: a daily rule
    opens at most one trade a day on a market. The replay acts at the open of the bar after a
    signal, known only once that bar is stored, so a trade opened, or an exit made, after the
    last bar replayed (``replayed_to``, its open) is waiting, not a difference."""
    mine: dict[date, Leg] = {}
    for leg in live:
        day = leg.opened.date()
        mine[day] = _merge(mine[day], leg) if day in mine else leg
    theirs: dict[date, Leg] = {}
    for t in replay:
        leg = Leg(
            t.opened,
            Decimal(str(t.pnl)).quantize(CENT),
            None if t.how == "still open" else t.closed,
            t.how,
        )
        day = t.opened.date()
        theirs[day] = _merge(theirs[day], leg) if day in theirs else leg
    last = None if replayed_to is None else replayed_to.date()
    out: list[TradePair] = []
    for day in sorted(mine.keys() | theirs.keys()):
        a, b = mine.get(day), theirs.get(day)
        waiting = last is not None and (
            (a is not None and b is None and day > last)
            or (
                a is not None
                and b is not None
                and a.closed is not None
                and b.open
                and a.closed.date() > last
            )
        )
        out.append(TradePair(day, a, b, waiting, closed_weekends))
    return out


async def _track(conn: AsyncConnection, now: datetime, st: RehearsalState) -> Tracking | None:
    """The rehearsal's mandate in force (or its last) against its replay over the same days."""
    b = st.board
    m = None if b is None else (b.mandate or b.ended)
    if b is None or m is None or st.account is None:
        return None
    until = min(m.ended_at or m.expires_at, now)
    try:
        inst = await md.instrument_by_key(conn, st.venue.market)
    except NotFoundError:
        return None
    start = replay_from(m.granted_at, m.timeframe, inst.base in venue_terms.METALS)
    # The replay's first fill is at the bar after ``start``. A trade opened from then counts,
    # even one made under the previous mandate before a renewal on the same day: the renewal
    # does not buy that bar's signal again, and the new mandate's replay does. Not when the
    # renewal changed the rule: that trade was another rule's.
    earlier = [
        x
        for x in await mandates.recent_mandates(conn, limit=5, account_id=st.account.id)
        if x.granted_at < m.granted_at
    ]
    same_rule = bool(earlier) and (earlier[0].strategy_key, earlier[0].timeframe) == (
        m.strategy_key,
        m.timeframe,
    )
    step = timedelta(days=1) if m.timeframe == mandates.DAILY else timedelta(hours=1)
    first = start + step if same_rule else m.granted_at
    # R08 as the checks applied it: from the account's high and losing streak at the grant.
    high_water, streak = await loss_history(conn, st.account.id, before=m.granted_at)
    e = await autotrader.expectation(
        conn,
        now,
        st.account,
        m.instruments,
        per_trade=m.max_loss_per_trade,
        daily_stop=m.daily_loss_limit,
        money=m.capital,
        trades_per_day=m.max_trades_per_day,
        open_positions=m.max_open_positions,
        total_stop=m.total_limit,
        strategy_key=m.strategy_key,
        timeframe=m.timeframe,
        equity=m.capital,  # the money it was given then, not today's
        since=start,
        until=until,
        high_water=high_water,
        streak_start=streak,
    )
    replay = [t for t in e.trades if t.opened < until]
    replayed_to = e.summary.end if e.summary is not None else None
    weekends = inst.base in venue_terms.METALS
    pairs = pair_trades(live_legs(b, first, until), replay, replayed_to, weekends)
    return Tracking(m.granted_at, until, e, pairs, m.id)


def live_legs(b: autotrader.Board, first: datetime, until: datetime) -> list[Leg]:
    """The auto-trader's trades opened from ``first`` to ``until``, in the replay's words."""
    return [
        Leg(t.opened_at, t.pnl, t.closed_at, LIVE_HOW.get(t.how, t.how))
        for t in b.closed
        if t.auto and first <= t.opened_at <= until
    ] + [
        Leg(t.opened_at, t.result_now or Decimal(0))
        for t in b.open
        if t.auto and first <= t.opened_at <= until
    ]


async def _replay(
    conn: AsyncConnection,
    now: datetime,
    market: str,
    rule: str,
    per_trade: Decimal,
    stop: Decimal | None,
) -> autotrader.Expectation:
    """The rule as a rehearsal on ``market`` trades it: $100 at Bold, daily, paper fills."""
    return await autotrader.expectation(
        conn,
        now,
        None,
        [market],
        per_trade=per_trade,
        daily_stop=per_trade * mandates.DAILY_STOP_TRADES,
        money=r.START,
        total_stop=stop,
        strategy_key=rule,
        timeframe=mandates.DAILY,
        equity=r.START,
        market_stop=False,
    )


async def build(
    conn: AsyncConnection, now: datetime, *, monthly: Decimal = Decimal(0), rule: str | None = None
) -> StartBoard:
    pct = mandates.CARE_LEVELS[r.CARE][0]
    per_trade = r.START * pct / 100
    total = r.START - r.FLOOR
    states = await _states(conn, now)
    try:
        version, policy = await active_policy(conn)
    except ConflictError:  # no valid policy: every check fails closed
        return StartBoard(
            [],
            [],
            None,
            [],
            monthly,
            states,
            [
                "There is no valid risk policy, so every risk check fails closed: set one "
                "under Risk policy first."
            ],
            policy_ok=False,
        )
    for st in states:
        v = st.venue
        st.fee_set = any(f.venue == v.venue for f in policy.venue_fees)
        st.fee_bps = policy.fee_bps_for(v.venue)
        st.allowed = (
            v.market in policy.allowed_instruments
            and v.venue in policy.allowed_venues
            and v.currency in policy.allowed_quote_currencies
        )
    results: list[RuleResult] = []
    for key in mandates.RULES_BY_TIMEFRAME[mandates.DAILY]:
        # as the rehearsal would trade, its stop for good included
        e = await _replay(conn, now, r.MARKET, key, per_trade, total)
        outcomes: list[Outcome] = []
        # The possible years draw from the whole history, so from a replay without the stop:
        # the resample applies the floor itself (a stopped replay holds only its last losses).
        s = (
            (await _replay(conn, now, r.MARKET, key, per_trade, None)).summary
            if e.summary and e.summary.stopped_at
            else e.summary
        )
        if s is not None:
            values = [v for _, v in s.curve]
            pnls = [b - a for a, b in zip([0.0, *values], values, strict=False)]
            per_year = s.trades / max(s.days / 365.25, 1 / 365.25)
            outcomes = [
                resample(pnls, per_year, y, start=float(r.START), floor=float(r.FLOOR), seed=SEED)
                for y in YEARS
            ]
        results.append(RuleResult(key, mandates.AUTO_RULES[key], e, outcomes))
    chosen_key = rule if rule in mandates.RULES_BY_TIMEFRAME[mandates.DAILY] else DEFAULT_RULE
    chosen = next((x for x in results if x.key == chosen_key), None)
    growth: list[GrowthRow] = []
    daily = await auto.load_bars(conn, r.MARKET, mandates.DAILY)
    if chosen is not None and chosen.outcomes:
        one = chosen.outcomes[0]
        rates = [float(v / r.START) - 1 for v in (one.bad, one.typical, one.good)]
        for y in YEARS:
            bad, typical, good = (grow(r.START, monthly, rate, y) for rate in rates)
            growth.append(
                GrowthRow(
                    y,
                    r.START + monthly * 12 * y,
                    bad,
                    typical,
                    good,
                    held_gold(daily, r.START, monthly, y),
                )
            )
    available = await tradable_rehearsals(conn)
    board = StartBoard(
        [],
        results,
        chosen,
        growth,
        monthly,
        states,
        autotrader.policy_notes(policy, r.START, r.START, per_trade),
        changes=ready_changes(policy, available),
        policy_version=version,
        not_tradable=[v.name for k, v in r.REHEARSALS.items() if k not in available],
    )
    cent_market = r.REHEARSALS["hfm-cent"].market
    if chosen is not None:
        try:  # the same rule on HFM Cent terms: HFM's prices, Cent lots, its swap (S38)
            board.cent = await _replay(conn, now, cent_market, chosen.key, per_trade, total)
        except NotFoundError:
            board.cent = None
    board.venues = await _venues(conn, now, per_trade)
    await _track_all(conn, now, states)
    return board


async def trackings(conn: AsyncConnection, now: datetime) -> list[RehearsalState]:
    """Each rehearsal against its replay, as the page shows it (the replay watch, S45). One
    rehearsal that cannot be compared (no risk policy, a broken replay) is left without a
    comparison; the others still are."""
    states = await _states(conn, now)
    for st in states:
        try:
            async with conn.begin_nested():
                st.tracking = await _track(conn, now, st)
        except Exception as exc:  # one rehearsal never hides the other
            log.warning("rehearsal_not_compared", venue=st.venue.key, error=type(exc).__name__)
            st.tracking = None
    return states


async def _track_all(conn: AsyncConnection, now: datetime, states: list[RehearsalState]) -> None:
    for st in states:
        try:
            st.tracking = await _track(conn, now, st)
        except NotFoundError:  # the rule or its prices are gone
            st.tracking = None


def telegram_lines(boards: list[tuple[str, autotrader.Board | None]], now: datetime) -> list[str]:
    """The /rehearsal answer: each rehearsal's money now and its auto-trader's lines."""
    lines: list[str] = []
    for name, b in boards:
        if b is None or b.account is None:
            lines.append(f"{name}: not started. Start it in the portal: Start with $100.")
            continue
        lines.append(
            f"{name}: money now {b.equity:,.2f} {b.currency} (started with 100.00)."
            if b.equity is not None
            else f"{name}:"
        )
        lines += autotrader.telegram_lines(b, now)
        lines.append("")
    return lines or ["No $100 rehearsal yet."]


__all__ = [
    "Leg",
    "StartBoard",
    "Tracking",
    "build",
    "pair_trades",
    "replay_from",
    "telegram_lines",
]
