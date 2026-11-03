"""A real trade cycle on an HFM DEMO account through the bridge (S56): HFM's own behaviour,
checked before an account is opened here on that demo.

Run in the worker (``cosmic broker hfm-trade-check``) while gold trades, and only while no HFM
account is open here on that demo: its deals would be unknown to that account's books, and its
reconciliation would fail for good (found by review). It is the check for a new demo account (a
Cent demo, say) before an account is opened on it; reconciliation then leaves the check's deals,
made before the opening, alone. Evidence on an open account comes from that account's own trades.

On the DEMO account only (the adapter refuses any other), with the smallest gold volume and
virtual money:

1. HFM rejects a buy below its minimum volume (retcode 10014), and nothing is bought;
2. a market buy carrying its stop-loss fills, and HFM holds the stop on the position;
3. once HFM's history shows the buy, sending it again buys nothing more;
4. the fill is read back from HFM's deals;
5. a sell naming the trade closes it, and its fill is read back as that sell's;
6. HFM's balance moved by exactly what the check's deals booked (profit, commission, swap,
   fees), and nothing the check bought is left open.

It does not start within half an hour of gold's daily break or the weekend close, nor while an
earlier check's position is open. Whatever fails, it closes what HFM holds under the check's own
order IDs, retrying, and ends by naming anything it could not close and what to do. It prints step
results only; the account is named by the bridge's fingerprint, never its number. The cost is the
demo spread on one lot step.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from cosmic.db.schema_trading import accounts
from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerUnavailableError,
    SubmitRequest,
    VenueOrderStatus,
)
from cosmic.trading.brokers.binance_check import Step
from cosmic.trading.brokers.mt5_bridge import ADAPTER, HfmBridgeAdapter, magic

INSTRUMENT = "CFD:XAU/USD@HFM"
SYMBOL = "XAUUSD"
CONTRACT_VERSION = "hfm-mt5-trade-1"
PREFIX = "ct-hfm"  # every order ID (and so every MT5 comment) of the checks starts with it
ACCOUNT = uuid.UUID(int=0)
SETTLE = 20.0  # seconds for HFM's answer to show in the bridge's report (it reports each second)
CLOSE_TRIES = 3
MARGIN = timedelta(minutes=30)  # no start this close to gold's daily break or weekend close
NEW_YORK = ZoneInfo("America/New_York")
CENT = Decimal("0.01")
BROKER_ERRORS = (BrokerUnavailableError, BrokerAmbiguousError)


def session_closed(now: datetime) -> str | None:
    """Why gold may not trade during the next half hour (None when it will): HFM closes gold from
    Friday 17:00 to Sunday 18:00 New York and daily for about an hour from 17:00; the first hour
    after the Sunday reopening is left alone too."""
    for t in (now, now + MARGIN):
        ny = t.astimezone(NEW_YORK)
        day = ny.weekday()  # Monday 0
        if day == 5 or (day == 4 and ny.hour >= 17) or (day == 6 and ny.hour < 19):
            return "gold is closed for the weekend, reopened under an hour ago, or closes soon"
        if ny.hour == 17:
            return "gold is in HFM's daily break from 17:00 New York, or enters it soon"
    return None


def _q(price: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (price / step).to_integral_value(rounding) * step


def _held(report: dict[str, Any], m: int) -> list[dict[str, Any]]:
    return [p for p in report.get("positions") or [] if p.get("magic") == m]


def _deal(report: dict[str, Any], m: int) -> bool:
    return any(d.get("magic") == m for d in report.get("deals") or [])


def _booked(report: dict[str, Any], magics: set[int]) -> Decimal:
    total = Decimal(0)
    for d in report.get("deals") or []:
        if d.get("magic") in magics:
            for k in ("profit", "commission", "swap", "fee"):
                total += Decimal(str(d.get(k) or 0))
    return total


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


async def _accounts_open(adapter: HfmBridgeAdapter) -> list[str]:
    """The HFM accounts open here on the bridge's demo."""
    async with adapter.db.connect() as conn:
        rows = await conn.execute(
            select(accounts.c.alias).where(
                accounts.c.broker_adapter == ADAPTER,
                accounts.c.kind == "SANDBOX",
                accounts.c.status == "ACTIVE",
            )
        )
        return [r.alias for r in rows]


async def run_trade_check(adapter: HfmBridgeAdapter, now: datetime) -> list[Step]:
    """Each step as PASS/FAIL; nothing the check bought is left open without a failed step
    naming it."""
    steps: list[Step] = []
    try:
        await _run(adapter, steps, now)
    except BROKER_ERRORS as exc:
        steps.append(Step("the bridge answered", False, str(exc)))
    return steps


