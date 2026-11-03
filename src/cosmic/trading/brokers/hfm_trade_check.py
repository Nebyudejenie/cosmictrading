"""A real trade cycle on the HFM DEMO account through the bridge (S56): the evidence that orders
reach HFM, that HFM holds the stop-loss, and that our records agree with HFM's own history.

Run in the worker (``cosmic broker hfm-trade-check``) while gold trades. On the DEMO account
only (the adapter refuses any other), with the smallest gold volume and virtual money:

1. HFM rejects a buy below its minimum volume, and nothing is bought;
2. a market buy carrying its stop-loss fills, and HFM holds the stop on the position;
3. sending the same order again buys nothing more;
4. the fill is read back from HFM's deals;
5. a sell naming the trade closes it;
6. HFM's balance moved by exactly what its deals booked (profit, commission, swap, fees), and
   nothing is left open.

Whatever fails, it closes what it bought. It prints step results only; the account is named by
the bridge's fingerprint, never its number. The cost is the demo spread on one lot step.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerUnavailableError,
    SubmitRequest,
)
from cosmic.trading.brokers.binance_check import Step
from cosmic.trading.brokers.mt5_bridge import HfmBridgeAdapter, magic

INSTRUMENT = "CFD:XAU/USD@HFM"
SYMBOL = "XAUUSD"
CONTRACT_VERSION = "hfm-mt5-trade-1"
ACCOUNT = uuid.UUID(int=0)
SETTLE = 20.0  # seconds for HFM's answer to show in the bridge's report (it reports each second)
CENT = Decimal("0.01")


def _q(price: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (price / step).to_integral_value(rounding) * step


async def _until(
    adapter: HfmBridgeAdapter, ok: Callable[[dict[str, Any]], bool], seconds: float = SETTLE
) -> dict[str, Any]:
    """The bridge's report once ``ok`` holds for it (or the last one, after ``seconds``)."""
    deadline = time.monotonic() + seconds
    while True:
        report = await adapter.report()
        if ok(report) or time.monotonic() > deadline:
            return report
        await asyncio.sleep(0.5)


def _held(report: dict[str, Any], m: int) -> list[dict[str, Any]]:
    return [p for p in report.get("positions") or [] if p.get("magic") == m]


def _booked(report: dict[str, Any], magics: set[int]) -> Decimal:
    total = Decimal(0)
    for d in report.get("deals") or []:
        if d.get("magic") in magics:
            for k in ("profit", "commission", "swap", "fee"):
                total += Decimal(str(d.get(k) or 0))
    return total


async def run_trade_check(adapter: HfmBridgeAdapter) -> list[Step]:
    """Each step as PASS/FAIL; a silent or unavailable bridge ends the run as a failed step."""
    steps: list[Step] = []
    try:
        await _run(adapter, steps)
    except (BrokerUnavailableError, BrokerAmbiguousError) as exc:
        steps.append(Step("the bridge answered", False, str(exc)))
    return steps


