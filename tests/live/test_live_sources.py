"""Opt-in smoke test against the real keyless endpoints (``make smoke-live-data``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cosmic.sources.connectors.binance import BinanceConnector, BinanceStatusConnector
from cosmic.sources.connectors.bitstamp import BitstampConnector
from cosmic.sources.connectors.coinbase import CoinbaseConnector
from cosmic.sources.connectors.statuspage import StatuspageConnector
from cosmic.sources.http import make_client
from cosmic.sources.ingest import validate_bar, validate_quote

pytestmark = pytest.mark.live_data


async def test_live_public_sources() -> None:
    now = datetime.now(UTC)
    async with make_client() as client:
        cb, bs = CoinbaseConnector(client), BitstampConnector(client)
        for conn, sym in ((cb, "BTC-USD"), (cb, "ETH-USD"), (bs, "btcusd"), (bs, "ethusd")):
            q = (await conn.fetch_quote(sym)).records[0]
            assert validate_quote(q, now) == []
            assert now - q.source_ts < timedelta(minutes=10)
            bars = (await conn.fetch_bars(sym, "1h")).records
            assert len(bars) >= 100
            assert all(validate_bar(b, now) == [] for b in bars)
            rules = (await conn.fetch_instrument(sym)).records[0]
            assert rules.tick_size and rules.lot_size
        status = await StatuspageConnector(
            "coinbase_status", "https://status.coinbase.com/api/v2", client
        ).fetch_status()
        assert status.records[0].status in {"OPERATIONAL", "DEGRADED", "MAINTENANCE", "UNKNOWN"}


async def test_live_binance_and_gold_listings() -> None:
    """Binance market data plus every gold listing in the registry, against the real APIs."""
    now = datetime.now(UTC)
    async with make_client() as client:
        bn, cb, bs = BinanceConnector(client), CoinbaseConnector(client), BitstampConnector(client)
        for conn, sym in (
            (bn, "BTCUSDT"),
            (bn, "PAXGUSDT"),
            (bn, "XAUTUSDT"),
            (cb, "PAXG-USD"),
            (cb, "BTC-USDT"),
            (bs, "paxgusd"),
        ):
            q = (await conn.fetch_quote(sym)).records[0]
            # Server time can lead this machine's clock by seconds (WSL drift is common).
            assert validate_quote(q, now + timedelta(seconds=60)) == [], sym
            assert abs(now - q.source_ts) < timedelta(minutes=10), sym
            daily = (await conn.fetch_bars_range(sym, "1d", now - timedelta(days=200), now)).records
            assert len(daily) >= 150, (sym, len(daily))
            assert all(validate_bar(b, now + timedelta(seconds=60)) == [] for b in daily), sym
            rules = (await conn.fetch_instrument(sym)).records[0]
            assert rules.tick_size and rules.lot_size and rules.status == "ONLINE", sym
        status = await BinanceStatusConnector(client).fetch_status()
        assert status.records[0].status in {"OPERATIONAL", "MAINTENANCE", "UNKNOWN"}
