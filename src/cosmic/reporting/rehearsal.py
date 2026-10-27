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
- **the rehearsal itself**, once started.

Read-only. Past results are no promise.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, NotFoundError
from cosmic.reporting import autotrader
from cosmic.research.library import auto, insight, ta
from cosmic.research.library.engine import Market
from cosmic.trading import mandates
from cosmic.trading import rehearsal as r
from cosmic.trading.accounts import Account
from cosmic.trading.policy import active_policy

CENT = Decimal("0.01")
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
        out.append(st)
    return out


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
        _, policy = await active_policy(conn)
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
    board = StartBoard(
        [],
        results,
        chosen,
        growth,
        monthly,
        states,
        autotrader.policy_notes(policy, r.START, r.START, per_trade),
    )
    cent_market = r.REHEARSALS["hfm-cent"].market
    if chosen is not None:
        try:  # the same rule on HFM Cent terms: HFM's prices, Cent lots, its swap (S38)
            board.cent = await _replay(conn, now, cent_market, chosen.key, per_trade, total)
        except NotFoundError:
            board.cent = None
    board.venues = await _venues(conn, now, per_trade)
    return board


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


__all__ = ["StartBoard", "build", "telegram_lines"]
