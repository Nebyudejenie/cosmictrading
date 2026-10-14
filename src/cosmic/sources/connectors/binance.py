"""Binance Spot public market data (keyless). Docs: developers.binance.com (Spot API).

Market data only, from ``data-api.binance.vision`` — the base Binance documents for public
market data. No key, no account, no order endpoint lives here (a trading adapter is a separate
module with its own contract tests).

- Quote: ``GET /api/v3/ticker/24hr?symbol=`` → best bid/ask plus ``closeTime`` (ms), the end of
  the rolling window, i.e. when Binance computed the response. ``bookTicker`` carries no time
  at all, so it cannot be labelled for freshness.
- Bars: ``GET /api/v3/klines`` → ``[openTime, open, high, low, close, volume, closeTime, …]``
  oldest first, at most 1000 per request; the still-open kline is included (completeness is
  decided by the ingest service from the clock, as for every source).
- Rules: ``GET /api/v3/exchangeInfo?symbols=[…]`` → PRICE_FILTER / LOT_SIZE / NOTIONAL filters.
- Request weight: 2 per call used here against a documented 6000 per minute per IP; HTTP 429
  and 418 carry ``Retry-After`` and are mapped to rate-limit errors by ``get_json``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from cosmic.sources.connectors._parse import dec
from cosmic.sources.contract import (
    BarRecord,
    FetchResult,
    InstrumentRules,
    QuoteRecord,
    SourceSchemaError,
    VenueStatusRecord,
)
from cosmic.sources.http import TokenBucket, get_json

SOURCE_KEY = "binance_public"
STATUS_SOURCE_KEY = "binance_status"
BASE_URL = "https://data-api.binance.vision/api/v3"
STATUS_URL = "https://api.binance.com/sapi/v1/system/status"
INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "6h": "6h", "1d": "1d"}
MAX_KLINES = 1000
# Binance symbol status → the instrument status vocabulary used by the risk checks.
STATUS = {"TRADING": "ONLINE", "HALT": "HALTED", "BREAK": "HALTED"}


def _ms(value: Any, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SourceSchemaError(f"field {field!r} is not a millisecond timestamp") from exc


def parse_ticker(symbol: str, data: Any) -> QuoteRecord:
    if not isinstance(data, dict) or data.get("symbol") != symbol:
        raise SourceSchemaError("ticker does not match the requested symbol")
    for key in ("bidPrice", "askPrice", "lastPrice", "closeTime"):
        if key not in data:
            raise SourceSchemaError(f"ticker missing {key!r}")
    return QuoteRecord(
        source_symbol=symbol,
        bid=dec(data["bidPrice"], "bidPrice"),
        ask=dec(data["askPrice"], "askPrice"),
        last=dec(data["lastPrice"], "lastPrice"),
        source_ts=_ms(data["closeTime"], "closeTime"),
    )


def parse_klines(
    symbol: str, timeframe: str, data: Any
) -> tuple[list[BarRecord], list[tuple[str, str]]]:
    if not isinstance(data, list):
        raise SourceSchemaError("klines is not a list")
    bars: list[BarRecord] = []
    rejected: list[tuple[str, str]] = []
    for row in data:
        if not isinstance(row, list) or len(row) < 7:
            rejected.append((repr(row)[:40], "malformed kline row"))
            continue
        try:
            bars.append(
                BarRecord(
                    source_symbol=symbol,
                    timeframe=timeframe,
                    open_time=_ms(row[0], "openTime"),
                    open=dec(row[1], "open"),
                    high=dec(row[2], "high"),
                    low=dec(row[3], "low"),
                    close=dec(row[4], "close"),
                    volume=dec(row[5], "volume"),
                )
            )
        except SourceSchemaError as exc:
            rejected.append((repr(row)[:40], str(exc)))
    bars.sort(key=lambda b: b.open_time)
    return bars, rejected


def _step_decimals(step: Any) -> int:
    exponent = dec(step, "step").normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def parse_exchange_info(symbol: str, data: Any) -> InstrumentRules:
    try:
        symbols = data["symbols"]
    except (TypeError, KeyError) as exc:
        raise SourceSchemaError("exchangeInfo missing symbols") from exc
    for s in symbols:
        if not isinstance(s, dict) or s.get("symbol") != symbol:
            continue
        filters = {f.get("filterType"): f for f in s.get("filters", []) if isinstance(f, dict)}
        try:
            price, lot = filters["PRICE_FILTER"], filters["LOT_SIZE"]
        except KeyError as exc:
            raise SourceSchemaError(f"{symbol}: missing filter {exc.args[0]}") from exc
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        status = STATUS.get(str(s.get("status")), "UNKNOWN")
        if status == "ONLINE" and "LIMIT" not in s.get("orderTypes", []):
            status = "UNKNOWN"  # this system only places limit orders
        return InstrumentRules(
            source_symbol=symbol,
            tick_size=dec(price["tickSize"], "tickSize"),
            lot_size=dec(lot["stepSize"], "stepSize"),
            min_qty=dec(lot["minQty"], "minQty"),
            max_qty=dec(lot["maxQty"], "maxQty"),
            min_notional=dec(notional["minNotional"], "minNotional")
            if "minNotional" in notional
            else None,
            price_decimals=_step_decimals(price["tickSize"]),
            qty_decimals=_step_decimals(lot["stepSize"]),
            status=status,
            raw={
                "status": str(s.get("status")),
                "baseAsset": str(s.get("baseAsset")),
                "quoteAsset": str(s.get("quoteAsset")),
                "isSpotTradingAllowed": str(s.get("isSpotTradingAllowed")),
            },
        )
    raise SourceSchemaError(f"symbol {symbol} not listed")


def parse_system_status(data: Any) -> VenueStatusRecord:
    """``{"status": 0|1, "msg": "normal"|"system_maintenance"}``. The response carries no time;
    freshness is judged by when it was ingested, as for every venue status."""
    if not isinstance(data, dict) or "status" not in data:
        raise SourceSchemaError("system status missing 'status'")
    code = data["status"]
    status = {0: "OPERATIONAL", 1: "MAINTENANCE"}.get(int(code), "UNKNOWN")
    return VenueStatusRecord(status, str(code), str(data.get("msg", "")), None)


class BinanceConnector:
    source_key = SOURCE_KEY

    def __init__(self, client: httpx.AsyncClient, bucket: TokenBucket | None = None) -> None:
        self.client = client
        # 2 requests/s × weight 2 = 240 weight/min: 4% of the documented 6000/min.
        self.bucket = bucket or TokenBucket(rate=2.0, capacity=4)

    async def fetch_quote(self, source_symbol: str) -> FetchResult[QuoteRecord]:
        data, status = await get_json(
            self.client,
            f"{BASE_URL}/ticker/24hr",
            params={"symbol": source_symbol},
            bucket=self.bucket,
        )
        return FetchResult([parse_ticker(source_symbol, data)], status)

    async def fetch_bars(self, source_symbol: str, timeframe: str) -> FetchResult[BarRecord]:
        if timeframe not in INTERVAL:
            raise SourceSchemaError(f"unsupported timeframe {timeframe}")
        data, status = await get_json(
            self.client,
            f"{BASE_URL}/klines",
            params={"symbol": source_symbol, "interval": INTERVAL[timeframe], "limit": 300},
            bucket=self.bucket,
        )
        bars, rejected = parse_klines(source_symbol, timeframe, data)
        return FetchResult(bars, status, rejected)

    async def fetch_bars_range(
        self, source_symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> FetchResult[BarRecord]:
        if timeframe not in INTERVAL:
            raise SourceSchemaError(f"unsupported timeframe {timeframe}")
        data, status = await get_json(
            self.client,
            f"{BASE_URL}/klines",
            params={
                "symbol": source_symbol,
                "interval": INTERVAL[timeframe],
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000) - 1,
                "limit": MAX_KLINES,
            },
            bucket=self.bucket,
        )
        bars, rejected = parse_klines(source_symbol, timeframe, data)
        return FetchResult(bars, status, rejected)

    async def fetch_instrument(self, source_symbol: str) -> FetchResult[InstrumentRules]:
        data, status = await get_json(
            self.client,
            f"{BASE_URL}/exchangeInfo",
            params={"symbols": json.dumps([source_symbol], separators=(",", ":"))},
            bucket=self.bucket,
        )
        return FetchResult([parse_exchange_info(source_symbol, data)], status)


class BinanceStatusConnector:
    """Binance's own system-status endpoint (public, no key)."""

    source_key = STATUS_SOURCE_KEY

    def __init__(self, client: httpx.AsyncClient, bucket: TokenBucket | None = None) -> None:
        self.client = client
        self.bucket = bucket or TokenBucket(rate=0.2, capacity=1)

    async def fetch_status(self) -> FetchResult[VenueStatusRecord]:
        data, status = await get_json(self.client, STATUS_URL, bucket=self.bucket)
        return FetchResult([parse_system_status(data)], status)
