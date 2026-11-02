"""How long the owner has to approve a real-money proposal (S51).

A real-money daily proposal is made just after 00:00 UTC, priced by Binance's own book: a limit at
the ask + 0.1 %, which the approval binds with its size and stop. It is bought only while the
price is still between 0.5 % below the proposal and that limit (``auto.BAND``, the band's top
being the limit itself); a price outside it is refused before sending and asked again at the new
price (S44), with the owner's code again. So the time the owner takes decides how often a signal
is bought as proposed. This reads the stored hourly bars of the real-money market: for each day,
the open of hour N against the 00:00 open, and how often it was still in that zone, over all days
and over the days after a busy day (a move of more than 1 %), when signals tend to come.
HISTORICAL: one market's recent hours, not a promise.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema_market import bars, instruments
from cosmic.research.library import auto
from cosmic.trading import mandates

MARKET = "SPOT:PAXG/USDT@BINANCE"  # the real-money market (ADR-034: Binance gold)
HOURS = (1, 3, 6, 12, 18)  # 18: the most a real-money daily proposal waits (LIVE_ASK_TTL)
BARS = 24 * 400  # about the last 13 months of hours
MIN_DAYS = 30
BUSY = 0.01  # a day whose close moved more than 1 % from its open


@dataclass(frozen=True)
class Window:
    days: int  # 00:00 UTC opens with every hour after them stored
    since: date
    inside: dict[int, int]  # hours after the signal → days still bought as proposed
    busy_days: int  # of them, days after a busy day
    busy_inside: dict[int, int]
    below: Decimal  # the zone, in percent of the proposal: down to -below
    above: Decimal  # up to +above (the limit)

    def share(self, hours: int) -> int:
        """All days, as a whole percent rounded down (never better than measured)."""
        return 100 * self.inside[hours] // self.days

    def busy_share(self, hours: int) -> int | None:
        if self.busy_days < MIN_DAYS // 3:
            return None  # too few busy days to say
        return 100 * self.busy_inside[hours] // self.busy_days


def measure(
    times: tuple[datetime, ...],
    opens: tuple[float, ...],
    below: Decimal = auto.BAND,
    above: Decimal = auto.ENTRY_MARKUP,
) -> Window | None:
    """Each 00:00 UTC open against the opens ``HOURS`` later; None below ``MIN_DAYS`` days."""
    at = dict(zip(times, opens, strict=True))
    lo, hi = -float(below), float(above)
    days, busy_days = 0, 0
    inside, busy_inside = dict.fromkeys(HOURS, 0), dict.fromkeys(HOURS, 0)
    first: date | None = None
    for t in sorted(at):
        if t.hour != 0 or t.minute != 0 or at[t] <= 0:
            continue
        later = [at.get(t + timedelta(hours=h)) for h in HOURS]
        if any(p is None for p in later):
            continue
        before = at.get(t - timedelta(hours=24))
        busy = before is not None and before > 0 and abs(at[t] / before - 1) > BUSY
        days += 1
        busy_days += busy
        first = first or t.date()
        for h, p in zip(HOURS, later, strict=True):
            assert p is not None
            if lo <= p / at[t] - 1 <= hi:
                inside[h] += 1
                busy_inside[h] += busy
    if days < MIN_DAYS or first is None:
        return None
    pct = Decimal(100)
    return Window(
        days,
        first,
        inside,
        busy_days,
        busy_inside,
        (below * pct).normalize(),
        (above * pct).normalize(),
    )


_kept: OrderedDict[tuple[object, ...], Window | None] = OrderedDict()


async def build(conn: AsyncConnection) -> Window | None:
    """From the stored hourly bars of the real-money market (kept until a new bar comes);
    None without enough of them."""
    series = mandates.AUTO_SIGNAL_SERIES.get(MARKET, MARKET)
    newest = await conn.scalar(
        select(func.max(bars.c.open_time))
        .join(instruments, instruments.c.id == bars.c.instrument_id)
        .where(
            instruments.c.canonical_key == series,
            bars.c.timeframe == mandates.HOURLY,
            bars.c.is_complete.is_(True),
        )
    )
    if newest is None:
        return None
    key = (series, newest, auto.BAND, auto.ENTRY_MARKUP)
    if key in _kept:
        return _kept[key]
    market = await auto.load_bars(conn, series, mandates.HOURLY, BARS)
    window = None
    if market is not None:
        times = tuple(t.astimezone(UTC) for t in market.dates if isinstance(t, datetime))
        if len(times) == len(market.dates):
            window = measure(times, market.open)
    _kept[key] = window
    while len(_kept) > 4:
        _kept.popitem(last=False)
    return window
