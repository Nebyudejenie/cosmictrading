"""In-process stand-in for the Binance Spot Test Network (contract tests only).

Behaviour follows ``docs/integrations/binance-spot-api.md`` (official docs, reviewed
2026-10-14): signed requests (HMAC hex or Ed25519 base64 over the percent-encoded query),
timestamp/recvWindow, the documented error codes and messages, filters taken from the real
exchangeInfo capture, NEW/PARTIALLY_FILLED/FILLED/CANCELED/EXPIRED statuses, commission charged
in the received asset. It is a test double, not a claim about undocumented venue behaviour:
where the docs are silent (e.g. the message for cancelling a finished order) it chooses the
documented generic answer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

FIX = Path(__file__).resolve().parent / "fixtures" / "market" / "binance_exchangeinfo.json"
COMMISSION = Decimal("0.001")  # 0.1 %, charged in the asset received
OTHER_FEE = Decimal("0.00001")  # flat fee when fees are paid in another asset (e.g. BNB)
D = Decimal


@dataclass
class Order:
    order_id: int
    client_order_id: str
    symbol: str
    side: str
    type: str
    tif: str
    qty: Decimal
    price: Decimal
    stop: Decimal | None
    status: str = "NEW"
    executed: Decimal = Decimal(0)
    quote: Decimal = Decimal(0)
    time: int = 0
    triggered: bool = False

    def json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "orderId": self.order_id,
            "orderListId": -1,
            "clientOrderId": self.client_order_id,
            "price": str(self.price),
            "origQty": str(self.qty),
            "executedQty": str(self.executed),
            "cummulativeQuoteQty": str(self.quote),
            "status": self.status,
            "timeInForce": self.tif,
            "type": self.type,
            "side": self.side,
            "stopPrice": str(self.stop or 0),
            "time": self.time,
            "updateTime": self.time,
            "isWorking": self.type == "LIMIT" or self.triggered,
            "selfTradePreventionMode": "EXPIRE_MAKER",
        }


@dataclass
class BinanceEmulator:
    api_key: str = "test-api-key"
    hmac_secret: str | None = "test-secret"
    ed25519_public_pem: bytes | None = None
    server_ms: int = 1_791_900_000_000
    balances: dict[str, list[Decimal]] = field(
        default_factory=lambda: {
            "USDT": [D("10000"), D(0)],
            "BTC": [D("1"), D(0)],
            "PAXG": [D("2"), D(0)],
        }
    )
    book: dict[str, tuple[Decimal, Decimal]] = field(
        default_factory=lambda: {
            "BTCUSDT": (D("82990.00"), D("83000.00")),
            "PAXGUSDT": (D("4100.00"), D("4100.50")),
            "XAUTUSDT": (D("4095.00"), D("4095.50")),
        }
    )
    faults: list[str] = field(default_factory=list)  # "5xx" | "timeout" | "connect" | "429"
    fee_asset: str | None = None  # pay every fee in this asset instead (BNB fee option)
    orders: dict[int, Order] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        info = json.loads(FIX.read_text())
        self.filters = {
            s["symbol"]: {f["filterType"]: f for f in s["filters"]} for s in info["symbols"]
        }
        self.assets = {s["symbol"]: (s["baseAsset"], s["quoteAsset"]) for s in info["symbols"]}
        self._next = 1000

    # ---- helpers ------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def move(self, symbol: str, bid: str, ask: str) -> None:
        """Market moves: trigger stops and match resting orders."""
        self.book[symbol] = (D(bid), D(ask))
        for o in list(self.orders.values()):
            if o.symbol == symbol and o.status in ("NEW", "PARTIALLY_FILLED"):
                self._match(o)

    @staticmethod
    def _err(code: int, msg: str, status: int = 400) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "msg": msg})

    def _verify(self, request: httpx.Request) -> tuple[dict[str, str], httpx.Response | None]:
        raw = request.url.query.decode()
        payload, _, signature = raw.rpartition("&signature=")
        params = dict(parse_qsl(payload, keep_blank_values=True))
        if request.headers.get("X-MBX-APIKEY") != self.api_key:
            return params, self._err(-2015, "Invalid API-key, IP, or permissions for action.", 401)
        from urllib.parse import unquote

        sig = unquote(signature)
        ok = False
        if self.hmac_secret is not None:
            expected = hmac.new(self.hmac_secret.encode(), payload.encode(), hashlib.sha256)
            ok = hmac.compare_digest(expected.hexdigest(), sig.lower())
        elif self.ed25519_public_pem is not None:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            key = load_pem_public_key(self.ed25519_public_pem)
            try:
                key.verify(base64.b64decode(sig), payload.encode())  # type: ignore[union-attr,call-arg]
                ok = True
            except (InvalidSignature, ValueError):
                ok = False
        if not ok:
            return params, self._err(-1022, "Signature for this request is not valid.")
        ts, window = int(params.get("timestamp", 0)), int(params.get("recvWindow", 5000))
        if not (ts < self.server_ms + 1000 and self.server_ms - ts <= window):
            return params, self._err(
                -1021, "Timestamp for this request is outside of the recvWindow."
            )
        return params, None

    # ---- request handling ---------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        self.requests.append(f"{request.method} {path}")
        if self.faults:
            fault = self.faults.pop(0)
            if fault == "connect":
                raise httpx.ConnectError("refused", request=request)
            if fault == "timeout":
                raise httpx.ReadTimeout("no answer", request=request)
            if fault == "5xx":
                return httpx.Response(503, text="Service Unavailable")
            if fault == "429":
                return httpx.Response(
                    429,
                    headers={"Retry-After": "7"},
                    json={"code": -1003, "msg": "Too many requests."},
                )
        if path == "/v3/time":
            return httpx.Response(200, json={"serverTime": self.server_ms})
        if path == "/v3/ping":
            return httpx.Response(200, json={})
        if path == "/v3/exchangeInfo":
            info = json.loads(FIX.read_text())
            symbol = request.url.params.get("symbol")
            info["symbols"] = [x for x in info["symbols"] if symbol in (None, x["symbol"])]
            return httpx.Response(200, json=info)
        if path == "/v3/ticker/bookTicker":
            symbol = request.url.params.get("symbol", "")
            bid, ask = self.book[symbol]
            return httpx.Response(
                200,
                json={
                    "symbol": symbol,
                    "bidPrice": str(bid),
                    "bidQty": "1",
                    "askPrice": str(ask),
                    "askQty": "1",
                },
            )
        params, problem = self._verify(request)
        if problem is not None:
            return problem
        route = {
            ("POST", "/v3/order"): self._new,
            ("GET", "/v3/order"): self._query,
            ("DELETE", "/v3/order"): self._cancel,
            ("GET", "/v3/openOrders"): self._open,
            ("GET", "/v3/myTrades"): self._my_trades,
            ("GET", "/v3/account"): self._account,
        }.get((request.method, path))
        if route is None:
            return self._err(-1100, "Illegal characters found in parameter.", 404)
        return route(params)

    def _filters_ok(self, symbol: str, qty: Decimal, price: Decimal) -> str | None:
        f = self.filters[symbol]
        pf, lot = f.get("PRICE_FILTER"), f.get("LOT_SIZE")
        if pf and (
            price < D(pf["minPrice"])
            or price > D(pf["maxPrice"])
            or (price - D(pf["minPrice"])) % D(pf["tickSize"]) != 0
        ):
            return "Filter failure: PRICE_FILTER"
        if lot and (
            qty < D(lot["minQty"])
            or qty > D(lot["maxQty"])
            or (qty - D(lot["minQty"])) % D(lot["stepSize"]) != 0
        ):
            return "Filter failure: LOT_SIZE"
        notional = f.get("NOTIONAL")
        if notional and not D(notional["minNotional"]) <= price * qty <= D(notional["maxNotional"]):
            return "Filter failure: NOTIONAL"
        return None

    def _new(self, p: dict[str, str]) -> httpx.Response:
        symbol = p.get("symbol", "")
        if symbol not in self.filters:
            return self._err(-1121, "Invalid symbol.")
        kind, side = p.get("type"), p.get("side")
        if kind not in ("LIMIT", "STOP_LOSS_LIMIT") or side not in ("BUY", "SELL"):
            return self._err(-1116, "Invalid orderType.")
        qty, price = D(p["quantity"]), D(p["price"])
        stop = D(p["stopPrice"]) if "stopPrice" in p else None
        if kind == "STOP_LOSS_LIMIT" and stop is None:
            return self._err(
                -1102, "Mandatory parameter 'stopPrice' was not sent, was empty/null, or malformed."
            )
        failure = self._filters_ok(symbol, qty, price)
        if failure:
            return self._err(-1013, failure)
        coid = p.get("newClientOrderId") or f"auto{self._next}"
        if any(
            o.client_order_id == coid and o.status in ("NEW", "PARTIALLY_FILLED")
            for o in self.orders.values()
        ):
            return self._err(-2010, "Duplicate order sent.")
        base, quote = self.assets[symbol]
        bid, _ask = self.book[symbol]
        if kind == "STOP_LOSS_LIMIT" and side == "SELL" and stop is not None and bid <= stop:
            return self._err(-2010, "Order would trigger immediately.")
        need_asset, need = (quote, qty * price) if side == "BUY" else (base, qty)
        free = self.balances.setdefault(need_asset, [D(0), D(0)])
        if free[0] < need:
            return self._err(-2010, "Account has insufficient balance for requested action.")
        free[0] -= need
        free[1] += need
        self._next += 1
        o = Order(
            self._next,
            coid,
            symbol,
            side,
            kind,
            p.get("timeInForce", "GTC"),
            qty,
            price,
            stop,
            time=self.server_ms,
        )
        self.orders[o.order_id] = o
        self._match(o)
        if o.tif == "IOC" and o.status in ("NEW", "PARTIALLY_FILLED"):
            self._close(o, "EXPIRED")
        return httpx.Response(
            200, json={**o.json(), "transactTime": self.server_ms, "workingTime": self.server_ms}
        )

    def _match(self, o: Order) -> None:
        bid, ask = self.book[o.symbol]
        if o.type == "STOP_LOSS_LIMIT" and not o.triggered:
            if o.stop is not None and bid <= o.stop:
                o.triggered = True
            else:
                return
        if o.side == "BUY" and ask <= o.price:
            self._fill(o, ask)
        elif o.side == "SELL" and bid >= o.price:
            self._fill(o, bid)

    def _fill(self, o: Order, price: Decimal) -> None:
        base, quote = self.assets[o.symbol]
        qty = o.qty - o.executed
        o.executed += qty
        o.quote += qty * price
        o.status = "FILLED"
        other = self.fee_asset
        if o.side == "BUY":
            self.balances[quote][1] -= qty * o.price
            self.balances[quote][0] += qty * (o.price - price)  # price improvement returned
            fee, fee_asset = (OTHER_FEE, other) if other else (qty * COMMISSION, base)
            received = qty if other else qty - fee
            self.balances.setdefault(base, [D(0), D(0)])[0] += received
        else:
            self.balances[base][1] -= qty
            fee, fee_asset = (OTHER_FEE, other) if other else (qty * price * COMMISSION, quote)
            received = qty * price if other else qty * price - fee
            self.balances.setdefault(quote, [D(0), D(0)])[0] += received
        if other:
            self.balances.setdefault(other, [D(0), D(0)])[0] -= fee
        self.trades.append(
            {
                "symbol": o.symbol,
                "id": len(self.trades) + 1,
                "orderId": o.order_id,
                "orderListId": -1,
                "price": str(price),
                "qty": str(qty),
                "quoteQty": str(qty * price),
                "commission": str(fee),
                "commissionAsset": fee_asset,
                "time": self.server_ms,
                "isBuyer": o.side == "BUY",
                "isMaker": False,
                "isBestMatch": True,
            }
        )

    def _close(self, o: Order, status: str) -> None:
        base, quote = self.assets[o.symbol]
        rest = o.qty - o.executed
        if o.side == "BUY":
            self.balances[quote][1] -= rest * o.price
            self.balances[quote][0] += rest * o.price
        else:
            self.balances[base][1] -= rest
            self.balances[base][0] += rest
        o.status = status

    def _find(self, p: dict[str, str]) -> Order | None:
        cands = [o for o in self.orders.values() if o.symbol == p.get("symbol")]
        if "orderId" in p:
            return next((o for o in cands if o.order_id == int(p["orderId"])), None)
        matching = [o for o in cands if o.client_order_id == p.get("origClientOrderId")]
        return matching[-1] if matching else None

    def _query(self, p: dict[str, str]) -> httpx.Response:
        o = self._find(p)
        if o is None:
            return self._err(-2013, "Order does not exist.")
        return httpx.Response(200, json=o.json())

    def _cancel(self, p: dict[str, str]) -> httpx.Response:
        o = self._find(p)
        if o is None or o.status not in ("NEW", "PARTIALLY_FILLED"):
            return self._err(-2011, "Unknown order sent.")
        self._close(o, "CANCELED")
        return httpx.Response(200, json={**o.json(), "origClientOrderId": o.client_order_id})

    def _open(self, p: dict[str, str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                o.json()
                for o in self.orders.values()
                if o.symbol == p.get("symbol") and o.status in ("NEW", "PARTIALLY_FILLED")
            ],
        )

    def _my_trades(self, p: dict[str, str]) -> httpx.Response:
        rows = [t for t in self.trades if t["symbol"] == p.get("symbol")]
        if "fromId" in p:
            rows = [t for t in rows if t["id"] >= int(p["fromId"])]
        if "startTime" in p:
            rows = [t for t in rows if int(p["startTime"]) <= t["time"] <= int(p["endTime"])]
        return httpx.Response(200, json=rows[: int(p.get("limit", 500))])

    def _account(self, _p: dict[str, str]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "canTrade": True,
                "canWithdraw": False,
                "canDeposit": False,
                "accountType": "SPOT",
                "permissions": ["SPOT"],
                "balances": [
                    {"asset": a, "free": str(v[0]), "locked": str(v[1])}
                    for a, v in self.balances.items()
                    if v[0] or v[1]
                ],
            },
        )
