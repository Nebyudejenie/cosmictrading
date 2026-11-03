"""The cross-venue check compares a price with independent markets first (audit 2026-11-03):
Binance's gold (PAXG/USDT) with HFM's gold, and with PAXG/USD where Coinbase and Bitstamp are
still polled (production retired them, ADR-027). The Binance test network's book copies
Binance's prices, so it is only the last resort, when no independent market is fresh (HFM's
weekend), as before the audit. An order on the test network is still compared with the market."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.core.labels import DataLabel
from cosmic.db.engine import Database
from cosmic.db.schema_market import fetch_runs, instruments, quotes, sources
from cosmic.market import data as md
from tests.trading_env import Env, build_env

D = Decimal
PAXG = ("SPOT:PAXG/USDT@BINANCE", "binance_public")
TESTNET = ("SPOT:PAXG/USDT@BINANCE_TESTNET", "binance_testnet_public")
COINBASE = ("SPOT:PAXG/USD@COINBASE", "coinbase_exchange_public")
BITSTAMP = ("SPOT:PAXG/USD@BITSTAMP", "bitstamp_public")
HFM = ("CFD:XAU/USD@HFM", "hfm_mt5_bridge")


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def _quote(env: Env, market: tuple[str, str], mid: str, *, age: int = 0) -> None:
    at = env.clock.now() - timedelta(seconds=age)
    key, source = market
    async with env.db.transaction() as conn:
        inst = await conn.scalar(select(instruments.c.id).where(instruments.c.canonical_key == key))
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == source))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run, source_id=src, kind="t", started_at=at, finished_at=at, status="SUCCESS"
            )
        )
        await conn.execute(
            pg_insert(quotes)
            .values(
                id=uuid7(),
                instrument_id=inst,
                source_id=src,
                bid=D(mid) - D("0.25"),
                ask=D(mid) + D("0.25"),
                last=D(mid),
                source_ts=at,
                ingested_at=at,
                fetch_run_id=run,
            )
            .on_conflict_do_nothing()
        )


async def _deviation(env: Env, pair: str, venue: str | None = None) -> md.Deviation:
    async with env.db.connect() as conn:
        return await md.cross_source_deviation(conn, pair, env.clock.now(), venue_key=venue)


async def test_binance_gold_is_checked_against_hfm_before_the_test_network(env: Env) -> None:
    await _quote(env, PAXG, "4000.00")
    await _quote(env, TESTNET, "4032.00")  # the test network's book, 80 bps away
    await _quote(env, HFM, "4001.00")
    dev = await _deviation(env, "PAXG/USDT", "BINANCE")
    assert dev.label is DataLabel.DELAYED and dev.bps is not None and dev.bps < D(5)
    assert TESTNET[0] not in {v.canonical_key for v in dev.quotes}


async def test_binance_gold_far_from_hfm_is_caught_though_the_test_network_agrees(env: Env) -> None:
    await _quote(env, PAXG, "4000.00")
    await _quote(env, TESTNET, "4000.00")  # a copy of the same (wrong) price
    await _quote(env, HFM, "4032.00")
    dev = await _deviation(env, "PAXG/USDT", "BINANCE")
    assert dev.label is DataLabel.DELAYED and dev.bps == D(80)


async def test_coinbase_and_bitstamp_count_where_they_are_still_polled(env: Env) -> None:
    await _quote(env, PAXG, "4000.00")
    await _quote(env, TESTNET, "4032.00")
    await _quote(env, COINBASE, "3993.00")  # PAXG/USD about 18 bps below, as on 2026-11-03
    await _quote(env, BITSTAMP, "3993.50")
    dev = await _deviation(env, "PAXG/USDT", "BINANCE")
    assert dev.label is DataLabel.DELAYED and dev.bps is not None and D(15) < dev.bps < D(20)
    assert TESTNET[0] not in {v.canonical_key for v in dev.quotes}


async def test_without_an_independent_market_the_test_network_is_the_last_resort(
    env: Env,
) -> None:
    """HFM's weekend on a server where Coinbase and Bitstamp are retired: as before the audit."""
    await _quote(env, PAXG, "4000.00")
    await _quote(env, HFM, "4001.00", age=3600)
    await _quote(env, TESTNET, "4000.50")
    dev = await _deviation(env, "PAXG/USDT", "BINANCE")
    assert dev.label is DataLabel.DELAYED and dev.bps is not None and dev.bps < D(2)
    assert TESTNET[0] in {v.canonical_key for v in dev.quotes}
    env.clock.advance(hours=1)  # the test network silent too
    await _quote(env, PAXG, "4000.00")
    dev = await _deviation(env, "PAXG/USDT", "BINANCE")
    assert dev.label is DataLabel.STALE and dev.bps is None


async def test_an_order_on_the_test_network_is_still_compared_with_the_market(env: Env) -> None:
    await _quote(env, PAXG, "4000.00")
    await _quote(env, TESTNET, "4032.00")
    dev = await _deviation(env, "PAXG/USDT", "BINANCE_TESTNET")
    assert dev.label is DataLabel.DELAYED and dev.bps == D(80)


async def test_hfm_gold_is_not_compared_with_the_test_network(env: Env) -> None:
    await _quote(env, HFM, "4001.00")
    await _quote(env, PAXG, "4000.00")
    await _quote(env, TESTNET, "4032.00")
    dev = await _deviation(env, "XAU/USD", "HFM")
    assert dev.label is DataLabel.DELAYED and dev.bps is not None and dev.bps < D(5)
