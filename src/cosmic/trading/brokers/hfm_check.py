"""Broker-contract run for HFM through the MetaTrader 5 bridge (ADR-028, B-10).

Run in the worker (``cosmic broker hfm-check``) while the bridge is connected. It places one
buy limit for the smallest gold volume far below the market on the DEMO account, finds it again
(also by re-sending it), lists it, cancels it and reads the balances. It prints step results only;
the account is identified by the bridge's fingerprint, never its number.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from decimal import ROUND_DOWN, Decimal

from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
    SubmitRequest,
    SubmitResult,
    VenueOrder,
    VenueOrderStatus,
)
from cosmic.trading.brokers.binance_check import Step
from cosmic.trading.brokers.mt5_bridge import HfmBridgeAdapter

INSTRUMENT = "CFD:XAU/USD@HFM"
SYMBOL = "XAUUSD"
CONTRACT_VERSION = "hfm-mt5-contract-1"
ACCOUNT = uuid.UUID(int=0)


async def run_contract(adapter: HfmBridgeAdapter) -> list[Step]:
    """Each step as PASS/FAIL; a silent or unavailable bridge ends the run as a failed step."""
    steps: list[Step] = []
    try:
        await _run(adapter, steps)
    except (BrokerUnavailableError, BrokerAmbiguousError) as exc:
        steps.append(Step("the bridge answered", False, str(exc)))
    return steps


async def _settled(adapter: HfmBridgeAdapter, coid: str, seconds: float = 15) -> VenueOrder:
    """The order as the bridge's next report shows it (results are reported one sync later)."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return await adapter.get_order(ACCOUNT, coid)
        except BrokerUnavailableError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.5)


async def _run(adapter: HfmBridgeAdapter, steps: list[Step]) -> None:
    def check(name: str, ok: bool, detail: str = "") -> bool:
        steps.append(Step(name, ok, detail))
        return ok

    status = await adapter.venue_status()
    if not check("the bridge reports a connected demo terminal", status == "OPERATIONAL", status):
        return
    report = await adapter.report()
    spec, tick = report["symbols"][SYMBOL], report["ticks"][SYMBOL]
    step = Decimal(spec["tick_size"])
    price = (Decimal(tick["bid"]) * Decimal("0.8") / step).to_integral_value(ROUND_DOWN) * step
    qty = Decimal(spec["volume_min"]) * Decimal(spec["contract_size"])
    coid = f"ct-hfmchk-{int(time.time())}-E"
    req = SubmitRequest(coid, INSTRUMENT, "BUY", "LIMIT", qty, price, None, "GTC", None)
    first = await adapter.submit_order(ACCOUNT, req)
    cancelled = False
    try:
        cancelled = await _steps(adapter, check, req, first)
    finally:
        if first.accepted and not cancelled:  # never leave the check's order resting
            try:
                await adapter.cancel_order(ACCOUNT, coid)
            except (BrokerUnavailableError, BrokerAmbiguousError, BrokerOrderNotFoundError) as exc:
                steps.append(Step("clean up the check's order", False, str(exc)))


async def _steps(
    adapter: HfmBridgeAdapter,
    check: Callable[[str, bool, str], bool],
    req: SubmitRequest,
    first: SubmitResult,
) -> bool:
    """The steps after placing; True once the venue has confirmed the cancel."""
    coid = req.client_order_id
    placed = first.accepted and first.raw.get("status") == "PLACED"
    detail = first.reject_reason or f"status {first.raw.get('status')}"
    if not check("place a resting buy limit far below the market", placed, detail):
        return False
    again = await adapter.submit_order(ACCOUNT, req)
    check(
        "re-send with the same client order ID returns the same order",
        again.accepted and again.venue_order_id == first.venue_order_id,
        "",
    )
    order = await _settled(adapter, coid)
    check(
        "query by client order ID",
        order.status is VenueOrderStatus.OPEN and order.quantity == req.quantity,
        order.status.value,
    )
    listed = await adapter.list_open_orders(ACCOUNT)
    check("listed among open orders", any(o.client_order_id == coid for o in listed), "")
    await adapter.cancel_order(ACCOUNT, coid)
    deadline = time.monotonic() + 15
    after = await _settled(adapter, coid)
    while after.status is VenueOrderStatus.OPEN and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        after = await _settled(adapter, coid)
    cancelled = check(
        "cancel confirmed by the venue",
        after.status is VenueOrderStatus.CANCELLED,
        after.status.value,
    )
    try:
        await adapter.get_order(ACCOUNT, coid.replace("-E", "-X9"))
        check("unknown order is reported as not found", False, "")
    except BrokerOrderNotFoundError:
        check("unknown order is reported as not found", True, "")
    balances = await adapter.get_balances(ACCOUNT)
    usd = next((b.total for b in balances if b.asset == "USD"), None)
    check("balances readable (demo funds)", usd is not None and usd > 0, f"{len(balances)} assets")
    return cancelled
