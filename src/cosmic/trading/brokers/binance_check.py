"""Broker-contract run against the REAL Binance Spot Test Network (B-07, ADR-025).

Used by ``cosmic broker binance-testnet-check`` (on the server, keys via stdin) and by the
opt-in pytest ``tests/sandbox``. Places one tiny BUY far below the market and cancels it; prints
step results only — never keys, signatures or account identifiers.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
    SubmitRequest,
    VenueOrderStatus,
)
from cosmic.trading.brokers.binance_spot import BinanceSpotAdapter

INSTRUMENT = "SPOT:BTC/USDT@BINANCE_TESTNET"
SYMBOL = "BTCUSDT"
# Identifies the steps below; recorded with every run (bump when the steps change).
CONTRACT_VERSION = "binance-spot-contract-1"
MIN_SPEND = Decimal("6")  # above the documented NOTIONAL minimum of 5 USDT for BTCUSDT


@dataclass(frozen=True)
class Step:
    name: str
    passed: bool
    detail: str = ""


async def _filters(adapter: BinanceSpotAdapter) -> tuple[Decimal, Decimal]:
    resp = await adapter.client.get(f"{adapter.base}/v3/exchangeInfo", params={"symbol": SYMBOL})
    resp.raise_for_status()
    f = {x["filterType"]: x for x in resp.json()["symbols"][0]["filters"]}
    return Decimal(f["PRICE_FILTER"]["tickSize"]), Decimal(f["LOT_SIZE"]["stepSize"])


async def run_contract(adapter: BinanceSpotAdapter) -> list[Step]:
    """Each contract step as PASS/FAIL. A refusal by the venue ends the run as a failed step
    with Binance's code and message (e.g. -2015 for a key the venue does not recognise)."""
    steps: list[Step] = []
    try:
        await _run(adapter, steps)
    except (BrokerUnavailableError, BrokerAmbiguousError) as exc:
        steps.append(Step("venue accepted the request", False, str(exc)))
    return steps


async def _run(adapter: BinanceSpotAdapter, steps: list[Step]) -> list[Step]:
    account = uuid.uuid4()

    def check(name: str, ok: bool, detail: str = "") -> bool:
        steps.append(Step(name, ok, detail))
        return ok

    status = await adapter.venue_status()
    if not check("venue reachable (ping)", status == "OPERATIONAL", status):
        return steps
    tick, step = await _filters(adapter)
    book = (
        await adapter.client.get(f"{adapter.base}/v3/ticker/bookTicker", params={"symbol": SYMBOL})
    ).json()
    price = (Decimal(book["bidPrice"]) * Decimal("0.8") / tick).to_integral_value(ROUND_DOWN) * tick
    qty = (MIN_SPEND / price / step).to_integral_value(ROUND_UP) * step
    coid = f"ct-sbx-{int(time.time())}"
    req = SubmitRequest(coid, INSTRUMENT, "BUY", "LIMIT", qty, price, None, "GTC", None)
    first = await adapter.submit_order(account, req)
    if not check("submit a resting limit order", first.accepted, first.reject_reason or ""):
        return steps
    again = await adapter.submit_order(account, req)
    check(
        "re-submit with the same client order ID returns the same order",
        again.accepted and again.venue_order_id == first.venue_order_id,
    )
    order = await adapter.get_order(account, coid)
    check(
        "query by client order ID",
        order.status is VenueOrderStatus.OPEN and order.quantity == qty,
        order.status.value,
    )
    listed = await adapter.list_open_orders(account)
    check("listed among open orders", any(o.client_order_id == coid for o in listed))
    await adapter.cancel_order(account, coid)
    after = await adapter.get_order(account, coid)
    check(
        "cancel confirmed by the venue",
        after.status is VenueOrderStatus.CANCELLED,
        after.status.value,
    )
    try:
        await adapter.get_order(account, f"{coid}-never")
        check("unknown order is reported as not found", False)
    except BrokerOrderNotFoundError:
        check("unknown order is reported as not found", True)
    balances = await adapter.get_balances(account)
    check("balances readable (virtual testnet funds)", bool(balances), f"{len(balances)} assets")
    return steps


async def check_with_keys(api_key: str, private_key_pem: str) -> list[Step]:
    """Build the testnet adapter from an Ed25519 key and run the contract (HTTP stays here)."""
    import httpx

    from cosmic.core.clock import SystemClock
    from cosmic.trading.brokers.binance_spot import Ed25519Signer

    async with httpx.AsyncClient(timeout=10) as client:
        adapter = BinanceSpotAdapter(
            client,
            api_key=api_key,
            signer=Ed25519Signer(private_key_pem.encode()),
            symbols={INSTRUMENT: SYMBOL},
            clock=SystemClock(),
        )
        return await run_contract(adapter)
