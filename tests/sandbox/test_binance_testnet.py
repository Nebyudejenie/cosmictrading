"""Opt-in contract run against the REAL Binance Spot Test Network (``make sandbox-binance``).

Needs the owner's testnet key (B-07), never real-money keys:
  BINANCE_TESTNET_API_KEY            the API key shown after registering the public key
  BINANCE_TESTNET_PRIVATE_KEY_FILE   path to the Ed25519 private key (PEM), or
  BINANCE_TESTNET_HMAC_SECRET_FILE   path to a file holding an HMAC secret
Keys are read from files and never printed. Places one tiny order far below the market and
cancels it. Must pass before the adapter may be registered for any account.
"""

from __future__ import annotations

import os
import time
import uuid
from decimal import ROUND_UP, Decimal
from pathlib import Path

import httpx
import pytest

from cosmic.core.clock import SystemClock
from cosmic.trading.brokers.base import BrokerOrderNotFoundError, SubmitRequest, VenueOrderStatus
from cosmic.trading.brokers.binance_spot import (
    TESTNET_BASES,
    BinanceSpotAdapter,
    Ed25519Signer,
    HmacSigner,
    Signer,
)

pytestmark = pytest.mark.sandbox
BTC = "SPOT:BTC/USDT@BINANCE"


def _signer() -> Signer | None:
    pem = os.environ.get("BINANCE_TESTNET_PRIVATE_KEY_FILE")
    if pem:
        return Ed25519Signer(Path(pem).read_bytes())
    secret = os.environ.get("BINANCE_TESTNET_HMAC_SECRET_FILE")
    if secret:
        return HmacSigner(Path(secret).read_text().strip())
    return None


@pytest.fixture
def testnet() -> BinanceSpotAdapter:
    key, signer = os.environ.get("BINANCE_TESTNET_API_KEY"), _signer()
    if not key or signer is None:
        pytest.skip("Binance Spot Test Network keys not provided (B-07)")
    return BinanceSpotAdapter(
        httpx.AsyncClient(timeout=10),
        api_key=key,
        signer=signer,
        symbols={BTC: "BTCUSDT"},
        clock=SystemClock(),
        base_url=TESTNET_BASES[0],
    )


async def test_contract_on_the_real_spot_test_network(testnet: BinanceSpotAdapter) -> None:
    account = uuid.uuid4()
    assert await testnet.venue_status() == "OPERATIONAL"
    book = (
        await testnet.client.get(
            f"{testnet.base}/v3/ticker/bookTicker", params={"symbol": "BTCUSDT"}
        )
    ).json()
    price = (Decimal(book["bidPrice"]) * Decimal("0.8")).quantize(Decimal("0.01"))
    qty = (Decimal("6") / price).quantize(Decimal("0.00001"), ROUND_UP)  # > 5 USDT notional
    coid = f"ct-sbx-{int(time.time())}"
    req = SubmitRequest(coid, BTC, "BUY", "LIMIT", qty, price, None, "GTC", None)
    first = await testnet.submit_order(account, req)
    assert first.accepted, first.reject_reason
    again = await testnet.submit_order(account, req)
    assert again.accepted and again.venue_order_id == first.venue_order_id  # idempotent
    order = await testnet.get_order(account, coid)
    assert order.status is VenueOrderStatus.OPEN and order.quantity == qty
    assert any(o.client_order_id == coid for o in await testnet.list_open_orders(account))
    await testnet.cancel_order(account, coid)
    assert (await testnet.get_order(account, coid)).status is VenueOrderStatus.CANCELLED
    with pytest.raises(BrokerOrderNotFoundError):
        await testnet.get_order(account, f"{coid}-never")
    assert await testnet.get_balances(account)  # virtual testnet balances
