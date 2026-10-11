"""Bitstamp public REST v2 (keyless). Docs: www.bitstamp.net/api/.

OHLC: ``GET /ohlc/{pair}/?step=&limit=`` → ``{"data": {"ohlc": [{timestamp, open, high,
low, close, volume}, ...]}}`` oldest first; the current candle is included by default.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from cosmic.sources.connectors._parse import dec, decimals_to_step, epoch
from cosmic.sources.contract import (
    BarRecord,
    FetchResult,
    InstrumentRules,
    QuoteRecord,
    SourceSchemaError,
)
from cosmic.sources.http import TokenBucket, get_json

SOURCE_KEY = "bitstamp_public"
BASE_URL = "https://www.bitstamp.net/api/v2"
STEP = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


def parse_ticker(symbol: str, data: Any) -> QuoteRecord:
    if not isinstance(data, dict):
        raise SourceSchemaError("ticker is not an object")
    for key in ("bid", "ask", "last", "timestamp"):
        if key not in data:
            raise SourceSchemaError(f"ticker missing {key!r}")
    return QuoteRecord(
        source_symbol=symbol,
        bid=dec(data["bid"], "bid"),
        ask=dec(data["ask"], "ask"),
        last=dec(data["last"], "last"),
        source_ts=epoch(data["timestamp"], "timestamp"),
    )


def parse_ohlc(
    symbol: str, timeframe: str, data: Any
) -> tuple[list[BarRecord], list[tuple[str, str]]]:
    try:
        rows = data["data"]["ohlc"]
    except (TypeError, KeyError) as exc:
        raise SourceSchemaError("ohlc response missing data.ohlc") from exc
    if not isinstance(rows, list):
        raise SourceSchemaError("data.ohlc is not a list")
    bars: list[BarRecord] = []
    rejected: list[tuple[str, str]] = []
    for row in rows:
        try:
            bars.append(
                BarRecord(
                    source_symbol=symbol,
                    timeframe=timeframe,
                    open_time=epoch(row["timestamp"], "timestamp"),
                    open=dec(row["open"], "open"),
                    high=dec(row["high"], "high"),
                    low=dec(row["low"], "low"),
                    close=dec(row["close"], "close"),
                    volume=dec(row["volume"], "volume"),
                )
            )
        except (SourceSchemaError, KeyError, TypeError) as exc:
            rejected.append((repr(row)[:40], str(exc) or "malformed row"))
    bars.sort(key=lambda b: b.open_time)
    return bars, rejected


def parse_market(symbol: str, markets: Any) -> InstrumentRules:
    if not isinstance(markets, list):
        raise SourceSchemaError("markets is not a list")
    for m in markets:
        if isinstance(m, dict) and m.get("market_symbol") == symbol:
            base_dec = int(m["base_decimals"])
            counter_dec = int(m["counter_decimals"])
            lot = decimals_to_step(base_dec)
            return InstrumentRules(
                source_symbol=symbol,
                tick_size=decimals_to_step(counter_dec),
                lot_size=lot,
                min_qty=lot,
                max_qty=None,
                min_notional=dec(m["minimum_order_value"], "minimum_order_value"),
                price_decimals=counter_dec,
                qty_decimals=base_dec,
                status="ONLINE" if m.get("trading") == "Enabled" else "HALTED",
                raw={
                    k: str(m.get(k))
                    for k in ("trading", "instant_and_market_orders", "market_type")
                },
            )
    raise SourceSchemaError(f"market {symbol} not listed")


class BitstampConnector:
    source_key = SOURCE_KEY

    def __init__(self, client: httpx.AsyncClient, bucket: TokenBucket | None = None) -> None:
        self.client = client
        self.bucket = bucket or TokenBucket(rate=2.0, capacity=2)

    async def fetch_quote(self, source_symbol: str) -> FetchResult[QuoteRecord]:
        data, status = await get_json(
            self.client, f"{BASE_URL}/ticker/{source_symbol}/", bucket=self.bucket
        )
        return FetchResult([parse_ticker(source_symbol, data)], status)

    async def fetch_bars(self, source_symbol: str, timeframe: str) -> FetchResult[BarRecord]:
        if timeframe not in STEP:
            raise SourceSchemaError(f"unsupported timeframe {timeframe}")
        data, status = await get_json(
            self.client,
            f"{BASE_URL}/ohlc/{source_symbol}/",
            params={"step": STEP[timeframe], "limit": 300},
            bucket=self.bucket,
        )
        bars, rejected = parse_ohlc(source_symbol, timeframe, data)
        return FetchResult(bars, status, rejected)

    async def fetch_bars_range(
        self, source_symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> FetchResult[BarRecord]:
        data, status = await get_json(
            self.client,
            f"{BASE_URL}/ohlc/{source_symbol}/",
            params={
                "step": STEP[timeframe],
                "limit": 1000,
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
            },
            bucket=self.bucket,
        )
        bars, rejected = parse_ohlc(source_symbol, timeframe, data)
        return FetchResult(bars, status, rejected)

    async def fetch_instrument(self, source_symbol: str) -> FetchResult[InstrumentRules]:
        data, status = await get_json(self.client, f"{BASE_URL}/markets/", bucket=self.bucket)
        return FetchResult([parse_market(source_symbol, data)], status)
