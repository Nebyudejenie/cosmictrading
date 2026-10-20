"""ADR-027: paper trading on Binance prices; Coinbase and Bitstamp retired, their data kept."""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

from sqlalchemy import func, select

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_execution import orders
from cosmic.db.schema_market import (
    bars,
    fetch_runs,
    instruments,
    quotes,
    sources,
    venue_status,
    venues,
)
from cosmic.db.schema_trading import positions
from cosmic.market import jobs as market_jobs
from cosmic.market.jobs import build_ingest_service, check_source_freshness
from cosmic.sources.ingest import ensure_registry
from cosmic.trading import authority
from cosmic.trading.accounts import get_account, paper_account_for_venue
from cosmic.trading.jobs import retired_venue_exposure
from cosmic.trading.modes import Mode
from cosmic.trading.paper_accounts import open_paper_account
from cosmic.trading.policy import ensure_default_policy
from cosmic.trading.services import TradingServices
from cosmic.worker.context import WorkerContext
from cosmic.worker.registry import Registry
from tests.helpers import make_user, principal_for
from tests.market_mock import MarketMock
from tests.trading_env import CAPTURE, Env, build_env

BTC = "SPOT:BTC/USDT@BINANCE"
RETIRED = ["COINBASE", "BITSTAMP"]


async def count(db: Database, table: Any, *where: Any) -> int:
    async with db.connect() as conn:
        return int(await conn.scalar(select(func.count()).select_from(table).where(*where)) or 0)


async def set_binance(db: Database, clock: FrozenClock, bid: str, ask: str) -> None:
    """Fresh BTC/USDT quotes on Binance and its test network, and Binance's status."""
    now = clock.now()
    async with db.transaction() as conn:
        for key, source in ((BTC, "binance_public"), (f"{BTC}_TESTNET", "binance_testnet_public")):
            inst = await conn.scalar(
                select(instruments.c.id).where(instruments.c.canonical_key == key)
            )
            src = await conn.scalar(select(sources.c.id).where(sources.c.key == source))
            run_id = uuid7()
            await conn.execute(
                fetch_runs.insert().values(
                    id=run_id,
                    source_id=src,
                    kind="test_quote",
                    started_at=now,
                    finished_at=now,
                    status="SUCCESS",
                )
            )
            await conn.execute(
                quotes.insert().values(
                    id=uuid7(),
                    instrument_id=inst,
                    source_id=src,
                    bid=D(bid),
                    ask=D(ask),
                    last=D(bid),
                    source_ts=now,
                    ingested_at=now,
                    fetch_run_id=run_id,
                )
            )
        await conn.execute(
            venue_status.insert().values(
                id=uuid7(),
                venue_id=await conn.scalar(select(venues.c.id).where(venues.c.key == "BINANCE")),
                source_id=await conn.scalar(
                    select(sources.c.id).where(sources.c.key == "binance_status")
                ),
                status="OPERATIONAL",
                indicator="normal",
                description="test",
                source_ts=now,
                ingested_at=now,
            )
        )


async def binance_env(settings: Settings, db: Database) -> Env:
    clock = FrozenClock(CAPTURE)
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="test")
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    ingest = build_ingest_service(ctx, transport=MarketMock().transport())
    for key in ("binance_public", "binance_testnet_public"):
        await ingest.sync_instruments(key)
    owner = await make_user(db, clock)
    svc = TradingServices.build(db, clock, settings)
    async with db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=principal_for(owner, clock),
            target=Mode.PAPER_TRADING,
            reason="paper trading on Binance prices",
            now=clock.now(),
            settings=settings,
        )
        account_id = await open_paper_account(
            conn,
            broker=svc.paper,
            alias="paper-binance",
            initial_capital=D("10000"),
            now=clock.now(),
            actor=Actor.user(owner.user_id),
            venue_key="BINANCE",
            base_currency="USDT",
        )
    await set_binance(db, clock, "82990.00", "83000.00")
    result = await svc.reconciler.reconcile(account_id, trigger="account_opened")
    assert result.passed, result.mismatches
    return Env(settings, db, clock, svc, owner, account_id)


