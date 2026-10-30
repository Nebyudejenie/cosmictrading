"""Paper fills against the venue's real order book (S41, ADR-034 addendum).

The paper simulator fills an order on a stored public quote (up to a minute or two old) plus
5 bps. Whether that is realistic is measured, not assumed: right after a paper fill on an
account priced from Binance, the worker reads Binance's public order book (keyless, weight 5)
and works out what the same quantity would have cost there, walking the levels as a real
marketable order would. The result is kept with the fill (``fill_book_checks``).

``bps`` is positive when the paper fill was worse for the account than the real book (it paid
more on a buy, got less on a sell): the simulator is cautious. Negative means the simulator was
kinder than the real venue would have been. Gate G (go-live: "paper period with realistic fill
comparison") counts these checks: the paper fills must not be kinder than the real book.

Nothing here places an order or uses a key.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import case, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.clock import Clock
from cosmic.core.logs import get_logger
from cosmic.core.money import BPS, ZERO
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema_execution import fill_book_checks, fills, orders
from cosmic.db.schema_market import instruments, venues
from cosmic.db.schema_trading import accounts
from cosmic.sources.connectors.binance import SOURCE_KEY, BookDepth
from cosmic.sources.contract import SourceError
from cosmic.sources.registry import INSTRUMENTS

log = get_logger("cosmic.trading.real_book")

# Venues whose public order book can be read, and the source that reads it.
BOOK_SOURCES = {"BINANCE": SOURCE_KEY}
MAX_LAG = timedelta(seconds=60)  # a fill first seen later than this is not compared
MIN_CHECKS = 20  # gate G: as many compared fills as the paper period's closed trades
KINDER_LIMIT_BPS = Decimal(5)  # median: the paper fills may be at most this much kinder
CAUTIOUS_LIMIT_BPS = Decimal(25)  # median: and at most this much harsher (not distorted)


class DepthReader(Protocol):
    async def fetch_depth(self, source_symbol: str, limit: int = ...) -> BookDepth: ...


def book_price(
    levels: Sequence[tuple[Decimal, Decimal]], quantity: Decimal
) -> tuple[Decimal | None, int]:
    """The volume-weighted price of taking ``quantity`` through ``levels`` (best first), and how
    many levels it took. None when the levels read hold less than the quantity."""
    left, cost, used = quantity, ZERO, 0
    for price, qty in levels:
        if left <= ZERO:
            break
        take = min(left, qty)
        cost += take * price
        left -= take
        used += 1
    if left > ZERO or quantity <= ZERO:
        return None, used
    return cost / quantity, used


@dataclass(frozen=True)
class Pending:
    fill_id: uuid.UUID
    account_id: uuid.UUID
    venue: str
    symbol: str
    purpose: str
    side: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class BookCheck:
    venue: str
    purpose: str
    side: str
    paper_price: Decimal
    book_price: Decimal | None
    filled_at: datetime
    read_at: datetime

    @property
    def bps(self) -> Decimal | None:
        """Positive: the paper fill was worse for the account than the real book."""
        if self.book_price is None or self.book_price <= 0:
            return None
        diff = self.paper_price - self.book_price
        if self.side == "SELL":
            diff = -diff
        return (diff / self.book_price * BPS).quantize(Decimal("0.01"))

    @property
    def lag(self) -> timedelta:
        return self.read_at - self.filled_at


def _checked() -> Any:
    return exists().where(fill_book_checks.c.fill_id == fills.c.id)


def _symbols() -> dict[str, str]:
    """Instrument key → the book source's symbol, for every venue with a readable book."""
    out: dict[str, str] = {}
    for d in INSTRUMENTS:
        source = BOOK_SOURCES.get(d.venue_key)
        if source is not None and source in d.listings:
            out[d.canonical_key] = d.listings[source]
    return out


