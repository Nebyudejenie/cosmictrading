"""httpx MockTransport serving captured fixtures, with injectable faults.

Most fixtures were captured on 2026-10-10 ~21:05 UTC and tests freeze the clock just after.
The PAXG and USDT listings and Binance were added later (captured 2026-10-14, see
``fixtures/market/CAPTURED.txt``); the mock shifts their timestamps back by ``REBASE_DAYS``
whole days — whole days keep hourly and daily bars aligned — so one frozen clock serves every
listing in the registry. Prices and other fields are served exactly as captured.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

FIX = Path(__file__).resolve().parent / "fixtures" / "market"
REBASE_DAYS = 4
DAY_S = 86_400

ROUTES = {
    "/products/BTC-USD/ticker": "coinbase_ticker_BTC-USD.json",
    "/products/ETH-USD/ticker": "coinbase_ticker_ETH-USD.json",
    "/products/BTC-USD/candles": "coinbase_candles_1h_BTC-USD.json",
    "/products/ETH-USD/candles": "coinbase_candles_1h_ETH-USD.json",
    "/products/BTC-USD": "coinbase_product_BTC-USD.json",
    "/products/ETH-USD": "coinbase_product_ETH-USD.json",
    "/products/PAXG-USD/ticker": "coinbase_ticker_PAXG-USD.json",
    "/products/PAXG-USD/candles": "coinbase_candles_1h_PAXG-USD.json",
    "/products/PAXG-USD": "coinbase_product_PAXG-USD.json",
    "/products/BTC-USDT/ticker": "coinbase_ticker_BTC-USDT.json",
    "/products/BTC-USDT/candles": "coinbase_candles_1h_BTC-USDT.json",
    "/products/BTC-USDT": "coinbase_product_BTC-USDT.json",
    "/api/v2/ticker/btcusd/": "bitstamp_ticker_btcusd.json",
    "/api/v2/ticker/ethusd/": "bitstamp_ticker_ethusd.json",
    "/api/v2/ticker/paxgusd/": "bitstamp_ticker_paxgusd.json",
    "/api/v2/ohlc/btcusd/": "bitstamp_ohlc_1h_btcusd.json",
    "/api/v2/ohlc/ethusd/": "bitstamp_ohlc_1h_ethusd.json",
    "/api/v2/ohlc/paxgusd/": "bitstamp_ohlc_1h_paxgusd.json",
    "/api/v2/markets/": "bitstamp_markets.json",
    "/api/v2/status.json": "coinbase_status.json",
    "/sapi/v1/system/status": "binance_system_status.json",
    "/api/v3/ping": "binance_ping.json",
    "/api/v3/exchangeInfo": "binance_exchangeinfo.json",
}
# Binance passes the symbol (and interval) as query parameters.
BINANCE = {
    ("/api/v3/ticker/24hr", "BTCUSDT", None): "binance_ticker24h_BTCUSDT.json",
    ("/api/v3/ticker/24hr", "PAXGUSDT", None): "binance_ticker24h_PAXGUSDT.json",
    ("/api/v3/ticker/24hr", "XAUTUSDT", None): "binance_ticker24h_XAUTUSDT.json",
    ("/api/v3/klines", "BTCUSDT", "1h"): "binance_klines_1h_BTCUSDT.json",
    ("/api/v3/klines", "PAXGUSDT", "1h"): "binance_klines_1h_PAXGUSDT.json",
    ("/api/v3/klines", "XAUTUSDT", "1h"): "binance_klines_1h_XAUTUSDT.json",
    ("/api/v3/klines", "PAXGUSDT", "1d"): "binance_klines_1d_PAXGUSDT.json",
}
LATER = {
    "coinbase_ticker_PAXG-USD.json",
    "coinbase_candles_1h_PAXG-USD.json",
    "coinbase_ticker_BTC-USDT.json",
    "coinbase_candles_1h_BTC-USDT.json",
    "bitstamp_ticker_paxgusd.json",
    "bitstamp_ohlc_1h_paxgusd.json",
    *BINANCE.values(),
}


def _rebase(name: str, data: Any) -> Any:
    """Shift a later capture's timestamps back by REBASE_DAYS (format per source)."""
    s, ms = REBASE_DAYS * DAY_S, REBASE_DAYS * DAY_S * 1000
    if name.startswith("coinbase_ticker"):
        from datetime import datetime, timedelta

        t = datetime.fromisoformat(data["time"].replace("Z", "+00:00")) - timedelta(
            days=REBASE_DAYS
        )
        return {**data, "time": t.isoformat().replace("+00:00", "Z")}
    if name.startswith("coinbase_candles"):
        return [[row[0] - s, *row[1:]] for row in data]
    if name.startswith("bitstamp_ticker"):
        return {**data, "timestamp": str(int(data["timestamp"]) - s)}
    if name.startswith("bitstamp_ohlc"):
        rows = [{**r, "timestamp": str(int(r["timestamp"]) - s)} for r in data["data"]["ohlc"]]
        return {**data, "data": {**data["data"], "ohlc": rows}}
    if name.startswith("binance_ticker24h"):
        return {**data, "openTime": data["openTime"] - ms, "closeTime": data["closeTime"] - ms}
    if name.startswith("binance_klines"):
        return [[r[0] - ms, *r[1:6], r[6] - ms, *r[7:]] for r in data]
    return data


def load(name: str) -> Any:
    data = json.loads((FIX / name).read_text())
    if name == "bitstamp_markets.json":
        data = data + json.loads((FIX / "bitstamp_markets_paxgusd.json").read_text())
    return _rebase(name, data) if name in LATER else data


class MarketMock:
    def __init__(self) -> None:
        self.overrides: dict[
            str, Any
        ] = {}  # path → body (json-able) or ("status", code) or exception
        self.calls: list[str] = []

    def body(self, path: str) -> Any:
        return load(ROUTES[path])

    def _fixture(self, request: httpx.Request) -> str | None:
        path = request.url.path
        if path in ROUTES:
            return ROUTES[path]
        params = request.url.params
        return BINANCE.get((path, params.get("symbol"), params.get("interval")))

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
        name = self._fixture(request)
        if name is None:
            return httpx.Response(404, text="{}")
        if name in LATER or name == "bitstamp_markets.json":
            return httpx.Response(200, text=json.dumps(load(name)))
        return httpx.Response(200, text=(FIX / name).read_text())  # byte-for-byte as captured

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
