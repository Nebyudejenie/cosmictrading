"""Opt-in smoke test against the real keyless endpoints (``make smoke-live-data``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