async def pending(conn: AsyncConnection, now: datetime) -> list[Pending]:
    """Paper fills on accounts priced from a venue with a readable book, recent enough to be
    compared, and not compared yet."""
    symbols = _symbols()
    rows = await conn.execute(
        select(
            fills.c.id,
            fills.c.quantity,
            fills.c.price,
            fills.c.filled_at,
            orders.c.account_id,
            orders.c.purpose,
            orders.c.side,
            instruments.c.canonical_key,
            venues.c.key.label("venue"),
        )
        .join(orders, orders.c.id == fills.c.order_id)
        .join(accounts, accounts.c.id == orders.c.account_id)
        .join(venues, venues.c.id == accounts.c.venue_id)
        .join(instruments, instruments.c.id == orders.c.instrument_id)
        .where(
            accounts.c.kind == "PAPER",
            venues.c.key.in_(list(BOOK_SOURCES)),
            fills.c.filled_at >= now - MAX_LAG,
            ~_checked(),
        )
        .order_by(fills.c.filled_at, fills.c.id)  # one order for every worker: no deadlock
    )
    return [
        Pending(
            r.id,
            r.account_id,
            r.venue,
            symbols[r.canonical_key],
            r.purpose,
            r.side,
            r.quantity,
            r.price,
            r.filled_at,
        )
        for r in rows
        if r.canonical_key in symbols
    ]


async def record(
    conn: AsyncConnection, p: Pending, depth: BookDepth, read_at: datetime
) -> BookCheck | None:
    """Keep one fill's comparison with the book read at ``read_at`` (once: a second worker's
    read of the same fill is dropped). None when the book had no level on that side."""
    levels = depth.asks if p.side == "BUY" else depth.bids
    if not levels or not timedelta(0) <= read_at - p.filled_at <= MAX_LAG:
        return None  # nothing on that side, or read too late (or before the fill) to compare
    price, used = book_price(levels, p.quantity)
    result = await conn.execute(
        insert(fill_book_checks)
        .values(
            fill_id=p.fill_id,
            account_id=p.account_id,
            venue=p.venue,
            symbol=p.symbol,
            purpose=p.purpose,
            side=p.side,
            quantity=p.quantity,
            paper_price=p.price,
            best_price=levels[0][0],
            book_price=price,
            levels=used,
            filled_at=p.filled_at,
            read_at=read_at,
            source_key=BOOK_SOURCES[p.venue],
        )
        .on_conflict_do_nothing(index_elements=["fill_id"])
    )
    if result.rowcount != 1:
        return None
    return BookCheck(p.venue, p.purpose, p.side, p.price, price, p.filled_at, read_at)


@dataclass(frozen=True)
class BookRun:
    compared: int
    unreadable: tuple[str, ...]  # symbols whose book could not be read this time


async def check_new_fills(db: Database, clock: Clock, reader: DepthReader) -> BookRun:
    """Compare every new paper fill with the real book (the worker's ``paper.book_check``,
    every 10 s). The book is read outside any transaction, once per symbol. A venue that does
    not answer leaves its fills uncompared; they age out after a minute and count as missed."""
    async with db.connect() as conn:
        todo = await pending(conn, clock.now())
    if not todo:
        return BookRun(0, ())
    books: dict[str, tuple[BookDepth, datetime]] = {}
    unreadable: list[str] = []
    for symbol in sorted({p.symbol for p in todo}):
        try:
            depth = await reader.fetch_depth(symbol)
        except (SourceError, ValueError) as exc:
            log.warning("real_book_unreadable", symbol=symbol, error=scrub_text(str(exc))[:200])
            unreadable.append(symbol)
            continue
        books[symbol] = (depth, clock.now())
    done = 0
    async with db.transaction() as conn:
        for p in todo:
            if p.symbol in books:
                depth, read_at = books[p.symbol]
                done += await record(conn, p, depth, read_at) is not None
    return BookRun(done, tuple(unreadable))


