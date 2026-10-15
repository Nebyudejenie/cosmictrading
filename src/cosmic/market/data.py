"""Read models over market data with explicit freshness labels."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import NotFoundError
from cosmic.core.labels import DataLabel
from cosmic.core.money import BPS
from cosmic.db.schema_market import assets, bars, instruments, quotes, sources, venue_status, venues
from cosmic.sources.freshness import age_seconds, classify
from cosmic.sources.registry import REFERENCE_PAIRS


@dataclass(frozen=True)
class QuoteView:
    instrument_id: uuid.UUID
    canonical_key: str
    source_key: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    source_ts: datetime | None
    ingested_at: datetime | None
    label: DataLabel
    age_seconds: float | None

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal | None:
        m = self.mid
        if m is None or m == 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / m * BPS


@dataclass(frozen=True)
class Bar:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_complete: bool


@dataclass(frozen=True)
class InstrumentView:
    id: uuid.UUID
    canonical_key: str
    pair_key: str
    venue_key: str
    base: str
    quote: str
    tick_size: Decimal | None
    lot_size: Decimal | None
    min_qty: Decimal | None
    max_qty: Decimal | None
    min_notional: Decimal | None
    status: str
    tradable: bool
    metadata_fetched_at: datetime | None
    asset_class: str = "CRYPTO_SPOT"
    metadata: dict[str, Any] | None = None  # the venue's raw rules, as last ingested


@dataclass(frozen=True)
class Deviation:
    pair_key: str
    bps: Decimal | None
    label: DataLabel  # DELAYED when both sides are fresh; otherwise STALE/UNKNOWN
    quotes: tuple[QuoteView, ...]


async def instrument_by_key(conn: AsyncConnection, canonical_key: str) -> InstrumentView:
    base_a = assets.alias("base_a")
    quote_a = assets.alias("quote_a")
    row = (
        await conn.execute(
            select(
                instruments,
                venues.c.key.label("venue_key"),
                base_a.c.symbol.label("base"),
                quote_a.c.symbol.label("quote"),
            )
            .join(venues, venues.c.id == instruments.c.venue_id)
            .join(base_a, base_a.c.id == instruments.c.base_asset_id)
            .join(quote_a, quote_a.c.id == instruments.c.quote_asset_id)
            .where(instruments.c.canonical_key == canonical_key)
        )
    ).first()
    if row is None:
        raise NotFoundError(f"unknown instrument {canonical_key}")
    return _instrument(row)


async def instrument_by_id(conn: AsyncConnection, instrument_id: uuid.UUID) -> InstrumentView:
    key = (
        await conn.execute(
            select(instruments.c.canonical_key).where(instruments.c.id == instrument_id)
        )
    ).scalar_one_or_none()
    if key is None:
        raise NotFoundError("unknown instrument")
    return await instrument_by_key(conn, key)


async def list_instruments(conn: AsyncConnection) -> list[InstrumentView]:
    keys = [
        r.canonical_key
        for r in await conn.execute(
            select(instruments.c.canonical_key).order_by(
                instruments.c.pair_key, instruments.c.canonical_key
            )
        )
    ]
    return [await instrument_by_key(conn, k) for k in keys]


def _instrument(row: Any) -> InstrumentView:
    return InstrumentView(
        id=row.id,
        canonical_key=row.canonical_key,
        pair_key=row.pair_key,
        venue_key=row.venue_key,
        base=row.base,
        quote=row.quote,
        tick_size=row.tick_size,
        lot_size=row.lot_size,
        min_qty=row.min_qty,
        max_qty=row.max_qty,
        min_notional=row.min_notional,
        status=row.status,
        tradable=row.tradable,
        metadata_fetched_at=row.metadata_fetched_at,
        asset_class=row.asset_class,
        metadata=row.metadata,
    )


async def latest_quote(conn: AsyncConnection, instrument_id: uuid.UUID, now: datetime) -> QuoteView:
    """Most recent quote for an instrument from its own venue's source, with a label."""
    row = (
        await conn.execute(
            select(
                quotes,
                sources.c.key.label("source_key"),
                sources.c.freshness_limit_seconds,
                sources.c.latency_class,
                sources.c.enabled,
                instruments.c.canonical_key,
            )
            .join(sources, sources.c.id == quotes.c.source_id)
            .join(instruments, instruments.c.id == quotes.c.instrument_id)
            .where(quotes.c.instrument_id == instrument_id)
            .order_by(desc(quotes.c.source_ts))
            .limit(1)
        )
    ).first()
    if row is None:
        key = (
            await conn.execute(
                select(instruments.c.canonical_key).where(instruments.c.id == instrument_id)
            )
        ).scalar_one_or_none() or "?"
        return QuoteView(
            instrument_id, key, "-", None, None, None, None, None, DataLabel.UNAVAILABLE, None
        )
    label = classify(
        row.source_ts,
        now,
        row.freshness_limit_seconds,
        latency_class=row.latency_class,
        enabled=row.enabled,
    )
    return QuoteView(
        instrument_id=instrument_id,
        canonical_key=row.canonical_key,
        source_key=row.source_key,
        bid=row.bid,
        ask=row.ask,
        last=row.last,
        source_ts=row.source_ts,
        ingested_at=row.ingested_at,
        label=label,
        age_seconds=age_seconds(row.source_ts, now),
    )


