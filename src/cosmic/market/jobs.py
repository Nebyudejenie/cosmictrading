"""Market-data jobs and schedules (polling well below published source limits)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from cosmic.db.schema_market import sources
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.sources.connectors.bitstamp import BitstampConnector
from cosmic.sources.connectors.coinbase import CoinbaseConnector
from cosmic.sources.connectors.statuspage import StatuspageConnector
from cosmic.sources.contract import MarketDataConnector, VenueStatusConnector
from cosmic.sources.http import make_client
from cosmic.sources.ingest import IngestService, ensure_registry
from cosmic.sources.registry import SOURCE_BY_KEY
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_DATA
from cosmic.worker.registry import Periodic, Registry

MARKET_SOURCES = ("coinbase_exchange_public", "bitstamp_public")
STATUS_SOURCES = ("coinbase_status",)
STALE_MULTIPLIER = 3


def _enabled(ctx: WorkerContext) -> bool:
    return ctx.settings.market_data_enabled


def build_ingest_service(ctx: WorkerContext, transport: Any = None) -> IngestService:
    client = make_client(transport)
    ctx.services["http_client"] = client
    market: dict[str, MarketDataConnector] = {
        "coinbase_exchange_public": CoinbaseConnector(client),
        "bitstamp_public": BitstampConnector(client),
    }
    status_def = SOURCE_BY_KEY["coinbase_status"]
    status: dict[str, VenueStatusConnector] = {
        "coinbase_status": StatuspageConnector("coinbase_status", status_def.base_url, client)
    }
    return IngestService(ctx.db, ctx.clock, market, status)


async def startup(ctx: WorkerContext) -> None:
    async with ctx.db.transaction() as conn:
        await ensure_registry(conn, ctx.clock.now())
    if "ingest" not in ctx.services:
        ctx.services["ingest"] = build_ingest_service(ctx)


async def shutdown(ctx: WorkerContext) -> None:
    client = ctx.services.pop("http_client", None)
    if client is not None:
        await client.aclose()


def _ingest(ctx: WorkerContext) -> IngestService:
    service: IngestService = ctx.services["ingest"]
    return service


def register(registry: Registry, ctx: WorkerContext) -> None:
    ctx.services.setdefault("startup_hooks", []).append(startup)
    ctx.services.setdefault("shutdown_hooks", []).append(shutdown)

    @registry.handler("market.registry_sync")
    async def registry_sync(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        async with ctx.db.transaction() as conn:
            await ensure_registry(conn, ctx.clock.now())

    @registry.handler("market.ingest_quotes")
    async def ingest_quotes(ctx: WorkerContext, p: dict[str, Any]) -> None:
        await _ingest(ctx).ingest_quotes(str(p["source"]))

    @registry.handler("market.ingest_bars")
    async def ingest_bars(ctx: WorkerContext, p: dict[str, Any]) -> None:
        await _ingest(ctx).ingest_bars(str(p["source"]), str(p.get("timeframe", "1h")))

    @registry.handler("market.sync_instruments")
    async def sync_instruments(ctx: WorkerContext, p: dict[str, Any]) -> None:
        await _ingest(ctx).sync_instruments(str(p["source"]))

    @registry.handler("market.venue_status")
    async def venue_status_job(ctx: WorkerContext, p: dict[str, Any]) -> None:
        await _ingest(ctx).ingest_venue_status(str(p["source"]))

    @registry.handler("market.backfill_bars")
    async def backfill(ctx: WorkerContext, p: dict[str, Any]) -> None:
        await _ingest(ctx).backfill_bars(
            str(p["source"]), str(p.get("timeframe", "1h")), days=int(p.get("days", 90))
        )

    @registry.handler("market.freshness_check")
    async def freshness_check(ctx: WorkerContext, _p: dict[str, Any]) -> None:
        await check_source_freshness(ctx)

    for key in MARKET_SOURCES:
        registry.every(
            Periodic("market.ingest_quotes", 60, PRIORITY_DATA, {"source": key}, _enabled)
        )
        registry.every(
            Periodic(
                "market.ingest_bars",
                300,
                PRIORITY_DATA,
                {"source": key, "timeframe": "1h"},
                _enabled,
            )
        )
        registry.every(
            Periodic("market.sync_instruments", 21600, PRIORITY_DATA, {"source": key}, _enabled)
        )
    for key in STATUS_SOURCES:
        registry.every(
            Periodic("market.venue_status", 300, PRIORITY_DATA, {"source": key}, _enabled)
        )
    registry.every(Periodic("market.freshness_check", 120, PRIORITY_DATA, {}, _enabled))
    registry.every(Periodic("market.registry_sync", 3600, PRIORITY_DATA, {}, _enabled))


async def check_source_freshness(ctx: WorkerContext) -> None:
    """Open a DATA incident when a source has not succeeded within 3× its freshness limit."""
    now = ctx.clock.now()
    async with ctx.db.connect() as conn:
        rows = list(await conn.execute(select(sources).where(sources.c.enabled.is_(True))))
    async with ctx.db.transaction() as conn:
        for s in rows:
            limit = timedelta(seconds=s.freshness_limit_seconds * STALE_MULTIPLIER)
            key = f"data.source_stale:{s.key}"
            if s.last_success_at is not None and now - s.last_success_at <= limit:
                await resolve_by_key(conn, dedup_key=key, now=now, resolution="source fresh again")
                continue
            await raise_incident(
                conn,
                dedup_key=key,
                category=Category.DATA,
                alert=Alert(
                    title=f"Source {s.name}: no successful fetch in {int(limit.total_seconds())}s",
                    severity="WARNING",
                    what_happened=f"{s.name} has not delivered data within three times its "
                    "freshness limit.",
                    verified=f"Last success: {s.last_success_at or 'never'}.",
                    uncertain="Whether the source, the network or our connector is at fault.",
                    risk_now="Prices from this source are STALE; checks that need them block new "
                    "exposure.",
                    action_taken="Data is labelled STALE; fetching keeps retrying on schedule.",
                    owner_action="Check the source status page and the Sources page; no action is "
                    "needed if it recovers by itself.",
                ),
                now=now,
                detail={
                    "source": s.key,
                    "last_success_at": s.last_success_at,
                    "last_error": s.last_error,
                },
            )
