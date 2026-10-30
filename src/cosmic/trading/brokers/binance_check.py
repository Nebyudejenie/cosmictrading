"""Broker-contract run against the REAL Binance Spot Test Network (B-07, ADR-025), and the
read-only check of a real-money key (ADR-034).

The contract is used by ``cosmic broker binance-testnet-check`` (on the server, keys via stdin)
and by the opt-in pytest ``tests/sandbox``. It places one tiny BUY far below the market and
cancels it. Its code version hashes the adapter's source, so a PASSED run counts only for the
code that ran it: the same code is what would trade real money.

The live check (``cosmic broker binance-live-check``) places no order: it reads the venue's
time, the gold market, the account's and the key's permissions, and the book. Both print step
results only — never keys, signatures or account identifiers.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

from cosmic.core import money
from cosmic.trading.brokers import base, binance_spot
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
LIVE_INSTRUMENT = "SPOT:PAXG/USDT@BINANCE"  # the $100 plan's market (ADR-035)
LIVE_SYMBOL = "PAXGUSDT"


# Everything that shapes what is sent to Binance and how its answers are read: the adapter, the
# contract it is checked by, the broker contract types and the number formatting.
CODE_FILES = (binance_spot.__file__, __file__, base.__file__, money.__file__)


def adapter_code_hash() -> str:
    """The first 12 hex characters of SHA-256 over those files, in that order."""
    digest = hashlib.sha256()
    for name in CODE_FILES:
        digest.update(Path(name).read_bytes())
    return digest.hexdigest()[:12]


# Identifies the steps below and the adapter code they ran on; recorded with every run (bump the
# number when the steps change; the hash follows the code by itself).
CONTRACT_VERSION = f"binance-spot-contract-1+{adapter_code_hash()}"
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


async def run_live_check(adapter: BinanceSpotAdapter) -> tuple[list[Step], dict[str, Any] | None]:
    """Read-only checks of the real venue with a real-money key: no order is placed. Returns
    the steps and the key's permissions as read (None when they could not be read), which the
    caller judges and records (``live.record_key_check``)."""
    if not adapter.live:
        raise ValueError("the live check needs the live link (BinanceSpotAdapter.live_link)")
    steps: list[Step] = []
    restrictions: dict[str, Any] | None = None

    def check(name: str, ok: bool, detail: str = "") -> bool:
        steps.append(Step(name, ok, detail))
        return ok

    try:
        status = await adapter.venue_status()
        if not check("venue reachable (ping)", status == "OPERATIONAL", status):
            return steps, None
        resp = await adapter.client.get(
            f"{adapter.base}/v3/exchangeInfo", params={"symbol": LIVE_SYMBOL}
        )
        market = resp.json()["symbols"][0] if resp.status_code == 200 else {}
        check(
            f"{LIVE_SYMBOL} is trading",
            market.get("status") == "TRADING",
            str(market.get("status")),
        )
        account = await adapter.account_permissions()
        permissions = account.get("permissions") or []
        check(
            "the account can trade spot",
            account.get("canTrade") is True
            and account.get("accountType") == "SPOT"
            and "SPOT" in permissions,
            f"canTrade={account.get('canTrade')}, accountType={account.get('accountType')}",
        )
        restrictions = await adapter.key_restrictions()
        check("the key's own permissions are readable", True)
        book = await adapter.book_ticker(LIVE_INSTRUMENT)
        check("the book is readable", book.bid > 0, f"bid {book.bid}, ask {book.ask}")
    except (BrokerUnavailableError, BrokerAmbiguousError, KeyError, IndexError, ValueError) as exc:
        steps.append(Step("venue answered", False, f"{type(exc).__name__}: {exc}"))
    return steps, restrictions


async def live_check_with_keys(
    api_key: str, private_key_pem: str
) -> tuple[list[Step], dict[str, Any] | None]:
    """Build the live link from an Ed25519 key and run the read-only live check."""
    import httpx

    from cosmic.core.clock import SystemClock
    from cosmic.trading.brokers.binance_spot import Ed25519Signer

    async with httpx.AsyncClient(timeout=10) as client:
        adapter = BinanceSpotAdapter.live_link(
            client,
            api_key=api_key,
            signer=Ed25519Signer(private_key_pem.encode()),
            symbols={LIVE_INSTRUMENT: LIVE_SYMBOL},
            clock=SystemClock(),
        )
        return await run_live_check(adapter)
