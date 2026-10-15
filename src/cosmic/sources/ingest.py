"""Ingestion: fetch → validate → normalise → idempotent upsert, with provenance.

Every write references a ``fetch_runs`` row (source vintage). Rejected records are
counted and explained in ``data_quality_results``; they are never silently dropped.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.clock import Clock
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.logs import get_logger
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema_market import (
    assets,
    bars,
    data_quality_results,
    fetch_runs,
    instrument_listings,
    instruments,
    market_events,
    quotes,
    sources,
    venue_status,
    venues,
)
from cosmic.sources.contract import (
    TIMEFRAME_SECONDS,
    BarRecord,
    InstrumentRules,
    MarketDataConnector,
    QuoteRecord,
    SourceError,
    VenueStatusConnector,
)
from cosmic.sources.registry import ASSETS, INSTRUMENTS, SOURCES, VENUES, source_retired

log = get_logger(__name__)
FUTURE_TOLERANCE = timedelta(minutes=2)
# How old a bar may be and still be accepted. A sanity bound against nonsense timestamps, not a
# retention policy: daily bars carry the strategy library's research history (BTC since 2011).
MAX_BAR_AGE = {
    "1m": timedelta(days=400),
    "5m": timedelta(days=400),
    "15m": timedelta(days=400),
    "1h": timedelta(days=400),
    "6h": timedelta(days=4000),
    "1d": timedelta(days=25 * 365),
}


# ---- registry seeding -------------------------------------------------------------------


async def ensure_registry(
    conn: AsyncConnection, now: datetime, *, retired_venues: frozenset[str] = frozenset()
) -> int:
    """Upsert code-defined sources, venues, assets, instruments and listings. Idempotent.

    Sources of retired venues are disabled and their instruments are not tradable; nothing
    stored is deleted (ADR-027). The registry decides tradability for existing rows too.
    Returns the number of newly inserted rows (audited when > 0).
    """
    inserted = 0
    for s in SOURCES:
        values = {
            "name": s.name,
            "publisher": s.publisher,
            "jurisdiction": s.jurisdiction,
            "language": s.language,
            "access_method": s.access_method,
            "base_url": s.base_url,
            "terms_url": s.terms_url,
            "terms_status": s.terms_status,
            "licence_note": s.licence_note,
            "data_class": s.data_class,
            "latency_class": s.latency_class,
            "update_frequency_seconds": s.update_frequency_seconds,
            "freshness_limit_seconds": s.freshness_limit_seconds,
            "timezone": s.timezone,
            "currency_unit": s.currency_unit,
            "known_bias": s.known_bias,
            "missingness_note": s.missingness_note,
            "quality_checks": list(s.quality_checks),
            "owner": s.owner,
            "fallback_source_key": s.fallback_source_key,
            "enabled": s.enabled and not source_retired(s.key, retired_venues),
            "updated_at": now,
        }
        # xmax = 0 on the returned row means the upsert inserted rather than updated.
        was_inserted: bool = (
            await conn.execute(
                pg_insert(sources)
                .values(id=uuid7(), key=s.key, created_at=now, **values)
                .on_conflict_do_update(index_elements=[sources.c.key], set_=values)
                .returning(literal_column("(xmax = 0)").label("inserted"))
            )
        ).scalar_one()
        if was_inserted:
            inserted += 1
    for v in VENUES:
        r = await conn.execute(
            pg_insert(venues)
            .values(
                id=uuid7(),
                key=v.key,
                name=v.name,
                kind=v.kind,
                timezone=v.timezone,
                session=v.session,
                status_source_key=v.status_source_key,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[venues.c.key])
        )
        inserted += r.rowcount
    for a in ASSETS:
        r = await conn.execute(
            pg_insert(assets)
            .values(
                id=uuid7(),
                symbol=a.symbol,
                name=a.name,
                kind=a.kind,
                display_decimals=a.display_decimals,
            )
            .on_conflict_do_nothing(index_elements=[assets.c.symbol])
        )
        inserted += r.rowcount
    venue_ids = {r.key: r.id for r in await conn.execute(select(venues.c.key, venues.c.id))}
    asset_ids = {r.symbol: r.id for r in await conn.execute(select(assets.c.symbol, assets.c.id))}
    source_ids = {r.key: r.id for r in await conn.execute(select(sources.c.key, sources.c.id))}
    for i in INSTRUMENTS:
        r = await conn.execute(
            pg_insert(instruments)
            .values(
                id=uuid7(),
                canonical_key=i.canonical_key,
                pair_key=i.pair_key,
                venue_id=venue_ids[i.venue_key],
                base_asset_id=asset_ids[i.base],
                quote_asset_id=asset_ids[i.quote],
                asset_class=i.asset_class,
                status="UNKNOWN",
                tradable=i.tradable and i.venue_key not in retired_venues,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[instruments.c.canonical_key])
        )
        inserted += r.rowcount
        tradable = i.tradable and i.venue_key not in retired_venues
        await conn.execute(
            update(instruments)
            .where(instruments.c.canonical_key == i.canonical_key)
            .where(instruments.c.tradable.is_not(tradable))
            .values(tradable=tradable, updated_at=now)
        )
        inst_id = (
            await conn.execute(
                select(instruments.c.id).where(instruments.c.canonical_key == i.canonical_key)
            )
        ).scalar_one()
        for source_key, symbol in i.listings.items():
            r = await conn.execute(
                pg_insert(instrument_listings)
                .values(
                    id=uuid7(),
                    instrument_id=inst_id,
                    source_id=source_ids[source_key],
                    source_symbol=symbol,
                )
                .on_conflict_do_nothing()
            )
            inserted += r.rowcount
    if inserted:
        await record(
            conn,
            action="registry.synced",
            actor=Actor.system("ingest"),
            occurred_at=now,
            entity_type="registry",
            entity_id="market",
            payload={"inserted": inserted},
        )
    return inserted


# ---- validation (pure) ------------------------------------------------------------------


def validate_bar(bar: BarRecord, now: datetime) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        problems.append(("positive_prices", "non-positive price"))
    if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
        problems.append(("ohlc_consistency", "open/close outside low-high range"))
    if bar.volume < 0:
        problems.append(("non_negative_volume", "negative volume"))
    if bar.open_time > now + FUTURE_TOLERANCE:
        problems.append(("timestamp_not_future", "bar starts in the future"))
    if bar.open_time < now - MAX_BAR_AGE[bar.timeframe]:
        problems.append(("timestamp_not_future", f"{bar.timeframe} bar older than accepted"))
    step = TIMEFRAME_SECONDS[bar.timeframe]
    if int(bar.open_time.timestamp()) % step != 0:
        problems.append(("bar_alignment", "open time not aligned to timeframe"))
    return problems


def validate_quote(q: QuoteRecord, now: datetime) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    for name, value in (("bid", q.bid), ("ask", q.ask), ("last", q.last)):
        if value is not None and value <= 0:
            problems.append(("positive_prices", f"non-positive {name}"))
    if q.bid is not None and q.ask is not None and q.bid > q.ask:
        problems.append(("bid_not_above_ask", "bid above ask (crossed quote)"))
    if q.source_ts > now + FUTURE_TOLERANCE:
        problems.append(("timestamp_not_future", "quote timestamp in the future"))
    return problems


def is_complete(open_time: datetime, timeframe: str, now: datetime) -> bool:
    return open_time + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]) <= now


# ---- ingestion service -------------------------------------------------------------------


@dataclass
class RunStats:
    received: int = 0
    new: int = 0
    updated: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


class IngestService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        market: dict[str, MarketDataConnector],
        status: dict[str, VenueStatusConnector] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.market = market
        self.status = status or {}

    async def _source(self, conn: AsyncConnection, key: str) -> Any:
        row = (await conn.execute(select(sources).where(sources.c.key == key))).first()
        if row is None:
            raise LookupError(f"source {key} not registered")
        return row

    async def _listings(self, conn: AsyncConnection, source_id: uuid.UUID) -> list[Any]:
        rows = await conn.execute(
            select(
                instrument_listings.c.instrument_id,
                instrument_listings.c.source_symbol,
                instruments.c.canonical_key,
            )
            .join(instruments, instruments.c.id == instrument_listings.c.instrument_id)
            .where(instrument_listings.c.source_id == source_id)
            .order_by(instruments.c.canonical_key)
        )
        return list(rows)

    async def _start_run(self, source_id: uuid.UUID, kind: str, subject: str | None) -> uuid.UUID:
        run_id = uuid7()
        async with self.db.transaction() as conn:
            await conn.execute(
                fetch_runs.insert().values(
                    id=run_id,
                    source_id=source_id,
                    kind=kind,
                    subject=subject,
                    started_at=self.clock.now(),
                    status="RUNNING",
                )
            )
        return run_id

    async def _finish_run(
        self,
        conn: AsyncConnection,
        *,
        run_id: uuid.UUID,
        source_id: uuid.UUID,
        stats: RunStats,
        http_status: int | None,
    ) -> None:
        now = self.clock.now()
        if stats.errors and stats.received == 0:
            status = "FAILED"
        elif stats.errors or stats.rejected:
            status = "PARTIAL"
        else:
            status = "SUCCESS"
        error = scrub_text("; ".join(stats.errors))[:1000] if stats.errors else None
        await conn.execute(
            update(fetch_runs)
            .where(fetch_runs.c.id == run_id)
            .values(
                finished_at=now,
                status=status,
                http_status=http_status,
                records_received=stats.received,
                records_new=stats.new,
                records_updated=stats.updated,
                records_rejected=stats.rejected,
                error=error,
            )
        )
        if status == "FAILED":
            await conn.execute(
                update(sources)
                .where(sources.c.id == source_id)
                .values(last_error_at=now, last_error=error)
            )
        else:
            await conn.execute(
                update(sources)
                .where(sources.c.id == source_id)
                .values(
                    last_success_at=now, **({"last_error": None} if status == "SUCCESS" else {})
                )
            )

    async def _quality(
        self,
        conn: AsyncConnection,
        run_id: uuid.UUID,
        check: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        await conn.execute(
            data_quality_results.insert().values(
                id=uuid7(),
                fetch_run_id=run_id,
                check_name=check,
                status=status,
                detail=to_jsonable(detail),
                created_at=self.clock.now(),
            )
        )

    # -- quotes ---------------------------------------------------------------------------

    async def ingest_quotes(self, source_key: str) -> RunStats:
        connector = self.market[source_key]
        async with self.db.connect() as conn:
            src = await self._source(conn, source_key)
            listings = await self._listings(conn, src.id)
        run_id = await self._start_run(src.id, "quotes", None)
        stats = RunStats()
        fetched: list[tuple[Any, QuoteRecord]] = []
        http_status: int | None = None
        for listing in listings:
            try:
                result = await connector.fetch_quote(listing.source_symbol)
                http_status = result.http_status
                fetched.extend((listing, q) for q in result.records)
            except SourceError as exc:
                stats.errors.append(f"{listing.source_symbol}: {exc}")
        now = self.clock.now()
        async with self.db.transaction() as conn:
            for listing, q in fetched:
                stats.received += 1
                problems = validate_quote(q, now)
                if problems:
                    stats.rejected += 1
                    for check, reason in problems:
                        await self._quality(
                            conn,
                            run_id,
                            check,
                            "FAIL",
                            {"symbol": q.source_symbol, "reason": reason},
                        )
                    continue
                inserted = await conn.execute(
                    pg_insert(quotes)
                    .values(
                        id=uuid7(),
                        instrument_id=listing.instrument_id,
                        source_id=src.id,
                        bid=q.bid,
                        ask=q.ask,
                        last=q.last,
                        source_ts=q.source_ts,
                        ingested_at=now,
                        fetch_run_id=run_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            quotes.c.instrument_id,
                            quotes.c.source_id,
                            quotes.c.source_ts,
                        ]
                    )
                )
                stats.new += inserted.rowcount
            await self._finish_run(
                conn, run_id=run_id, source_id=src.id, stats=stats, http_status=http_status
            )
        return stats

    # -- bars -----------------------------------------------------------------------------

    async def ingest_bars(self, source_key: str, timeframe: str) -> RunStats:
        connector = self.market[source_key]
        async with self.db.connect() as conn:
            src = await self._source(conn, source_key)
            listings = await self._listings(conn, src.id)
        run_id = await self._start_run(src.id, f"bars_{timeframe}", None)
        stats = RunStats()
        fetched: list[tuple[Any, list[BarRecord], list[tuple[str, str]]]] = []
        http_status: int | None = None
        for listing in listings:
            try:
                result = await connector.fetch_bars(listing.source_symbol, timeframe)
                http_status = result.http_status
                fetched.append((listing, result.records, result.rejected))
            except SourceError as exc:
                stats.errors.append(f"{listing.source_symbol}: {exc}")
        now = self.clock.now()
        async with self.db.transaction() as conn:
            for listing, records, parse_rejects in fetched:
                for subject, reason in parse_rejects:
                    stats.rejected += 1
                    await self._quality(
                        conn,
                        run_id,
                        "schema",
                        "FAIL",
                        {"symbol": listing.source_symbol, "row": subject, "reason": reason},
                    )
                valid: list[BarRecord] = []
                for bar in records:
                    stats.received += 1
                    problems = validate_bar(bar, now)
                    if not problems:
                        valid.append(bar)
                        continue
                    stats.rejected += 1
                    for check, reason in problems:
                        await self._quality(
                            conn,
                            run_id,
                            check,
                            "FAIL",
                            {
                                "symbol": bar.source_symbol,
                                "open_time": bar.open_time,
                                "reason": reason,
                            },
                        )
                await self._upsert_bars(
                    conn, listing.instrument_id, src.id, run_id, timeframe, valid, now, stats
                )
            await self._finish_run(
                conn, run_id=run_id, source_id=src.id, stats=stats, http_status=http_status
            )
        return stats

    async def _upsert_bars(
        self,
        conn: AsyncConnection,
        instrument_id: uuid.UUID,
        source_id: uuid.UUID,
        run_id: uuid.UUID,
        timeframe: str,
        records: list[BarRecord],
        now: datetime,
        stats: RunStats,
    ) -> None:
        """Batch upsert: one SELECT of existing rows, bulk INSERT of new ones, targeted UPDATEs."""
        if not records:
            return
        times = [b.open_time for b in records]
        scope = and_(
            bars.c.instrument_id == instrument_id,
            bars.c.source_id == source_id,
            bars.c.timeframe == timeframe,
        )
        existing = {
            r.open_time: r
            for r in await conn.execute(
                select(bars).where(
                    scope, bars.c.open_time >= min(times), bars.c.open_time <= max(times)
                )
            )
        }
        inserts: list[dict[str, Any]] = []
        for bar in records:
            complete = is_complete(bar.open_time, timeframe, now)
            values = {
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "is_complete": complete,
                "fetch_run_id": run_id,
                "updated_at": now,
            }
            old = existing.get(bar.open_time)
            if old is None:
                inserts.append(
                    {
                        "instrument_id": instrument_id,
                        "source_id": source_id,
                        "timeframe": timeframe,
                        "open_time": bar.open_time,
                        "ingested_at": now,
                        **values,
                    }
                )
                continue
            same = (old.open, old.high, old.low, old.close, old.volume) == (
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            )
            if same and old.is_complete == complete:
                continue
            if old.is_complete and not same:
                # A completed bar was revised by the source: keep the vintage trail.
                await self._quality(
                    conn,
                    run_id,
                    "bar_revised",
                    "WARN",
                    {
                        "open_time": bar.open_time,
                        "old_close": old.close,
                        "new_close": bar.close,
                        "old_volume": old.volume,
                        "new_volume": bar.volume,
                        "previous_fetch_run": old.fetch_run_id,
                    },
                )
            await conn.execute(
                bars.update().where(scope, bars.c.open_time == bar.open_time).values(**values)
            )
            stats.updated += 1
        if inserts:
            await conn.execute(bars.insert(), inserts)
            stats.new += len(inserts)

    async def backfill_bars(
        self, source_key: str, timeframe: str, *, days: int, page_bars: int = 300
    ) -> RunStats:
        """Fetch history in pages (politely, via the source's token bucket); idempotent upserts."""
        connector = self.market[source_key]
        step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
        async with self.db.connect() as conn:
            src = await self._source(conn, source_key)
            listings = await self._listings(conn, src.id)
        run_id = await self._start_run(src.id, f"backfill_{timeframe}", f"{days}d")
        stats = RunStats()
        end_all = self.clock.now()
        start_all = end_all - timedelta(days=days)
        http_status: int | None = None
        for listing in listings:
            cursor = start_all
            while cursor < end_all:
                window_end = min(cursor + step * page_bars, end_all)
                try:
                    result = await connector.fetch_bars_range(
                        listing.source_symbol, timeframe, cursor, window_end
                    )
                    http_status = result.http_status
                except SourceError as exc:
                    stats.errors.append(f"{listing.source_symbol} {cursor:%Y-%m-%d}: {exc}")
                    break
                now = self.clock.now()
                async with self.db.transaction() as conn:
                    valid = []
                    for bar in result.records:
                        stats.received += 1
                        if validate_bar(bar, now):
                            stats.rejected += 1
                        else:
                            valid.append(bar)
                    await self._upsert_bars(
                        conn, listing.instrument_id, src.id, run_id, timeframe, valid, now, stats
                    )
                cursor = window_end
        async with self.db.transaction() as conn:
            await self._finish_run(
                conn, run_id=run_id, source_id=src.id, stats=stats, http_status=http_status
            )
        return stats

    # -- instrument rules -------------------------------------------------------------------

    async def sync_instruments(self, source_key: str) -> RunStats:
        connector = self.market[source_key]
        async with self.db.connect() as conn:
            src = await self._source(conn, source_key)
            listings = await self._listings(conn, src.id)
        run_id = await self._start_run(src.id, "instruments", None)
        stats = RunStats()
        fetched: list[tuple[Any, InstrumentRules]] = []
        http_status: int | None = None
        for listing in listings:
            try:
                result = await connector.fetch_instrument(listing.source_symbol)
                http_status = result.http_status
                fetched.extend((listing, r) for r in result.records)
            except SourceError as exc:
                stats.errors.append(f"{listing.source_symbol}: {exc}")
        now = self.clock.now()
        async with self.db.transaction() as conn:
            for listing, rules in fetched:
                stats.received += 1
                current = (
                    await conn.execute(
                        select(instruments).where(instruments.c.id == listing.instrument_id)
                    )
                ).one()
                changed = {
                    name: (getattr(current, name), getattr(rules, name))
                    for name in ("tick_size", "lot_size", "min_notional", "status")
                    if getattr(current, name) != getattr(rules, name)
                }
                await conn.execute(
                    instruments.update()
                    .where(instruments.c.id == listing.instrument_id)
                    .values(
                        tick_size=rules.tick_size,
                        lot_size=rules.lot_size,
                        min_qty=rules.min_qty,
                        max_qty=rules.max_qty,
                        min_notional=rules.min_notional,
                        price_decimals=rules.price_decimals,
                        qty_decimals=rules.qty_decimals,
                        status=rules.status,
                        metadata=to_jsonable(rules.raw),
                        metadata_source_id=src.id,
                        metadata_fetch_run_id=run_id,
                        metadata_fetched_at=now,
                        updated_at=now,
                    )
                )
                if changed and current.metadata_fetched_at is not None:
                    stats.updated += 1
                    await self._event(
                        conn,
                        listing.instrument_id,
                        "INSTRUMENT_RULES_CHANGED",
                        now,
                        {"changes": {k: [str(a), str(b)] for k, (a, b) in changed.items()}},
                        f"rules:{listing.canonical_key}:{run_id}",
                    )
                elif current.metadata_fetched_at is None:
                    stats.new += 1
            await self._finish_run(
                conn, run_id=run_id, source_id=src.id, stats=stats, http_status=http_status
            )
        return stats

    async def _event(
        self,
        conn: AsyncConnection,
        instrument_id: uuid.UUID | None,
        kind: str,
        now: datetime,
        detail: dict[str, Any],
        dedup_key: str,
        bar_time: datetime | None = None,
    ) -> bool:
        result = await conn.execute(
            pg_insert(market_events)
            .values(
                id=uuid7(),
                instrument_id=instrument_id,
                kind=kind,
                detected_at=now,
                bar_time=bar_time,
                detail=to_jsonable(detail),
                dedup_key=dedup_key,
                status="NEW",
            )
            .on_conflict_do_nothing(index_elements=[market_events.c.dedup_key])
        )
        return result.rowcount > 0

    # -- venue status ------------------------------------------------------------------------

    async def ingest_venue_status(self, source_key: str) -> RunStats:
        connector = self.status[source_key]
        async with self.db.connect() as conn:
            src = await self._source(conn, source_key)
            venue_rows = list(
                await conn.execute(
                    select(venues.c.id).where(venues.c.status_source_key == source_key)
                )
            )
        run_id = await self._start_run(src.id, "venue_status", None)
        stats = RunStats()
        http_status: int | None = None
        records = []
        try:
            result = await connector.fetch_status()
            http_status = result.http_status
            records = result.records
        except SourceError as exc:
            stats.errors.append(str(exc))
        now = self.clock.now()
        async with self.db.transaction() as conn:
            for rec in records:
                stats.received += 1
                if rec.status == "UNKNOWN":
                    await self._quality(
                        conn, run_id, "indicator_known", "WARN", {"indicator": rec.indicator}
                    )
                for v in venue_rows:
                    await conn.execute(
                        venue_status.insert().values(
                            id=uuid7(),
                            venue_id=v.id,
                            source_id=src.id,
                            status=rec.status,
                            indicator=rec.indicator,
                            description=rec.description,
                            source_ts=rec.source_ts,
                            ingested_at=now,
                            fetch_run_id=run_id,
                        )
                    )
                    stats.new += 1
            await self._finish_run(
                conn, run_id=run_id, source_id=src.id, stats=stats, http_status=http_status
            )
        return stats


def mid(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2
