from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.labels import DataLabel
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_market import (
    bars,
    data_quality_results,
    fetch_runs,
    market_events,
    quotes,
    sources,
)
from cosmic.market import data as md
from cosmic.market.jobs import build_ingest_service, check_source_freshness
from cosmic.sources.ingest import IngestService, ensure_registry
from cosmic.worker.context import WorkerContext
from tests.market_mock import MarketMock

# Fixture capture time is 2026-10-10 ~21:05 UTC; quotes are fresh shortly after.
CAPTURE = datetime(2026, 10, 10, 21, 6, 0, tzinfo=UTC)


@pytest.fixture
def mclock() -> FrozenClock:
    return FrozenClock(CAPTURE)


@pytest.fixture
def mock() -> MarketMock:
    return MarketMock()


@pytest.fixture
async def ctx(
    settings: Settings, db: Database, mclock: FrozenClock, mock: MarketMock
) -> WorkerContext:
    c = WorkerContext(settings=settings, db=db, clock=mclock, worker_id="test")
    async with db.transaction() as conn:
        await ensure_registry(conn, mclock.now())
    c.services["ingest"] = build_ingest_service(c, transport=mock.transport())
    return c


def svc(ctx: WorkerContext) -> IngestService:
    s: IngestService = ctx.services["ingest"]
    return s


async def count(db: Database, table: object, *where: object) -> int:
    async with db.connect() as conn:
        return int(await conn.scalar(select(func.count()).select_from(table).where(*where)) or 0)  # type: ignore[arg-type]


async def test_registry_idempotent_with_full_provenance(db: Database, mclock: FrozenClock) -> None:
    async with db.transaction() as conn:
        first = await ensure_registry(conn, mclock.now())
        second = await ensure_registry(conn, mclock.now())
    assert first > 0 and second == 0
    async with db.connect() as conn:
        rows = list(await conn.execute(select(sources)))
    assert {r.key for r in rows} == {
        "coinbase_exchange_public",
        "bitstamp_public",
        "coinbase_status",
        "binance_public",
        "binance_status",
    }
    for r in rows:
        assert r.publisher and r.licence_note and r.latency_class == "DELAYED_POLLING"
        assert r.freshness_limit_seconds > 0 and r.quality_checks and r.known_bias


async def test_quotes_idempotent_and_labelled(
    ctx: WorkerContext, db: Database, mclock: FrozenClock
) -> None:
    stats = await svc(ctx).ingest_quotes("coinbase_exchange_public")
    # BTC-USD, ETH-USD, PAXG-USD and BTC-USDT
    assert (stats.received, stats.new, stats.rejected) == (4, 4, 0)
    again = await svc(ctx).ingest_quotes("coinbase_exchange_public")
    assert again.new == 0  # same source timestamps → no duplicates
    async with db.connect() as conn:
        inst = await md.instrument_by_key(conn, "SPOT:BTC/USD@COINBASE")
        q = await md.latest_quote(conn, inst.id, mclock.now())
        runs = list(await conn.execute(select(fetch_runs.c.status)))
        src = (
            await conn.execute(select(sources).where(sources.c.key == "coinbase_exchange_public"))
        ).one()
    assert q.label is DataLabel.DELAYED and q.source_key == "coinbase_exchange_public"
    assert q.spread_bps is not None and q.spread_bps >= 0
    assert all(r.status == "SUCCESS" for r in runs)
    assert src.last_success_at == mclock.now()
    mclock.advance(minutes=10)
    async with db.connect() as conn:
        stale = await md.latest_quote(conn, inst.id, mclock.now())
    assert stale.label is DataLabel.STALE


async def test_bars_idempotent_open_bar_and_revision(
    ctx: WorkerContext, db: Database, mock: MarketMock, mclock: FrozenClock
) -> None:
    stats = await svc(ctx).ingest_bars("coinbase_exchange_public", "1h")
    assert stats.rejected == 0 and stats.new == stats.received > 600
    assert (await svc(ctx).ingest_bars("coinbase_exchange_public", "1h")).new == 0
    open_bars = await count(db, bars, bars.c.is_complete.is_(False))
    assert open_bars == 2  # one still-open bar per product
    async with db.connect() as conn:
        inst = await md.instrument_by_key(conn, "SPOT:BTC/USD@COINBASE")
        complete = await md.complete_bars(conn, inst.id, "1h", limit=1000)
    assert all(b.is_complete for b in complete)
    assert complete[-1].open_time + timedelta(hours=1) <= mclock.now()
    # Source revises a completed bar → updated, with a WARN quality record (vintage trail).
    body = mock.body("/products/BTC-USD/candles")
    body[5][4] = float(body[5][4]) + 1.0  # close of an older, completed bar
    mock.overrides["/products/BTC-USD/candles"] = body
    revised = await svc(ctx).ingest_bars("coinbase_exchange_public", "1h")
    assert revised.updated >= 1
    assert (
        await count(db, data_quality_results, data_quality_results.c.check_name == "bar_revised")
        == 1
    )


