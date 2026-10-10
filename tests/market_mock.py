"""httpx MockTransport serving captured fixtures, with injectable faults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

FIX = Path(__file__).resolve().parent / "fixtures" / "market"

ROUTES = {
    "/products/BTC-USD/ticker": "coinbase_ticker_BTC-USD.json",
    "/products/ETH-USD/ticker": "coinbase_ticker_ETH-USD.json",
    "/products/BTC-USD/candles": "coinbase_candles_1h_BTC-USD.json",
    "/products/ETH-USD/candles": "coinbase_candles_1h_ETH-USD.json",
    "/products/BTC-USD": "coinbase_product_BTC-USD.json",
    "/products/ETH-USD": "coinbase_product_ETH-USD.json",
    "/api/v2/ticker/btcusd/": "bitstamp_ticker_btcusd.json",
    "/api/v2/ticker/ethusd/": "bitstamp_ticker_ethusd.json",
    "/api/v2/ohlc/btcusd/": "bitstamp_ohlc_1h_btcusd.json",
    "/api/v2/ohlc/ethusd/": "bitstamp_ohlc_1h_ethusd.json",
    "/api/v2/markets/": "bitstamp_markets.json",
    "/api/v2/status.json": "coinbase_status.json",
}


class MarketMock:
    def __init__(self) -> None:
        self.overrides: dict[
            str, Any
        ] = {}  # path → body (json-able) or ("status", code) or exception
        self.calls: list[str] = []

    def body(self, path: str) -> Any:
        return json.loads((FIX / ROUTES[path]).read_text())

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.url.host}{path}")
        if path in self.overrides:
            o = self.overrides[path]
            if isinstance(o, Exception):
                raise o
            if isinstance(o, tuple) and o[0] == "status":
                return httpx.Response(o[1], headers=o[2] if len(o) > 2 else None, text="{}")
            return httpx.Response(200, text=json.dumps(o))
        if path not in ROUTES:
            return httpx.Response(404, text="{}")
        return httpx.Response(200, text=(FIX / ROUTES[path]).read_text())

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
