"""Binance Spot adapter — Spot Test Network ONLY (sandbox; virtual funds).

Every behaviour below follows the official documentation as reviewed and independently
re-checked on 2026-10-14 (``docs/integrations/binance-spot-api.md``). It is NOT registered for
any account: registration needs the contract suite to pass against the real Spot Test Network
with the owner's testnet keys (B-07), and a live variant needs go-live gates A–I.

How the contract maps onto Binance:
- Idempotent submit: Binance only refuses a client order ID while the first order is open, so
  the adapter first looks the ID up and returns the existing order if there is one.
- Rejections (filter failures, insufficient balance, "would trigger immediately") are results.
  Clock, signature, key and rate-limit errors mean "not accepted": BrokerUnavailableError.
- 5XX, -1006 and -1007 mean "execution status UNKNOWN" (documented): a mutating request then
  raises BrokerAmbiguousError so the caller reconciles instead of resubmitting.
- Order lookups need the symbol; the adapter tries the configured symbols (few) and caches.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import httpx

from cosmic.core.clock import Clock
from cosmic.core.money import canonical_str
from cosmic.trading.brokers.base import (
    AdapterCapabilities,
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
    SubmitRequest,
    SubmitResult,
    VenueBalance,
    VenueFill,
    VenueOrder,
    VenueOrderStatus,
)

TESTNET_BASES = ("https://testnet.binance.vision/api", "https://api1.testnet.binance.vision/api")
STATUS = {
    "NEW": VenueOrderStatus.OPEN,
    "PENDING_NEW": VenueOrderStatus.OPEN,
    "PARTIALLY_FILLED": VenueOrderStatus.PARTIALLY_FILLED,
    "FILLED": VenueOrderStatus.FILLED,
    "CANCELED": VenueOrderStatus.CANCELLED,
    "PENDING_CANCEL": VenueOrderStatus.OPEN,  # documented as currently unused
    "REJECTED": VenueOrderStatus.REJECTED,
    "EXPIRED": VenueOrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": VenueOrderStatus.EXPIRED,  # self-trade prevention expired it
}
TERMINAL = frozenset(
    {
        VenueOrderStatus.FILLED,
        VenueOrderStatus.CANCELLED,
        VenueOrderStatus.REJECTED,
        VenueOrderStatus.EXPIRED,
    }
)
NOT_ACCEPTED = {-1003, -1015, -1021, -1022, -2014, -2015}  # rate limit, clock, signature, key
UNKNOWN_EXECUTION = {-1006, -1007}
NO_SUCH_ORDER = -2013
ORDER_ARCHIVED = -2026
CANCEL_REJECTED = -2011
DUPLICATE = "Duplicate order sent."
TRADES_PAGE = 1000
DAY = timedelta(hours=24)  # documented maximum myTrades time window
TRADE_WINDOW_MARGIN = timedelta(minutes=1)  # clock skew between us and the venue


class Signer(Protocol):
    key_type: str

    def sign(self, payload: str) -> str: ...


class HmacSigner:
    """HMAC-SHA-256 over the payload, hex encoded (documented; HMAC keys are deprecated)."""

    key_type = "HMAC"

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).hexdigest()


class Ed25519Signer:
    """Ed25519 over the payload, base64 encoded (documented; recommended by Binance)."""

    key_type = "ED25519"

    def __init__(self, private_key_pem: bytes) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("not an Ed25519 private key")
        self._key = key

    def sign(self, payload: str) -> str:
        return base64.b64encode(self._key.sign(payload.encode("ascii"))).decode()


@dataclass(frozen=True)
class VenueError:
    http_status: int
    code: int | None
    msg: str


class BinanceSpotAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        signer: Signer,
        symbols: Mapping[str, str],
        clock: Clock,
        base_url: str = TESTNET_BASES[0],
        recv_window_ms: int = 5000,
    ) -> None:
        if base_url not in TESTNET_BASES:
            raise ValueError("this adapter only talks to the Binance Spot Test Network")
        if not 0 < recv_window_ms <= 60_000:
            raise ValueError("recvWindow must be 1–60000 ms (documented maximum)")
        self.client = client
        self.base = base_url
        self._api_key = api_key
        self._signer = signer
        self.symbols = dict(symbols)  # instrument key → Binance symbol
        self._instrument = {v: k for k, v in self.symbols.items()}
        self.clock = clock
        self.recv_window_ms = recv_window_ms
        self._offset_ms: int | None = None
        self._symbol_of: dict[str, str] = {}  # client order ID → symbol
        self._coid_of: dict[tuple[str, int], str] = {}  # (symbol, orderId) → client order ID

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            name="binance_spot_testnet",
            is_live=False,
            is_simulated=True,  # Spot Test Network: virtual funds, real matching engine
            order_types=("LIMIT", "STOP_LIMIT"),
            time_in_force=("GTC", "IOC", "FOK"),
            idempotent_client_ids=True,
            bracket_or_oco=False,
            partial_fills=True,
            data_freshness="venue state per request",
            precision_source="exchangeInfo PRICE_FILTER / LOT_SIZE / NOTIONAL",
            rate_limits="REQUEST_WEIGHT and ORDERS from exchangeInfo; 429/418 Retry-After honoured",
            fees="as reported by the venue per trade (testnet: not real money)",
            venue_status="GET /api/v3/ping (the Spot Test Network has no /sapi status endpoint)",
            reconciliation="query by origClientOrderId; myTrades per symbol",
        )

    # ---- transport -------------------------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self.clock.now().timestamp() * 1000)

    async def _offset(self) -> int:
        """Server time minus our clock (requests must be within recvWindow of server time)."""
        if self._offset_ms is None:
            body = await self._public("GET", "/v3/time")
            self._offset_ms = int(body["serverTime"]) - self._now_ms()
        return self._offset_ms

    async def _public(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self.client.request(method, f"{self.base}{path}", params=params)
        except httpx.TransportError as exc:
            raise BrokerUnavailableError(f"{type(exc).__name__} contacting Binance") from exc
        if resp.status_code != 200:
            raise BrokerUnavailableError(f"Binance answered HTTP {resp.status_code}")
        return _json(resp)

    async def _signed(
        self, method: str, path: str, params: dict[str, Any], *, mutating: bool
    ) -> tuple[Any, VenueError | None]:
        offset = await self._offset()
        full = {**params, "recvWindow": self.recv_window_ms, "timestamp": self._now_ms() + offset}
        payload = urlencode(full)  # percent-encoded, as documented
        signature = quote(self._signer.sign(payload), safe="")
        url = f"{self.base}{path}?{payload}&signature={signature}"
        try:
            resp = await self.client.request(method, url, headers={"X-MBX-APIKEY": self._api_key})
        except httpx.ConnectError as exc:
            raise BrokerUnavailableError("could not connect to Binance (request not sent)") from exc
        except httpx.TransportError as exc:
            if mutating:
                raise BrokerAmbiguousError(f"{type(exc).__name__}: outcome unknown") from exc
            raise BrokerUnavailableError(f"{type(exc).__name__} contacting Binance") from exc
        if resp.status_code in (418, 429):
            retry = resp.headers.get("retry-after", "?")
            raise BrokerUnavailableError(
                f"rate limited (HTTP {resp.status_code}); retry after {retry}s"
            )
        if resp.status_code >= 500:
            if mutating:
                raise BrokerAmbiguousError(f"HTTP {resp.status_code}: execution status unknown")
            raise BrokerUnavailableError(f"Binance internal error HTTP {resp.status_code}")
        body = _json(resp)
        if resp.status_code == 200:
            return body, None
        err = VenueError(resp.status_code, _int(body.get("code")), str(body.get("msg", "")))
        if err.code == -1021:
            self._offset_ms = None  # resynchronise with server time next time
        if err.code in UNKNOWN_EXECUTION:
            if mutating:
                raise BrokerAmbiguousError(f"{err.code} {err.msg}: execution status unknown")
            raise BrokerUnavailableError(f"{err.code} {err.msg}")
        if err.code in NOT_ACCEPTED:
            raise BrokerUnavailableError(f"not accepted: {err.code} {err.msg}")
        return body, err

    # ---- contract ----------------------------------------------------------------------------

    async def submit_order(self, account_ref: uuid.UUID, req: SubmitRequest) -> SubmitResult:
        symbol = self.symbols.get(req.instrument_key)
        if symbol is None:
            return SubmitResult(False, None, f"{req.instrument_key} is not traded on Binance")
        if req.time_in_force not in ("GTC", "IOC", "FOK"):
            return SubmitResult(False, None, f"time in force {req.time_in_force} not supported")
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": req.side,
            "timeInForce": req.time_in_force,
            "quantity": canonical_str(req.quantity),
            "price": canonical_str(req.limit_price),
            "newClientOrderId": req.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if req.order_type == "LIMIT":
            params["type"] = "LIMIT"
        elif req.order_type == "STOP_LIMIT" and req.side == "SELL" and req.stop_price is not None:
            params["type"] = "STOP_LOSS_LIMIT"  # a SELL stop triggers below the market
            params["stopPrice"] = canonical_str(req.stop_price)
        else:
            return SubmitResult(
                False, None, f"order type {req.order_type} {req.side} not supported"
            )
        try:  # idempotency: an order with this client ID already exists → it is the answer
            existing = await self.get_order(account_ref, req.client_order_id)
            return SubmitResult(True, existing.venue_order_id, None, {"idempotent": "existing"})
        except BrokerOrderNotFoundError:
            pass
        body, err = await self._signed("POST", "/v3/order", params, mutating=True)
        if err is None:
            self._symbol_of[req.client_order_id] = symbol
            return SubmitResult(
                True, str(body["orderId"]), None, {"status": str(body.get("status", ""))}
            )
        if err.msg == DUPLICATE:  # open order with this ID raced us: it is the same order
            existing = await self.get_order(account_ref, req.client_order_id)
            return SubmitResult(True, existing.venue_order_id, None, {"idempotent": "duplicate"})
        return SubmitResult(False, None, f"{err.code} {err.msg}".strip(), {"code": str(err.code)})

    async def get_order(self, account_ref: uuid.UUID, client_order_id: str) -> VenueOrder:
        cached = self._symbol_of.get(client_order_id)
        order = [cached] if cached else []
        candidates = order + [s for s in self.symbols.values() if s != cached]
        for symbol in candidates:
            body, err = await self._signed(
                "GET",
                "/v3/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
                mutating=False,
            )
            if err is None:
                self._symbol_of[client_order_id] = symbol
                return self._venue_order(body)
            if err.code == ORDER_ARCHIVED:
                raise BrokerAmbiguousError(f"{client_order_id} is archived at the venue")
            if err.code != NO_SUCH_ORDER:
                raise BrokerUnavailableError(f"order query failed: {err.code} {err.msg}")
        raise BrokerOrderNotFoundError(client_order_id)

    async def cancel_order(self, account_ref: uuid.UUID, client_order_id: str) -> None:
        current = await self.get_order(account_ref, client_order_id)  # may raise NotFound
        if current.status in TERMINAL:
            return  # nothing to cancel (filled, cancelled, rejected or expired)
        symbol = self._symbol_of[client_order_id]
        _, err = await self._signed(
            "DELETE",
            "/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            mutating=True,
        )
        if err is None:
            return
        if err.code == CANCEL_REJECTED:
            after = await self.get_order(account_ref, client_order_id)
            if after.status in TERMINAL:
                return  # it finished meanwhile: the cancel is moot
        raise BrokerAmbiguousError(f"cancel not confirmed: {err.code} {err.msg}")

    async def list_open_orders(self, account_ref: uuid.UUID) -> list[VenueOrder]:
        out: list[VenueOrder] = []
        for symbol in self.symbols.values():
            body, err = await self._signed(
                "GET", "/v3/openOrders", {"symbol": symbol}, mutating=False
            )
            if err is not None:
                raise BrokerUnavailableError(f"open orders failed: {err.code} {err.msg}")
            for o in body:
                self._symbol_of[str(o["clientOrderId"])] = symbol
                out.append(self._venue_order(o))
        return out

    async def get_fills(self, account_ref: uuid.UUID, since: datetime | None) -> list[VenueFill]:
        fills: list[VenueFill] = []
        for symbol in self.symbols.values():
            for t in await self._trades(symbol, since):
                coid = await self._client_id(symbol, int(t["orderId"]))
                fills.append(
                    VenueFill(
                        venue_fill_id=f"{symbol}:{t['id']}",
                        client_order_id=coid,
                        quantity=Decimal(str(t["qty"])),
                        price=Decimal(str(t["price"])),
                        fee=Decimal(str(t["commission"])),
                        fee_asset=str(t["commissionAsset"]),
                        liquidity="MAKER" if t.get("isMaker") else "TAKER",
                        filled_at=datetime.fromtimestamp(int(t["time"]) / 1000, tz=UTC),
                    )
                )
        fills.sort(key=lambda f: f.filled_at)
        return fills

    async def _trades(self, symbol: str, since: datetime | None) -> list[dict[str, Any]]:
        """myTrades: a time window may not exceed 24 hours (documented); otherwise page by id.

        Trade times are the venue's clock: the window is shifted by the measured offset and
        widened by a margin, so a fill stamped a little before ``since`` or after our "now"
        is still found (duplicates are harmless: fills are applied once per fill id)."""
        out: list[dict[str, Any]] = []
        if since is not None and self.clock.now() - since <= DAY * 7:
            offset = await self._offset()
            margin = int(TRADE_WINDOW_MARGIN.total_seconds() * 1000)
            start = int(since.timestamp() * 1000) + offset - margin
            stop = self._now_ms() + offset + margin
            day = int(DAY.total_seconds() * 1000)
            while start <= stop:
                end = min(start + day - 1, stop)
                body, err = await self._signed(
                    "GET",
                    "/v3/myTrades",
                    {"symbol": symbol, "startTime": start, "endTime": end, "limit": TRADES_PAGE},
                    mutating=False,
                )
                if err is not None:
                    raise BrokerUnavailableError(f"trades failed: {err.code} {err.msg}")
                out.extend(body)
                start = end + 1
            return out
        from_id = 0
        while True:
            body, err = await self._signed(
                "GET",
                "/v3/myTrades",
                {"symbol": symbol, "fromId": from_id, "limit": TRADES_PAGE},
                mutating=False,
            )
            if err is not None:
                raise BrokerUnavailableError(f"trades failed: {err.code} {err.msg}")
            out.extend(body)
            if len(body) < TRADES_PAGE:
                return out
            from_id = int(body[-1]["id"]) + 1

    async def _client_id(self, symbol: str, order_id: int) -> str:
        key = (symbol, order_id)
        if key not in self._coid_of:
            body, err = await self._signed(
                "GET", "/v3/order", {"symbol": symbol, "orderId": order_id}, mutating=False
            )
            if err is not None:
                raise BrokerUnavailableError(f"order lookup failed: {err.code} {err.msg}")
            self._coid_of[key] = str(body["clientOrderId"])
        return self._coid_of[key]

    async def get_balances(self, account_ref: uuid.UUID) -> list[VenueBalance]:
        body, err = await self._signed(
            "GET", "/v3/account", {"omitZeroBalances": "true"}, mutating=False
        )
        if err is not None:
            raise BrokerUnavailableError(f"account failed: {err.code} {err.msg}")
        out = []
        for b in body.get("balances", []):
            free, locked = Decimal(str(b["free"])), Decimal(str(b["locked"]))
            out.append(VenueBalance(str(b["asset"]), free + locked, free))
        return out

    async def venue_status(self) -> str:
        try:
            await self._public("GET", "/v3/ping")
        except BrokerUnavailableError:
            return "UNKNOWN"
        return "OPERATIONAL"

    def _venue_order(self, o: Mapping[str, Any]) -> VenueOrder:
        symbol = str(o["symbol"])
        status = STATUS.get(str(o["status"]))
        if status is None:
            raise BrokerAmbiguousError(f"unknown Binance order status {o['status']}")
        self._coid_of[(symbol, int(o["orderId"]))] = str(o["clientOrderId"])
        return VenueOrder(
            client_order_id=str(o["clientOrderId"]),
            venue_order_id=str(o["orderId"]),
            instrument_key=self._instrument.get(symbol, symbol),
            side=str(o["side"]),
            status=status,
            quantity=Decimal(str(o["origQty"])),
            filled_qty=Decimal(str(o["executedQty"])),
            limit_price=Decimal(str(o["price"])),
            reject_reason=str(o.get("expiryReason")) if o.get("expiryReason") else None,
        )


def _json(resp: httpx.Response) -> Any:
    try:
        return json.loads(resp.text, parse_float=Decimal)
    except ValueError as exc:
        raise BrokerUnavailableError(
            f"Binance answered non-JSON (HTTP {resp.status_code})"
        ) from exc


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
