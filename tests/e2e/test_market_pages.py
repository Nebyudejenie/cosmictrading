from __future__ import annotations

from datetime import UTC, datetime

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Role
from cosmic.market.jobs import build_ingest_service
from cosmic.sources.ingest import IngestService, ensure_registry
from cosmic.web.app import create_app
from cosmic.worker.context import WorkerContext
from tests.helpers import client_for, full_login, make_user
from tests.market_mock import MarketMock

CAPTURE = datetime(2026, 10, 10, 21, 6, 0, tzinfo=UTC)


async def _ingest_all(settings: Settings, db: Database, clock: FrozenClock) -> None:
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="t")
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
    svc: IngestService = build_ingest_service(ctx, transport=MarketMock().transport())
    for key in ("coinbase_exchange_public", "bitstamp_public"):
        await svc.sync_instruments(key)
        await svc.ingest_quotes(key)
        await svc.ingest_bars(key, "1h")
    await svc.ingest_venue_status("coinbase_status")


async def test_market_pages_show_labelled_data(settings: Settings, db: Database) -> None:
    clock = FrozenClock(CAPTURE)
    await _ingest_all(settings, db, clock)
    app = create_app(settings, db=db, clock=clock)
    user = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    async with client_for(app) as c:
        await full_login(c, user, clock)
        sources = await c.get("/sources")
        market = await c.get("/market")
        detail = await c.get("/market/SPOT:BTC/USD@COINBASE")
        missing = await c.get("/market/SPOT:DOGE/USD@NOWHERE")
    assert sources.status_code == 200
    assert "Coinbase Exchange public market data" in sources.text
    assert "Coverage gaps" in sources.text and "NOT_CONFIGURED" in sources.text
    assert market.status_code == 200
    assert "SPOT:ETH/USD@BITSTAMP" in market.text and "Reference only" in market.text
    assert "DELAYED" in market.text and "LIVE<" not in market.text
    assert detail.status_code == 200
    assert "<svg" in detail.text and "RSI 14" in detail.text and "Tick size" in detail.text
    assert missing.status_code == 404


async def test_market_pages_report_stale_data(settings: Settings, db: Database) -> None:
    clock = FrozenClock(CAPTURE)
    await _ingest_all(settings, db, clock)
    clock.advance(hours=2)
    app = create_app(settings, db=db, clock=clock)
    user = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, user, clock)
        market = await c.get("/market")
    assert "STALE" in market.text


async def test_market_pages_require_auth(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    app = create_app(settings, db=db, clock=clock)
    async with client_for(app) as c:
        for path in ("/sources", "/market", "/market/SPOT:BTC/USD@COINBASE"):
            r = await c.get(path)
            assert r.status_code == 303 and r.headers["location"] == "/login"
