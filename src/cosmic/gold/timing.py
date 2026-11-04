"""The market timing assistant (S63): which of gold's trading sessions is open, whether now suits
a trade, and when the next busy window starts. From the clock, HFM's own price and the hourly
volatility only; never an invented "best time".

The sessions are the usual business hours of the three centres where gold trades most (Tokyo,
London, New York), in their own time zones, so summer time moves them as it moves the markets.
London and New York together are gold's busiest hours, as a matter of habit and not a promise.
No economic calendar is connected, so news is never claimed to have been checked."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from cosmic.gold import analysis

SESSIONS = (  # name, zone, opens, closes (local business hours)
    ("Asia (Tokyo)", ZoneInfo("Asia/Tokyo"), time(9), time(18)),
    ("London", ZoneInfo("Europe/London"), time(8), time(17)),
    ("New York", ZoneInfo("America/New_York"), time(8), time(17)),
)
NEW_YORK = ZoneInfo("America/New_York")
COSTLY_SPREAD = Decimal("0.10")  # a spread over 10 % of the stop distance costs too much of a trade
NEWS = (
    "News: no economic calendar is connected, so news is not checked. Big releases (US jobs, "
    "inflation, the Federal Reserve) can move gold sharply; the stop-loss still applies."
)
GOOD, QUIET, WILD, COSTLY, CLOSED = "GOOD", "QUIET", "WILD", "COSTLY", "CLOSED"


@dataclass(frozen=True)
class Timing:
    open_sessions: tuple[str, ...]
    suitability: str
    why: str
    next_window: tuple[datetime, datetime] | None  # the next London and New York overlap (UTC)
    news: str = NEWS


def _open(name_zone_hours: tuple[str, ZoneInfo, time, time], t: datetime) -> bool:
    _, zone, opens, closes = name_zone_hours
    local = t.astimezone(zone)
    return local.weekday() < 5 and opens <= local.time() < closes


def weekend(t: datetime) -> bool:
    """Gold's weekly break, as HFM keeps it: from Friday 17:00 to Sunday 18:00 New York."""
    ny = t.astimezone(NEW_YORK)
    return (
        ny.weekday() == 5
        or (ny.weekday() == 4 and ny.time() >= time(17))
        or (ny.weekday() == 6 and ny.time() < time(18))
    )


def _overlap(day: datetime) -> tuple[datetime, datetime] | None:
    """London and New York both open on this calendar day (UTC's), in UTC; None at weekends.
    One date for both cities: near midnight UTC their own dates differ."""
    date = day.astimezone(UTC).date()
    if date.weekday() >= 5:
        return None
    spans = [
        (datetime.combine(date, opens, zone), datetime.combine(date, closes, zone))
        for _, zone, opens, closes in (SESSIONS[1], SESSIONS[2])
    ]
    start, end = max(s for s, _ in spans), min(e for _, e in spans)
    return (start.astimezone(UTC), end.astimezone(UTC)) if start < end else None


def next_window(now: datetime) -> tuple[datetime, datetime] | None:
    """The London and New York overlap under way now, or the next one (within a week)."""
    for k in range(8):
        span = _overlap(now + timedelta(days=k))
        if span is not None and span[1] > now:
            return span
    return None


def timing(
    now: datetime,
    reading: analysis.Reading,
    spread: Decimal | None,
    market_open: bool,
) -> Timing:
    """``market_open``: HFM's own evidence (a new price within 5 minutes), not the calendar."""
    sessions = tuple(s[0] for s in SESSIONS if _open(s, now))
    window = next_window(now)
    if not market_open:
        why = (
            "gold's weekly break (Friday 17:00 to Sunday 18:00 New York)"
            if weekend(now)
            else "HFM has sent no new gold price for 5 minutes (a daily break or a pause)"
        )
        return Timing(sessions, CLOSED, f"the market is closed now: {why}", window)
    hourly = reading.frames.get("1h")
    pct = hourly.atr_pct if hourly is not None else None
    if pct is not None and pct < analysis.QUIET:
        return Timing(
            sessions,
            QUIET,
            f"gold barely moves now (hourly range {pct * 100:.3f} % of the price): the rules "
            "wait, since costs would weigh more than the move",
            window,
        )
    if pct is not None and pct > analysis.WILD:
        return Timing(
            sessions,
            WILD,
            f"gold moves wildly now (hourly range {pct * 100:.2f} % of the price): the rules "
            "wait, since a stop could be jumped",
            window,
        )
    stop = reading.stop_distance
    if spread is not None and stop is not None and stop > 0 and spread / stop > COSTLY_SPREAD:
        return Timing(
            sessions,
            COSTLY,
            f"HFM's spread now ({spread:.2f}) is over a tenth of the stop distance "
            f"({stop:.2f}): a trade starts far behind",
            window,
        )
    busy = "London" in sessions and "New York" in sessions
    return Timing(
        sessions,
        GOOD,
        "the market is open and moving normally"
        + ("; London and New York are both open, gold's busiest hours" if busy else ""),
        window,
    )
