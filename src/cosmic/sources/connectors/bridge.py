"""Connectors for data a MetaTrader 5 bridge pushes (ADR-028).

The bridge sends its terminal's ticks, symbol specifications and status; these connectors hand
them to the ordinary ingestion service, so pushed prices pass the same validation, provenance
and freshness rules as polled ones. Numbers arrive as strings and become Decimal here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from cosmic.sources.contract import (
    BarRecord,
    FetchResult,
    InstrumentRules,
    QuoteRecord,
    SourceSchemaError,
    SourceUnavailableError,
    VenueStatusRecord,
)

# SYMBOL_TRADE_MODE values exported by the MetaTrader5 package 5.0.6231 (docs/integrations).
TRADE_MODE_STATUS = {4: "ONLINE", 1: "ONLINE", 3: "CANCEL_ONLY", 0: "HALTED"}


def _dec(value: Any, field: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SourceSchemaError(f"{field} is not a number") from None
    if not out.is_finite():
        raise SourceSchemaError(f"{field} is not finite")
    return out


class BridgeMarketConnector:
    """Ticks and symbol rules from one bridge sync (no bars: the bridge pushes none)."""

    def __init__(
        self,
        source_key: str,
        ticks: Mapping[str, Mapping[str, Any]],
        symbols: Mapping[str, Mapping[str, Any]],
        server_utc_offset_ms: int | None = None,
    ) -> None:
        self.source_key = source_key
        self.ticks = ticks
        self.symbols = symbols
        self.offset_ms = server_utc_offset_ms

    async def fetch_quote(self, source_symbol: str) -> FetchResult[QuoteRecord]:
        tick = self.ticks.get(source_symbol)
        if not tick:
            raise SourceUnavailableError(f"no tick for {source_symbol} in this sync")
        if self.offset_ms is None:
            raise SourceUnavailableError("broker server time offset unknown")
        # MT5 stamps ticks in the broker's server time; store UTC.
        ms = int(_dec(tick.get("time_msc"), "time_msc")) - self.offset_ms
        bid, ask = _dec(tick.get("bid"), "bid"), _dec(tick.get("ask"), "ask")
        record = QuoteRecord(
            source_symbol,
            bid,
            ask,
            None,
            datetime.fromtimestamp(ms / 1000, tz=UTC),
        )
        return FetchResult([record], 200)

    async def fetch_instrument(self, source_symbol: str) -> FetchResult[InstrumentRules]:
        s = self.symbols.get(source_symbol)
        if not s:
            raise SourceUnavailableError(f"no specification for {source_symbol} in this sync")
        contract = _dec(s.get("contract_size"), "contract_size")
        if contract <= 0:
            raise SourceSchemaError("contract_size must be positive")
        step, vmin, vmax = (
            _dec(s.get(k), k) * contract for k in ("volume_step", "volume_min", "volume_max")
        )
        digits = int(_dec(s.get("digits"), "digits"))
        rules = InstrumentRules(
            source_symbol=source_symbol,
            tick_size=_dec(s.get("tick_size"), "tick_size"),
            lot_size=step,
            min_qty=vmin,
            max_qty=vmax,
            min_notional=None,
            price_decimals=digits,
            qty_decimals=max(0, -step.normalize().as_tuple().exponent),  # type: ignore[operator]
            status=TRADE_MODE_STATUS.get(int(_dec(s.get("trade_mode"), "trade_mode")), "UNKNOWN"),
            raw={k: s.get(k) for k in sorted(s)},
        )
        return FetchResult([rules], 200)

    async def fetch_bars(self, source_symbol: str, timeframe: str) -> FetchResult[BarRecord]:
        raise SourceUnavailableError("the bridge pushes no bars")

    async def fetch_bars_range(
        self, source_symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> FetchResult[BarRecord]:
        raise SourceUnavailableError("the bridge pushes no bars")


class BridgeStatusConnector:
    """The terminal's state as the venue status: OPERATIONAL only when connected, trading is
    allowed and the account is a demo account."""

    def __init__(
        self, source_key: str, terminal: Mapping[str, Any], account: Mapping[str, Any]
    ) -> None:
        self.source_key = source_key
        self.terminal = terminal
        self.account = account

    async def fetch_status(self) -> FetchResult[VenueStatusRecord]:
        connected = self.terminal.get("connected") is True
        trading = (
            self.terminal.get("trade_allowed") is True
            and self.terminal.get("tradeapi_disabled") is False
        )
        demo = self.account.get("trade_mode") == "DEMO"
        if connected and trading and demo:
            status, text = "OPERATIONAL", "terminal connected; demo account; trading allowed"
        elif connected:
            status = "DEGRADED"
            text = "terminal connected but " + (
                "the account is not a demo account" if not demo else "trading is not allowed"
            )
        else:
            status, text = "UNKNOWN", "terminal not connected to the broker"
        record = VenueStatusRecord(status, "bridge", text, None)
        return FetchResult([record], 200)