async def test_invalid_records_rejected_with_reasons(
    ctx: WorkerContext, db: Database, mock: MarketMock
) -> None:
    mock.overrides["/products/BTC-USD/ticker"] = {
        "bid": "101",
        "ask": "100",
        "price": "100",
        "time": "2026-10-10T21:05:00Z",
    }
    stats = await svc(ctx).ingest_quotes("coinbase_exchange_public")
    assert stats.rejected == 1 and stats.new == 3
    async with db.connect() as conn:
        checks = [
            r.check_name for r in await conn.execute(select(data_quality_results.c.check_name))
        ]
        status = (await conn.execute(select(fetch_runs.c.status))).scalar_one()
    assert checks == ["bid_not_above_ask"]
    assert status == "PARTIAL"


@pytest.mark.parametrize(
    "fault",
    [("status", 503), ("status", 429, {"retry-after": "30"}), httpx.ConnectTimeout("timeout")],
)
async def test_source_outage_recorded_and_incident(
    ctx: WorkerContext, db: Database, mock: MarketMock, mclock: FrozenClock, fault: object
) -> None:
    for path in ("/api/v2/ticker/btcusd/", "/api/v2/ticker/ethusd/", "/api/v2/ticker/paxgusd/"):
        mock.overrides[path] = fault  # the whole source is down
    stats = await svc(ctx).ingest_quotes("bitstamp_public")
    assert stats.received == 0 and len(stats.errors) == 3
    async with db.connect() as conn:
        run = (await conn.execute(select(fetch_runs))).one()
        src = (await conn.execute(select(sources).where(sources.c.key == "bitstamp_public"))).one()
    assert run.status == "FAILED" and run.error
    assert src.last_success_at is None and src.last_error
    await check_source_freshness(ctx)
    assert (
        await count(
            db,
            incidents,
            incidents.c.dedup_key == "data.source_stale:bitstamp_public",
            incidents.c.status == "OPEN",
        )
        == 1
    )
    mock.overrides.clear()
    await svc(ctx).ingest_quotes("bitstamp_public")
    await check_source_freshness(ctx)
    assert (
        await count(
            db,
            incidents,
            incidents.c.dedup_key == "data.source_stale:bitstamp_public",
            incidents.c.status == "RESOLVED",
        )
        == 1
    )


async def test_cross_source_deviation(
    ctx: WorkerContext, db: Database, mclock: FrozenClock
) -> None:
    await svc(ctx).ingest_quotes("coinbase_exchange_public")
    async with db.connect() as conn:
        dev = await md.cross_source_deviation(conn, "BTC/USD", mclock.now())
    assert (
        dev.label is DataLabel.STALE and dev.bps is None
    )  # Bitstamp never fetched → no comparison
    await svc(ctx).ingest_quotes("bitstamp_public")
    async with db.connect() as conn:
        dev = await md.cross_source_deviation(conn, "BTC/USD", mclock.now())
    assert dev.label is DataLabel.DELAYED
    assert dev.bps is not None and Decimal(0) <= dev.bps < Decimal(100)


async def test_instrument_rules_and_change_event(
    ctx: WorkerContext, db: Database, mock: MarketMock
) -> None:
    await svc(ctx).sync_instruments("coinbase_exchange_public")
    async with db.connect() as conn:
        inst = await md.instrument_by_key(conn, "SPOT:BTC/USD@COINBASE")
    assert inst.tick_size == Decimal("0.01") and inst.lot_size == Decimal("0.00000001")
    assert inst.status == "ONLINE" and inst.tradable and inst.metadata_fetched_at is not None
    body = mock.body("/products/BTC-USD")
    body["trading_disabled"] = True
    mock.overrides["/products/BTC-USD"] = body
    await svc(ctx).sync_instruments("coinbase_exchange_public")
    async with db.connect() as conn:
        inst = await md.instrument_by_key(conn, "SPOT:BTC/USD@COINBASE")
    assert inst.status == "HALTED"
    assert await count(db, market_events, market_events.c.kind == "INSTRUMENT_RULES_CHANGED") == 1


async def test_bitstamp_rules_and_venue_status(
    ctx: WorkerContext, db: Database, mclock: FrozenClock
) -> None:
    await svc(ctx).sync_instruments("bitstamp_public")
    await svc(ctx).ingest_venue_status("coinbase_status")
    async with db.connect() as conn:
        inst = await md.instrument_by_key(conn, "SPOT:ETH/USD@BITSTAMP")
        status, label = await md.latest_venue_status(conn, "COINBASE", mclock.now())
        _, bs_label = await md.latest_venue_status(conn, "BITSTAMP", mclock.now())
    assert inst.min_notional == Decimal("10.00") and not inst.tradable
    assert status == "OPERATIONAL" and label is DataLabel.DELAYED
    assert bs_label is DataLabel.NOT_CONFIGURED  # no status source for Bitstamp: shown honestly


async def test_ingestion_uses_only_fixtures(ctx: WorkerContext, mock: MarketMock) -> None:
    await svc(ctx).ingest_quotes("coinbase_exchange_public")
    assert mock.calls and all(c.startswith("api.exchange.coinbase.com/") for c in mock.calls)
    assert await count(ctx.db, quotes) == 4