async def _run(adapter: HfmBridgeAdapter, steps: list[Step], now: datetime) -> None:
    def check(name: str, ok: bool, detail: str = "") -> bool:
        steps.append(Step(name, ok, detail))
        return ok

    status = await adapter.venue_status()
    why = status if status != "UNKNOWN" else (await adapter.unavailable_reason() or status)
    if not check("the bridge reports a connected demo terminal", status == "OPERATIONAL", why):
        return
    open_here = await _accounts_open(adapter)
    if not check(
        "no HFM account is open here on this demo",
        not open_here,
        f"{', '.join(open_here)} is open on it: its reconciliation would fail for good on the "
        "check's deals; that account's own trades are its evidence"
        if open_here
        else "",
    ):
        return
    closed = session_closed(now)
    if not check("gold trades for the next half hour", closed is None, closed or ""):
        return
    report = await adapter.report()
    earlier = [p for p in report.get("positions") or [] if str(p.get("comment")).startswith(PREFIX)]
    if not check(
        "no earlier check's position is open",
        not earlier,
        "close position ticket(s) "
        + ", ".join(str(p.get("ticket")) for p in earlier)
        + " in MT5 on the Windows VM first"
        if earlier
        else "",
    ):
        return
    spec = (report.get("symbols") or {}).get(SYMBOL)
    tick = (report.get("ticks") or {}).get(SYMBOL)
    if not check(
        "HFM reports gold's rules and a current price",
        bool(spec and tick and tick.get("bid") and tick.get("ask")),
        "",
    ):
        return
    assert spec is not None and tick is not None
    step = Decimal(str(spec["tick_size"]))
    lot = Decimal(str(spec["volume_min"])) * Decimal(str(spec["contract_size"]))
    bid, ask = Decimal(str(tick["bid"])), Decimal(str(tick["ask"]))
    before = Decimal(str(report["account"]["balance"]))
    stamp = int(time.time())
    stop = _q(bid * Decimal("0.97"), step, ROUND_DOWN)

    def entry(name: str, quantity: Decimal) -> SubmitRequest:
        return SubmitRequest(
            f"{PREFIX}{name}-{stamp}-E",
            INSTRUMENT,
            "BUY",
            "LIMIT",
            quantity,
            _q(ask * Decimal("1.002"), step, ROUND_UP),
            None,
            "IOC",
            None,
            attached_stop=stop,
        )

    tiny, buy = entry("rej", lot / 2), entry("trd", lot)
    sent: list[SubmitRequest] = []
    try:
        # 1. HFM rejects what it does not allow, and nothing is bought.
        sent.append(tiny)
        refused = await adapter.submit_order(ACCOUNT, tiny)
        report = await adapter.report()
        reason = refused.reject_reason or f"status {refused.raw.get('status')}"
        check(
            "HFM rejects a buy below its minimum volume (10014), and nothing is bought",
            not refused.accepted
            and reason.startswith("10014")
            and not _held(report, magic(tiny.client_order_id)),
            reason,
        )
        # 2-4. A market buy of the smallest volume carrying its stop-loss.
        sent.append(buy)
        first = await adapter.submit_order(ACCOUNT, buy)
        filled = first.accepted and first.raw.get("status") == "FILLED"
        detail = first.reject_reason or f"status {first.raw.get('status')}"
        if check("a market buy of the smallest volume fills at HFM", filled, detail):
            await _bought(adapter, check, buy, stop)
    except BROKER_ERRORS as exc:
        check("the bridge answered", False, str(exc))
    finally:
        exits = await _close_all(adapter, sent)
    await _closed(adapter, check, buy, exits.get(buy.client_order_id, []))
    await _balance(adapter, check, before, sent, exits)
    await _nothing_open(adapter, check, sent)


def _gone(m: int) -> Callable[[dict[str, Any]], bool]:
    return lambda r: not _held(r, m)


async def _bought(
    adapter: HfmBridgeAdapter,
    check: Callable[[str, bool, str], bool],
    buy: SubmitRequest,
    stop: Decimal,
) -> None:
    """Steps 2-4 once the buy filled: HFM's stop, no second buy, the fill read back."""
    m = magic(buy.client_order_id)
    report = await _until(adapter, lambda r: bool(_held(r, m)) and _deal(r, m))
    held = _held(report, m)
    sl = Decimal(str(held[0]["sl"])) if held else None
    check(
        "HFM holds the stop-loss on the position",
        len(held) == 1 and sl == stop,
        f"HFM reports {[p.get('sl') for p in held]}, sent {stop}",
    )
    if _deal(report, m):  # only once HFM's history shows the buy can a re-send find it
        again = await adapter.submit_order(ACCOUNT, buy)
        report = await adapter.report()
        check(
            "sending the same order again buys nothing more",
            again.accepted and len(_held(report, m)) == 1,
            f"{len(_held(report, m))} position(s)",
        )
    else:
        check(
            "sending the same order again buys nothing more",
            False,
            "HFM's history did not show the buy in time, so it was not sent again",
        )
    fills = await adapter.get_fills(ACCOUNT, None)
    check(
        "the fill is read back from HFM's deals",
        any(f.client_order_id == buy.client_order_id for f in fills),
        f"{len(fills)} fill(s) in HFM's history",
    )