async def test_paper_trade_on_binance_prices(settings: Settings, db: Database) -> None:
    env = await binance_env(settings, db)
    async with db.connect() as conn:
        acct = await get_account(conn, account_id=env.account_id)
    assert (acct.kind, acct.venue_key, acct.base_currency) == ("PAPER", "BINANCE", "USDT")
    r = await env.propose(account_alias="paper-binance", instrument_key=BTC)
    assert r.status == "PROPOSED", r.problems
    await env.approve(r.intent_id)
    await env.cycle(2)  # the simulator fills against Binance's ask; the stop follows
    async with db.connect() as conn:
        rows = {o.purpose: o for o in await conn.execute(select(orders))}
        held = await conn.scalar(
            select(positions.c.quantity).where(positions.c.account_id == env.account_id)
        )
    assert rows["ENTRY"].status == "FILLED" and rows["ENTRY"].is_simulated
    assert rows["STOP_LOSS"].status == "ACKNOWLEDGED"
    assert held == D("0.004")


async def test_retired_venues_stop_fetching_but_keep_their_data(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)  # Coinbase and Bitstamp data stored, paper on Coinbase
    stored = await count(db, quotes)
    retired = settings.model_copy(update={"retired_venues": RETIRED, "market_data_enabled": True})
    ctx = WorkerContext(settings=retired, db=db, clock=env.clock, worker_id="test")
    async with db.transaction() as conn:
        await ensure_registry(conn, env.clock.now(), retired_venues=frozenset(RETIRED))
    async with db.connect() as conn:
        enabled = {r.key: r.enabled for r in await conn.execute(select(sources))}
        tradable = {
            r.canonical_key: r.tradable
            for r in await conn.execute(select(instruments.c.canonical_key, instruments.c.tradable))
        }
    assert not enabled["coinbase_exchange_public"] and not enabled["bitstamp_public"]
    assert not enabled["coinbase_status"]
    assert enabled["binance_public"] and enabled["binance_testnet_public"]
    assert not tradable["SPOT:BTC/USD@COINBASE"] and tradable[BTC]
    # Scheduled fetching skips them; nothing stored is deleted.
    registry = Registry()
    market_jobs.register(registry, ctx)
    polled = {
        p.payload["source"]
        for p in registry.periodic
        if p.kind == "market.ingest_quotes" and p.enabled(ctx)
    }
    assert polled == {"binance_public", "binance_testnet_public"}
    assert await count(db, quotes) == stored and await count(db, bars) >= 0
    # A retired source going quiet is not a stale-data incident.
    env.clock.advance(hours=6)
    await check_source_freshness(ctx)
    async with db.connect() as conn:
        keys = {r.dedup_key for r in await conn.execute(select(incidents.c.dedup_key))}
    assert "data.source_stale:coinbase_exchange_public" not in keys
    # Un-retiring brings them back.
    async with db.transaction() as conn:
        await ensure_registry(conn, env.clock.now())
    async with db.connect() as conn:
        assert await conn.scalar(
            select(instruments.c.tradable).where(
                instruments.c.canonical_key == "SPOT:BTC/USD@COINBASE"
            )
        )


async def test_holdings_left_on_a_retired_venue_raise_an_incident(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)
    r = await env.propose()
    await env.approve(r.intent_id)
    await env.cycle(2)  # a Coinbase paper position with its stop
    retired = settings.model_copy(update={"retired_venues": RETIRED})
    ctx = WorkerContext(settings=retired, db=db, clock=env.clock, worker_id="test")
    found = await retired_venue_exposure(ctx)
    assert set(found) == {"COINBASE"} and len(found["COINBASE"]) == 2  # position + stop
    # DIR-02: each item names its account with the environment.
    assert all(x.startswith("paper-main [SIMULATED · PAPER] ") for x in found["COINBASE"])
    async with db.connect() as conn:
        open_ = await conn.scalar(
            select(func.count())
            .select_from(incidents)
            .where(
                incidents.c.dedup_key == "trading.retired_venue_exposure:COINBASE",
                incidents.c.status != "RESOLVED",
            )
        )
    assert open_ == 1


async def test_automatic_proposals_use_the_paper_account_of_the_instruments_venue(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)  # paper-main on COINBASE, opened first
    async with db.transaction() as conn:
        binance = await open_paper_account(
            conn,
            broker=env.svc.paper,
            alias="paper-binance",
            initial_capital=D("1000"),
            now=env.clock.now(),
            actor=Actor.user(env.owner.user_id),
            venue_key="BINANCE",
            base_currency="USDT",
        )
    async with db.connect() as conn:
        assert await paper_account_for_venue(conn, "BINANCE") == binance
        assert await paper_account_for_venue(conn, "COINBASE") == env.account_id
        assert await paper_account_for_venue(conn, "BINANCE_TESTNET") is None