async def _run(adapter: HfmBridgeAdapter, steps: list[Step]) -> None:
    def check(name: str, ok: bool, detail: str = "") -> bool:
        steps.append(Step(name, ok, detail))
        return ok

    status = await adapter.venue_status()
    why = status if status != "UNKNOWN" else (await adapter.unavailable_reason() or status)
    if not check("the bridge reports a connected demo terminal", status == "OPERATIONAL", why):
        return
    report = await adapter.report()
    spec, tick = report["symbols"][SYMBOL], report["ticks"][SYMBOL]
    step = Decimal(str(spec["tick_size"]))
    contract = Decimal(str(spec["contract_size"]))
    smallest = Decimal(str(spec["volume_min"]))
    bid, ask = Decimal(str(tick["bid"])), Decimal(str(tick["ask"]))
    before = Decimal(str(report["account"]["balance"]))
    stamp = int(time.time())

    # 1. HFM rejects what it does not allow, and nothing is bought.
    tiny = SubmitRequest(
        f"ct-hfmrej-{stamp}-E",
        INSTRUMENT,
        "BUY",
        "LIMIT",
        smallest * contract / 2,
        _q(ask * Decimal("1.002"), step, ROUND_UP),
        None,
        "IOC",
        None,
        attached_stop=_q(bid * Decimal("0.97"), step, ROUND_DOWN),
    )
    refused = await adapter.submit_order(ACCOUNT, tiny)
    report = await adapter.report()
    check(
        "HFM rejects a buy below its minimum volume, and nothing is bought",
        not refused.accepted and not _held(report, magic(tiny.client_order_id)),
        refused.reject_reason or "accepted",
    )
    if refused.accepted:  # never leave it: close it below
        await _close(adapter, tiny, check)

    # 2-4. A market buy of the smallest volume carrying its stop-loss.
    stop = _q(bid * Decimal("0.97"), step, ROUND_DOWN)
    buy = SubmitRequest(
        f"ct-hfmtrd-{stamp}-E",
        INSTRUMENT,
        "BUY",
        "LIMIT",
        smallest * contract,
        _q(ask * Decimal("1.002"), step, ROUND_UP),
        None,
        "IOC",
        None,
        attached_stop=stop,
    )
    m = magic(buy.client_order_id)
    first = await adapter.submit_order(ACCOUNT, buy)
    try:
        filled = first.accepted and first.raw.get("status") == "FILLED"
        detail = first.reject_reason or f"status {first.raw.get('status')}"
        if not check("a market buy of the smallest volume fills at HFM", filled, detail):
            return
        report = await _until(adapter, lambda r: bool(_held(r, m)))
        held = _held(report, m)
        sl = Decimal(str(held[0]["sl"])) if held else None
        check(
            "HFM holds the stop-loss on the position",
            len(held) == 1 and sl == stop,
            f"HFM reports {[p.get('sl') for p in held]}, sent {stop}",
        )
        again = await adapter.submit_order(ACCOUNT, buy)
        report = await adapter.report()
        check(
            "sending the same order again buys nothing more",
            again.accepted and len(_held(report, m)) == 1,
            f"{len(_held(report, m))} position(s)",
        )
        fills = await adapter.get_fills(ACCOUNT, None)
        check(
            "the fill is read back from HFM's deals",
            any(f.client_order_id == buy.client_order_id for f in fills),
            f"{len(fills)} fill(s) in HFM's history",
        )
    finally:
        closed = await _close(adapter, buy, check) if first.accepted else True
    if not closed:
        return
    exit_magic = magic(buy.client_order_id.replace("-E", "-X1"))
    report = await _until(
        adapter,
        lambda r: (
            not _held(r, m)
            and Decimal(str(r["account"]["balance"])) != before
            and any(d.get("magic") == exit_magic for d in r.get("deals") or [])
        ),
    )
    after = Decimal(str(report["account"]["balance"]))
    booked = _booked(report, {m, exit_magic})
    check(
        "HFM's balance moved by exactly what its deals booked",
        abs(after - before - booked) < CENT,
        f"balance {before} -> {after}, deals booked {booked}",
    )
    check("nothing is left open", not _held(report, m), "")


async def _close(
    adapter: HfmBridgeAdapter, buy: SubmitRequest, check: Callable[[str, bool, str], bool]
) -> bool:
    """Close the check's position with a sell naming it; True once HFM shows it gone."""
    report = await adapter.report()
    tick = report["ticks"][SYMBOL]
    step = Decimal(str(report["symbols"][SYMBOL]["tick_size"]))
    sell = SubmitRequest(
        buy.client_order_id.replace("-E", "-X1"),
        INSTRUMENT,
        "SELL",
        "LIMIT",
        buy.quantity,
        _q(Decimal(str(tick["bid"])) * Decimal("0.998"), step, ROUND_DOWN),
        None,
        "IOC",
        None,
    )
    res = await adapter.submit_order(ACCOUNT, sell)
    m = magic(buy.client_order_id)
    report = await _until(adapter, lambda r: not _held(r, m))
    return check(
        "a sell naming the trade closes it",
        res.accepted and not _held(report, m),
        res.reject_reason or f"status {res.raw.get('status')}",
    )