async def _close_all(
    adapter: HfmBridgeAdapter, entries: list[SubmitRequest]
) -> dict[str, list[SubmitRequest]]:
    """Close whatever HFM holds under each entry's own order ID: each try a fresh exit ID, a wide
    limit, every broker error caught. Returns the exits sent per entry."""
    sent: dict[str, list[SubmitRequest]] = {}
    for e in entries:
        m = magic(e.client_order_id)
        exits: list[SubmitRequest] = []
        for n in range(1, CLOSE_TRIES + 1):
            try:
                report = await _until(adapter, lambda _r: True, 0)
                held = _held(report, m)
                if not held:
                    break
                spec = report["symbols"][SYMBOL]
                tick = (report.get("ticks") or {}).get(SYMBOL)
                if not tick or not tick.get("bid"):
                    await asyncio.sleep(2)
                    continue
                volume = sum((Decimal(str(p["volume"])) for p in held), Decimal(0))
                sell = SubmitRequest(
                    e.client_order_id.replace("-E", f"-X{n}"),
                    INSTRUMENT,
                    "SELL",
                    "LIMIT",
                    volume * Decimal(str(spec["contract_size"])),
                    _q(
                        Decimal(str(tick["bid"])) * Decimal("0.99"),
                        Decimal(str(spec["tick_size"])),
                        ROUND_DOWN,
                    ),
                    None,
                    "IOC",
                    None,
                )
                exits.append(sell)
                await adapter.submit_order(ACCOUNT, sell)
                await _until(adapter, _gone(m))
            except (*BROKER_ERRORS, KeyError):
                await asyncio.sleep(2)
        sent[e.client_order_id] = exits
    return sent


async def _closed(
    adapter: HfmBridgeAdapter,
    check: Callable[[str, bool, str], bool],
    buy: SubmitRequest,
    exits: list[SubmitRequest],
) -> None:
    """The buy was closed by a sell naming it, and HFM's deal is read back as that sell's."""
    if not exits:
        return  # nothing was bought, or nothing needed closing
    try:
        report = await adapter.report()
        fills = await adapter.get_fills(ACCOUNT, None)
        last = exits[-1]
        order = await adapter.get_order(ACCOUNT, last.client_order_id)
    except BROKER_ERRORS as exc:
        check("a sell naming the trade closes it", False, str(exc))
        return
    ids = {x.client_order_id for x in exits}
    sold = sum((f.quantity for f in fills if f.client_order_id in ids), Decimal(0))
    check(
        "a sell naming the trade closes it, read back as that sell's",
        not _held(report, magic(buy.client_order_id))
        and sold == buy.quantity
        and order.status is VenueOrderStatus.FILLED,
        f"sold {sold} of {buy.quantity} under {', '.join(sorted(ids))}; the last sell is "
        f"{order.status.value}",
    )


async def _balance(
    adapter: HfmBridgeAdapter,
    check: Callable[[str, bool, str], bool],
    before: Decimal,
    entries: list[SubmitRequest],
    exits: dict[str, list[SubmitRequest]],
) -> None:
    magics = {magic(e.client_order_id) for e in entries} | {
        magic(x.client_order_id) for xs in exits.values() for x in xs
    }
    exit_magics = {magic(x.client_order_id) for xs in exits.values() for x in xs}
    if not exit_magics:
        return  # nothing was bought: nothing to account for
    try:
        report = await _until(
            adapter,
            lambda r: (
                Decimal(str(r["account"]["balance"])) != before
                and any(d.get("magic") in exit_magics for d in r.get("deals") or [])
            ),
        )
    except BROKER_ERRORS as exc:
        check("HFM's balance moved by exactly what the check's deals booked", False, str(exc))
        return
    after = Decimal(str(report["account"]["balance"]))
    booked = _booked(report, magics)
    check(
        "HFM's balance moved by exactly what the check's deals booked",
        abs(after - before - booked) < CENT,
        f"balance {before} -> {after}, deals booked {booked}",
    )


async def _nothing_open(
    adapter: HfmBridgeAdapter,
    check: Callable[[str, bool, str], bool],
    entries: list[SubmitRequest],
) -> None:
    try:
        report = await adapter.report()
    except BROKER_ERRORS as exc:
        check(
            "nothing the check bought is left open",
            False,
            f"{exc}: look in MT5 on the Windows VM for positions whose comment starts with "
            f"'{PREFIX}' and close them",
        )
        return
    left = [p for e in entries for p in _held(report, magic(e.client_order_id))]
    check(
        "nothing the check bought is left open",
        not left,
        "still open at HFM: position ticket(s) "
        + ", ".join(str(p.get("ticket")) for p in left)
        + f" (comment starting '{PREFIX}'); close them in MT5 on the Windows VM"
        if left
        else "",
    )
