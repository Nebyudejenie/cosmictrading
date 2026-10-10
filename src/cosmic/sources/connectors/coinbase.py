"""Coinbase Exchange public REST (keyless). Docs: docs.cdp.coinbase.com/exchange.

Candles: ``GET /products/{id}/candles?granularity=`` → arrays
``[time, low, high, open, close, volume]``, newest first, including the open bucket.
"""

from __future__ import annotations

from typing import Any

import httpx

from cosmic.sources.connectors._parse import dec, epoch, iso
from cosmic.sources.contract import (
    TIMEFRAME_SECONDS,
    BarRecord,
    FetchResult,
    InstrumentRules,
    QuoteRecord,
    SourceSchemaError,
)
from cosmic.sources.http import TokenBucket, get_json

SOURCE_KEY = "coinbase_exchange_public"
BASE_URL = "https://api.exchange.coinbase.com"
GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


def parse_ticker(symbol: str, data: Any) -> QuoteRecord:
    if not isinstance(data, dict):
        raise SourceSchemaError("ticker is not an object")
    for key in ("bid", "ask", "price", "time"):
        if key not in data:
            raise SourceSchemaError(f"ticker missing {key!r}")
    return QuoteRecord(
        source_symbol=symbol,
        bid=dec(data["bid"], "bid"),
        ask=dec(data["ask"], "ask"),
        last=dec(data["price"], "price"),
        source_ts=iso(data["time"], "time"),
    )


def parse_candles(
    symbol: str, timeframe: str, data: Any
) -> tuple[list[BarRecord], list[tuple[str, str]]]:
    if not isinstance(data, list):
        raise SourceSchemaError("candles is not a list")
    bars: list[BarRecord] = []
    rejected: list[tuple[str, str]] = []
    for row in data:
        if not isinstance(row, list) or len(row) < 6:
            rejected.append((repr(row)[:40], "malformed candle row"))
            continue
        try:
            t, low, high, open_, close, volume = row[:6]
            bars.append(
                BarRecord(
                    source_symbol=symbol,
                    timeframe=timeframe,
                    open_time=epoch(t, "time"),
                    open=dec(open_, "open"),
                    high=dec(high, "high"),
                    low=dec(low, "low"),
                    close=dec(close, "close"),
                    volume=dec(volume, "volume"),
                )
            )
        except SourceSchemaError as exc:
            rejected.append((repr(row)[:40], str(exc)))
    bars.sort(key=lambda b: b.open_time)
    return bars, rejected


def parse_product(symbol: str, data: Any) -> InstrumentRules:
    if not isinstance(data, dict) or data.get("id") != symbol:
        raise SourceSchemaError("product response does not match the requested symbol")
    if data.get("trading_disabled"):
        status = "HALTED"
    elif data.get("status") == "delisted":
        status = "DELISTED"
    elif data.get("cancel_only"):
        status = "CANCEL_ONLY"
    elif data.get("limit_only"):
        status = "LIMIT_ONLY"
    elif data.get("post_only"):
        status = "POST_ONLY"
    elif data.get("status") == "online" and not data.get("auction_mode"):
        status = "ONLINE"
    else:
        status = "UNKNOWN"
    tick = dec(data["quote_increment"], "quote_increment")
    lot = dec(data["base_increment"], "base_increment")
    keep = (
        "status",
        "trading_disabled",
        "cancel_only",
        "limit_only",
        "post_only",
        "auction_mode",
        "min_market_funds",
        "quote_increment",
        "base_increment",
        "max_slippage_percentage",
    )
    return InstrumentRules(
        source_symbol=symbol,
        tick_size=tick,
        lot_size=lot,
        min_qty=lot,
        max_qty=None,
        min_notional=dec(data["min_market_funds"], "min_market_funds"),
        price_decimals=max(0, -tick.normalize().as_tuple().exponent),  # type: ignore[operator]
        qty_decimals=max(0, -lot.normalize().as_tuple().exponent),  # type: ignore[operator]
        status=status,
        raw={k: str(data.get(k)) for k in keep if k in data},
    )


class CoinbaseConnector:
    source_key = SOURCE_KEY

    def __init__(self, client: httpx.AsyncClient, bucket: TokenBucket | None = None) -> None:
        self.client = client
        self.bucket = bucket or TokenBucket(rate=3.0, capacity=3)

    async def fetch_quote(self, source_symbol: str) -> FetchResult[QuoteRecord]:
        data, status = await get_json(
            self.client, f"{BASE_URL}/products/{source_symbol}/ticker", bucket=self.bucket
        )
        return FetchResult([parse_ticker(source_symbol, data)], status)

    async def fetch_bars(self, source_symbol: str, timeframe: str) -> FetchResult[BarRecord]:
        if timeframe not in GRANULARITY:
            raise SourceSchemaError(f"unsupported timeframe {timeframe}")
        data, status = await get_json(
            self.client,
            f"{BASE_URL}/products/{source_symbol}/candles",
            params={"granularity": GRANULARITY[timeframe]},
            bucket=self.bucket,
        )
        bars, rejected = parse_candles(source_symbol, timeframe, data)
        assert TIMEFRAME_SECONDS[timeframe] == GRANULARITY[timeframe]
        return FetchResult(bars, status, rejected)

    async def fetch_instrument(self, source_symbol: str) -> FetchResult[InstrumentRules]:
        data, status = await get_json(
            self.client, f"{BASE_URL}/products/{source_symbol}", bucket=self.bucket
        )
        return FetchResult([parse_product(source_symbol, data)], status)
