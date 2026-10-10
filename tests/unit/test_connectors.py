"""Connector parsers against responses captured from the real keyless endpoints."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from cosmic.sources.connectors import bitstamp, coinbase, statuspage
from cosmic.sources.contract import SourceSchemaError

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "market"


def load(name: str) -> object:
    return json.loads((FIX / name).read_text(), parse_float=Decimal)


def test_coinbase_ticker() -> None:
    q = coinbase.parse_ticker("BTC-USD", load("coinbase_ticker_BTC-USD.json"))
    assert isinstance(q.bid, Decimal) and isinstance(q.ask, Decimal)
    assert q.bid <= q.ask
    assert q.source_ts.tzinfo is UTC


def test_coinbase_candles_sorted_exact_decimals() -> None:
    bars, rejected = coinbase.parse_candles(
        "BTC-USD", "1h", load("coinbase_candles_1h_BTC-USD.json")
    )
    assert not rejected
    assert len(bars) > 100
    assert all(a.open_time < b.open_time for a, b in itertools.pairwise(bars))
    assert all(isinstance(b.close, Decimal) for b in bars)
    assert all(b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) for b in bars)
    assert all(int(b.open_time.timestamp()) % 3600 == 0 for b in bars)


def test_coinbase_product_rules() -> None:
    rules = coinbase.parse_product("BTC-USD", load("coinbase_product_BTC-USD.json"))
    assert rules.tick_size == Decimal("0.01")
    assert rules.lot_size == Decimal("0.00000001")
    assert rules.min_notional == Decimal("1")
    assert rules.price_decimals == 2 and rules.qty_decimals == 8
    assert rules.status == "ONLINE"


@pytest.mark.parametrize(
    ("flags", "status"),
    [
        ({"trading_disabled": True}, "HALTED"),
        ({"cancel_only": True}, "CANCEL_ONLY"),
        ({"limit_only": True}, "LIMIT_ONLY"),
        ({"status": "delisted"}, "DELISTED"),
        ({"status": "offline"}, "UNKNOWN"),
    ],
)
def test_coinbase_product_status_mapping(flags: dict[str, object], status: str) -> None:
    data = dict(load("coinbase_product_BTC-USD.json"))  # type: ignore[call-overload]
    data.update(flags)
    assert coinbase.parse_product("BTC-USD", data).status == status


def test_coinbase_schema_errors() -> None:
    with pytest.raises(SourceSchemaError):
        coinbase.parse_ticker("BTC-USD", {"bid": "1"})
    with pytest.raises(SourceSchemaError):
        coinbase.parse_candles("BTC-USD", "1h", {"message": "error"})
    with pytest.raises(SourceSchemaError):
        coinbase.parse_product("BTC-USD", {"id": "ETH-USD"})
    bars, rejected = coinbase.parse_candles(
        "BTC-USD", "1h", [[1, 2], [1791662400, "x", 1, 1, 1, 1]]
    )
    assert bars == [] and len(rejected) == 2


def test_bitstamp_parsers() -> None:
    q = bitstamp.parse_ticker("btcusd", load("bitstamp_ticker_btcusd.json"))
    assert q.bid is not None and q.ask is not None and q.bid <= q.ask
    bars, rejected = bitstamp.parse_ohlc("btcusd", "1h", load("bitstamp_ohlc_1h_btcusd.json"))
    assert not rejected and len(bars) == 300
    rules = bitstamp.parse_market("ethusd", load("bitstamp_markets.json"))
    assert rules.lot_size == Decimal("0.000001") and rules.tick_size == Decimal("0.01")
    assert rules.min_notional == Decimal("10.00") and rules.status == "ONLINE"
    with pytest.raises(SourceSchemaError):
        bitstamp.parse_market("dogeusd", load("bitstamp_markets.json"))
    with pytest.raises(SourceSchemaError):
        bitstamp.parse_ohlc("btcusd", "1h", {"data": {}})


def test_cross_venue_prices_close_in_fixtures() -> None:
    cb = coinbase.parse_ticker("BTC-USD", load("coinbase_ticker_BTC-USD.json"))
    bs = bitstamp.parse_ticker("btcusd", load("bitstamp_ticker_btcusd.json"))
    assert cb.last is not None and bs.last is not None
    assert abs(cb.last - bs.last) / cb.last < Decimal("0.01")  # same market, < 1 % apart


def test_statuspage() -> None:
    rec = statuspage.parse_status(load("coinbase_status.json"))
    assert rec.status in {"OPERATIONAL", "DEGRADED", "MAINTENANCE"}
    assert (
        statuspage.parse_status({"status": {"indicator": "weird", "description": "?"}}).status
        == "UNKNOWN"
    )
    assert (
        statuspage.parse_status({"status": {"indicator": "major", "description": "x"}}).status
        == "DEGRADED"
    )
    with pytest.raises(SourceSchemaError):
        statuspage.parse_status({})


def test_fixture_capture_metadata_present() -> None:
    text = (FIX / "CAPTURED.txt").read_text()
    assert "captured_at" in text
    assert datetime.fromisoformat(text.split("captured_at: ")[1].split()[0])