async def _pair_views(conn: AsyncConnection, pair_key: str, now: datetime) -> list[QuoteView]:
    ids = [
        r.id
        for r in await conn.execute(
            select(instruments.c.id)
            .where(instruments.c.pair_key == pair_key)
            .order_by(instruments.c.canonical_key)
        )
    ]
    return [await latest_quote(conn, i, now) for i in ids]


async def cross_source_deviation(conn: AsyncConnection, pair_key: str, now: datetime) -> Deviation:
    """Mid-price deviation across venues quoting the same pair (DATA cross-check). A pair with
    fewer than two fresh venues is compared with its reference pairs (REFERENCE_PAIRS)."""
    views = tuple(await _pair_views(conn, pair_key, now))

    def fresh_of(vs: tuple[QuoteView, ...]) -> list[QuoteView]:
        return [v for v in vs if v.label is DataLabel.DELAYED and v.mid is not None]

    if len(fresh_of(views)) < 2:
        for ref in REFERENCE_PAIRS.get(pair_key, ()):
            views += tuple(await _pair_views(conn, ref, now))
    fresh = fresh_of(views)
    if len(fresh) < 2:
        label = DataLabel.UNKNOWN if len(views) < 2 else DataLabel.STALE
        return Deviation(pair_key, None, label, views)
    mids = [v.mid for v in fresh if v.mid is not None]
    lo, hi = min(mids), max(mids)
    return Deviation(pair_key, (hi - lo) / lo * BPS, DataLabel.DELAYED, views)


async def complete_bars(
    conn: AsyncConnection,
    instrument_id: uuid.UUID,
    timeframe: str,
    *,
    limit: int = 500,
    until: datetime | None = None,
) -> list[Bar]:
    """Completed bars only (oldest first). The still-open bar is never returned."""
    q = select(bars).where(
        bars.c.instrument_id == instrument_id,
        bars.c.timeframe == timeframe,
        bars.c.is_complete.is_(True),
    )
    if until is not None:
        q = q.where(bars.c.open_time < until)
    rows = await conn.execute(q.order_by(desc(bars.c.open_time)).limit(limit))
    out = [Bar(r.open_time, r.open, r.high, r.low, r.close, r.volume, r.is_complete) for r in rows]
    out.reverse()
    return out


async def latest_venue_status(
    conn: AsyncConnection, venue_key: str, now: datetime
) -> tuple[str, DataLabel]:
    row = (
        await conn.execute(
            select(
                venue_status.c.status, venue_status.c.ingested_at, sources.c.freshness_limit_seconds
            )
            .join(venues, venues.c.id == venue_status.c.venue_id)
            .join(sources, sources.c.id == venue_status.c.source_id)
            .where(venues.c.key == venue_key)
            .order_by(desc(venue_status.c.ingested_at))
            .limit(1)
        )
    ).first()
    if row is None:
        has_source = (
            await conn.execute(select(venues.c.status_source_key).where(venues.c.key == venue_key))
        ).scalar_one_or_none()
        return "UNKNOWN", DataLabel.UNAVAILABLE if has_source else DataLabel.NOT_CONFIGURED
    return row.status, classify(row.ingested_at, now, row.freshness_limit_seconds)


MID_MAX_AGE = timedelta(minutes=5)


async def mid_at(
    conn: AsyncConnection,
    instrument_id: uuid.UUID,
    at: datetime,
    *,
    max_age: timedelta = MID_MAX_AGE,
) -> Decimal | None:
    """Mid price from the latest quote at or before ``at``; None if none is recent enough.

    Used to attribute results after the fact (post-trade review), never for decisions.
    """
    row = (
        await conn.execute(
            select(quotes.c.bid, quotes.c.ask)
            .where(
                quotes.c.instrument_id == instrument_id,
                quotes.c.source_ts <= at,
                quotes.c.source_ts >= at - max_age,
            )
            .order_by(desc(quotes.c.source_ts))
            .limit(1)
        )
    ).first()
    if row is None or row.bid is None or row.ask is None:
        return None
    return (Decimal(row.bid) + Decimal(row.ask)) / 2
