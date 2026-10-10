"""Normalised records and the connector contract (DATA-02)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


class SourceError(Exception):
    """Base error for connector failures (message is safe to store)."""


class SourceUnavailableError(SourceError):
    pass


class SourceRateLimitedError(SourceError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SourceSchemaError(SourceError):
    """The response no longer matches the documented structure."""


@dataclass(frozen=True)
class QuoteRecord:
    source_symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    source_ts: datetime


@dataclass(frozen=True)
class BarRecord:
    source_symbol: str
    timeframe: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class InstrumentRules:
    source_symbol: str
    tick_size: Decimal | None
    lot_size: Decimal | None
    min_qty: Decimal | None
    max_qty: Decimal | None
    min_notional: Decimal | None
    price_decimals: int | None
    qty_decimals: int | None
    status: str  # ONLINE | LIMIT_ONLY | CANCEL_ONLY | POST_ONLY | HALTED | DELISTED | UNKNOWN
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VenueStatusRecord:
    status: str  # OPERATIONAL | DEGRADED | MAINTENANCE | UNKNOWN
    indicator: str
    description: str
    source_ts: datetime | None


@dataclass(frozen=True)
class FetchResult[T]:
    records: list[T]
    http_status: int
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (subject, reason)


class MarketDataConnector(Protocol):
    source_key: str

    async def fetch_quote(self, source_symbol: str) -> FetchResult[QuoteRecord]: ...

    async def fetch_bars(self, source_symbol: str, timeframe: str) -> FetchResult[BarRecord]: ...

    async def fetch_instrument(self, source_symbol: str) -> FetchResult[InstrumentRules]: ...


class VenueStatusConnector(Protocol):
    source_key: str

    async def fetch_status(self) -> FetchResult[VenueStatusRecord]: ...