@dataclass(frozen=True)
class BookSummary:
    venue: str
    checks: int  # fills compared with a book deep enough for their size
    thin: int  # fills whose book read held less than their size
    missed: int  # paper fills on the venue never compared (the book could not be read in time)
    median_bps: Decimal | None
    kindest_bps: Decimal | None  # the paper fill most favourable to the account
    harshest_bps: Decimal | None
    median_lag: timedelta | None
    last_read: datetime | None

    @property
    def realistic(self) -> bool:
        """Gate G's fill comparison: enough checks; none on a book too thin for its size; at
        most one fill missed for every five compared; and on median the paper fills neither
        kinder than the real book by more than 5 bps nor harsher by more than 25 bps."""
        return not self.shortfalls

    @property
    def shortfalls(self) -> list[str]:
        """Why the comparison does not (yet) count for gate G, in plain words."""
        out: list[str] = []
        if self.checks < MIN_CHECKS:
            out.append(f"{self.checks} of {MIN_CHECKS} fills compared")
        if self.thin:
            out.append(f"{self.thin} fill(s) on a book too thin for their size")
        if self.missed > self.checks // 5:
            out.append(f"{self.missed} fill(s) not compared in time (at most one in five)")
        if self.median_bps is not None and not (
            -KINDER_LIMIT_BPS <= self.median_bps <= CAUTIOUS_LIMIT_BPS
        ):
            out.append(
                f"the typical fill is {self.median_bps:+} bps from the real book, outside "
                f"-{KINDER_LIMIT_BPS} to +{CAUTIOUS_LIMIT_BPS} bps"
            )
        return out


async def summary(
    conn: AsyncConnection,
    *,
    now: datetime,
    venue: str = "BINANCE",
    since: datetime | None = None,
    account_id: uuid.UUID | None = None,
) -> BookSummary:
    """The comparisons on ``venue`` (one account's with ``account_id``). Counted in the
    database; only the bps of the compared fills are read, to take their median exactly."""
    c = fill_book_checks.c
    where = [c.venue == venue]
    if since is not None:
        where.append(c.filled_at >= since)
    if account_id is not None:
        where.append(c.account_id == account_id)
    diff = case((c.side == "BUY", c.paper_price - c.book_price), else_=c.book_price - c.paper_price)
    rows = list(
        await conn.execute(
            select(diff / c.book_price * BPS, c.read_at - c.filled_at).where(
                *where, c.book_price.isnot(None)
            )
        )
    )
    values = sorted(Decimal(r[0]).quantize(Decimal("0.01")) for r in rows)
    lags = sorted(r[1] for r in rows)
    thin = int(
        await conn.scalar(
            select(func.count()).select_from(fill_book_checks).where(*where, c.book_price.is_(None))
        )
        or 0
    )
    last_read = await conn.scalar(select(func.max(c.read_at)).where(*where))
    first_read = await conn.scalar(select(func.min(c.read_at)).where(c.venue == venue))
    missed = 0
    if first_read is not None:  # counted from the first comparison on this venue
        missed_q = (
            select(func.count())
            .select_from(fills)
            .join(orders, orders.c.id == fills.c.order_id)
            .join(accounts, accounts.c.id == orders.c.account_id)
            .join(venues, venues.c.id == accounts.c.venue_id)
            .join(instruments, instruments.c.id == orders.c.instrument_id)
            .where(
                accounts.c.kind == "PAPER",
                venues.c.key == venue,
                instruments.c.canonical_key.in_(list(_symbols())),
                fills.c.recorded_at >= first_read,
                fills.c.filled_at < now - MAX_LAG,  # still inside its minute: not missed yet
                ~_checked(),
            )
        )
        if since is not None:
            missed_q = missed_q.where(fills.c.filled_at >= since)
        if account_id is not None:
            missed_q = missed_q.where(orders.c.account_id == account_id)
        missed = int(await conn.scalar(missed_q) or 0)
    return BookSummary(
        venue=venue,
        checks=len(values),
        thin=thin,
        missed=missed,
        median_bps=Decimal(statistics.median(values)).quantize(Decimal("0.01")) if values else None,
        kindest_bps=values[0] if values else None,
        harshest_bps=values[-1] if values else None,
        median_lag=lags[len(lags) // 2] if lags else None,
        last_read=last_read,
    )
