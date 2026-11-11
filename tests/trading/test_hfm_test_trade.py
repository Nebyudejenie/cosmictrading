"""A test trade on the HFM demo through the owner's own approval path (S57).

Proposed from the home page, sent only once the owner approves it, its stop-loss held by HFM,
closed by an exit the owner approves, and the ledger equal to HFM's balance to the cent after.
Refused, on the page and by the action itself, whenever HFM's report is not safe to trade on."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.core.labels import DataLabel
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders, trade_intents
from cosmic.db.schema_trading import bridge_state
from cosmic.market import data as md
from cosmic.reporting import hfm_home
from cosmic.trading import hfm_test_trade
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.brokers.base import BrokerUnavailableError
from cosmic.trading.brokers.mt5_bridge import HfmBridgeAdapter, hfm_symbols
from cosmic.trading.proposals import latest_packet
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.mt5_emulator import ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL, MT5Emulator
from tests.trading.test_hfm import WEDNESDAY, XAU, Hfm, bridge_running, build, open_account, paxg


async def _snapshot_until(h: Hfm, ok: Callable[[dict[str, Any]], bool]) -> None:
    """Until the bridge's report (sent every 20 ms here, every second on the VM) shows it."""
    for _ in range(1000):
        async with h.env.db.connect() as conn:
            row = (await conn.execute(select(bridge_state))).first()
        if row is not None and row.received_at == h.env.clock.now() and ok(row.snapshot):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the bridge never reported it")


async def _weekday(h: Hfm) -> None:
    """Wednesday, 10:00 in New York: gold trades; fresh prices and a passed check."""
    h.env.clock.set(WEDNESDAY)
    h.emu.now_ms = int(WEDNESDAY.timestamp() * 1000)
    await paxg(h.env.db, h.env.clock, "4100.10", "4100.40")
    await _snapshot_until(h, lambda s: True)
    await _status_fresh(h)
    assert await h.reconcile() == ()


async def _status_fresh(h: Hfm) -> None:
    """Until HFM's status is stored at the clock's time too: a sync stores it last."""
    for _ in range(1000):
        async with h.env.db.connect() as conn:
            _, label = await md.latest_venue_status(conn, "HFM", h.env.clock.now())
        if label is DataLabel.DELAYED:  # within its freshness limit
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"HFM's status never became fresh ({label})")


async def _refusal(h: Hfm) -> str | None:
    async with h.env.db.connect() as conn:
        return await hfm_test_trade.refusal(conn, h.env.clock.now())


async def _intents(db: Database) -> list[Any]:
    async with db.connect() as conn:
        return list(await conn.execute(select(trade_intents).order_by(trade_intents.c.created_at)))


async def _cycles(h: Hfm, n: int = 4) -> None:
    for _ in range(n):
        await h.env.svc.execution.run_cycle()


async def test_a_test_trade_goes_the_owners_way(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        assert await _refusal(h) is None
        sent = len(emu.sent)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/")).text
            assert "Test one demo trade" in page
            r = await c.post("/hfm/test-trade", data={"csrf_token": csrf_from(page)})
            assert r.status_code == 303 and r.headers["location"].startswith("/approvals/")
            (entry,) = await _intents(db)
            # Proposed, not sent: nothing reaches HFM before the owner approves it with a code.
            assert entry.status == "PROPOSED" and len(emu.sent) == sent
            assert entry.quantity == D(1)  # 0.01 lot = 1 oz, the smallest gold trade
            assert D("4100.30") <= entry.limit_price <= D("4100.30") * D("1.001")
            async with db.connect() as conn:
                packet = (await latest_packet(conn, entry.id)).packet
            stop = D(str(packet["management_plan"]["stop_loss"]["stop_price"]))
            assert (
                entry.limit_price * D("0.97") - D("0.01") <= stop <= entry.limit_price * D("0.97")
            )
            assert packet["account"]["environment"] == "DEMO"
            assert entry.plain_summary.startswith(
                "TEST on your HFM demo (virtual money): buy 0.01 lot = 1 oz of gold"
            )  # as people write it, never 1.000000000000000000
            # Filled at once or not at all, and decided within 15 minutes (review, 2026-11-04).
            assert entry.time_in_force == "IOC"
            assert entry.intent_expires_at - entry.created_at == timedelta(minutes=15)
            # Pressed again: one test at a time, refused by the action itself.
            again = await c.post("/hfm/test-trade", data={"csrf_token": csrf_from(page)})
            assert again.status_code == 422 and "waits for a decision" in again.text
            assert len(await _intents(db)) == 1

            await h.env.approve(entry.id)
            await _cycles(h)
            (pos,) = emu.positions.values()
            assert (pos.symbol, pos.volume, pos.sl) == ("XAUUSD", D("0.01"), stop)
            await _snapshot_until(h, lambda s: bool(s.get("positions")))
            async with db.connect() as conn:
                home = await hfm_home.build(conn, h.env.clock.now())
            assert home.gold_held and home.test_trade == "a trade is open on HFM: close it first"
            page = (await c.get("/")).text
            assert "Close the gold trade" in page and "Test one demo trade" not in page

            r = await c.post(
                "/portfolio/hfm-demo/exit",
                data={
                    "csrf_token": csrf_from(page),
                    "instrument_key": XAU,
                    "reason": "closed from the home page",
                },
            )
            assert r.status_code == 303 and r.headers["location"].startswith("/approvals/")
            exit_ = (await _intents(db))[-1]
            assert exit_.side == "SELL" and exit_.status == "PROPOSED"
            # While the close waits, it is said, and not offered a second time.
            page = (await c.get("/")).text
            assert "A sale on your HFM account waits for your approval" in page
            assert "Close the gold trade" not in page
        await h.env.approve(exit_.id)
        await _cycles(h)
        assert not emu.positions
        await _snapshot_until(h, lambda s: not s.get("positions"))
        # The ledger is HFM's balance to the cent: the spread, profit and costs as HFM booked.
        assert await h.reconcile() == ()
        assert await h.book("CASH", "USD") + await h.book("UNMANAGED", "USD") == emu.balance
        assert emu.balance != D(100_000)  # the round trip cost or made something
        assert await _refusal(h) is None  # and it can be tested again


async def test_no_test_trade_unless_hfm_is_safe_to_trade(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _snapshot_until(h, lambda s: True)
        assert "weekend" in (await _refusal(h) or "")  # Saturday: gold is closed
        await _weekday(h)
        for change, undo, said in (
            (
                lambda: setattr(emu, "algo_trading", False),
                lambda: setattr(emu, "algo_trading", True),
                "Algo Trading is off in MT5",
            ),
            (
                lambda: setattr(emu, "number", 87654321),
                lambda: setattr(emu, "number", 12345678),
                mt5_bridge.OTHER_ACCOUNT,
            ),
            (
                lambda: setattr(emu, "trade_mode", ACCOUNT_TRADE_MODE_REAL),
                lambda: setattr(emu, "trade_mode", ACCOUNT_TRADE_MODE_DEMO),
                "the MT5 terminal is not on a demo account",
            ),
            (
                lambda: setattr(emu, "currency", "USC"),
                lambda: setattr(emu, "currency", "USD"),
                f"{mt5_bridge.MONEY_NOT_HANDLED} USC",
            ),
        ):
            before = _seen(await _snapshot_of(h))
            change()
            await _snapshot_until(h, lambda s, b=before: _seen(s) != b and _algo(s, emu))
            assert (await _refusal(h) or "").startswith(said), said
            undo()
            await _snapshot_until(h, lambda s, b=before: _seen(s) == b)
            assert await _refusal(h) is None
    h.env.clock.advance(minutes=1)  # the bridge has stopped reporting
    assert await _refusal(h) == "the HFM bridge last reported 60 s ago"
    app = create_app(settings, db=db, clock=h.env.clock)
    async with client_for(app) as c:
        await full_login(c, h.env.owner, h.env.clock)
        page = (await c.get("/")).text
        assert "A test demo trade is not possible now: the HFM bridge last reported" in page
        r = await c.post("/hfm/test-trade", data={"csrf_token": csrf_from(page)})
        assert r.status_code == 422 and "last reported" in r.text
    assert await _intents(db) == []


async def _snapshot_of(h: Hfm) -> dict[str, Any]:
    async with h.env.db.connect() as conn:
        snap: dict[str, Any] = await conn.scalar(select(bridge_state.c.snapshot))
    return snap


def _algo(s: dict[str, Any], emu: MT5Emulator) -> bool:
    return bool(s["terminal"]["trade_allowed"]) == emu.algo_trading


def _seen(s: dict[str, Any]) -> tuple[Any, Any]:
    """What the bridge reports of the account and the terminal."""
    return s["account"], s["terminal"]


FRIDAY = datetime(2026, 10, 16, 20, 15, tzinfo=UTC)  # 16:15 in New York: gold closes at 17:00


async def test_a_buy_the_price_has_left_is_not_left_resting_at_hfm(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """Review, 2026-11-04: sent GTC with the ask above its limit, the buy would have rested at
    HFM as a buy limit, and could fill hours later. Sent IOC, nothing is bought or left."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        async with db.transaction() as conn:
            r = await hfm_test_trade.propose(
                conn, actor=Actor.user(h.env.owner.user_id), now=h.env.clock.now()
            )
        assert r.status == "PROPOSED", r.problems
        (entry,) = await _intents(db)
        # The ask moves just past the limit while the middle price stays inside the band.
        emu.book["XAUUSD"] = (entry.limit_price - D("0.40"), entry.limit_price + D("0.40"))
        await _snapshot_until(h, lambda s: D(str(s["ticks"]["XAUUSD"]["ask"])) > entry.limit_price)
        await h.env.approve(r.intent_id)
        await _cycles(h)
        assert not emu.positions and not emu.orders  # nothing bought, nothing resting
        async with db.connect() as conn:
            status = await conn.scalar(
                select(orders.c.status).where(
                    orders.c.intent_id == r.intent_id, orders.c.purpose == "ENTRY"
                )
            )
        assert status == "EXPIRED"
        assert await _refusal(h) is None  # and it can be tried again


async def test_no_test_trade_within_the_hour_before_gold_closes(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """Review, 2026-11-04: at least 45 minutes to close the test after approving it."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        h.env.clock.set(FRIDAY)
        emu.now_ms = int(FRIDAY.timestamp() * 1000)
        await _snapshot_until(h, lambda s: True)
        assert await h.reconcile() == ()
        assert (await _refusal(h) or "").endswith("or closes within the hour")
        h.env.clock.set(FRIDAY - timedelta(minutes=30))  # 15:45: an hour and a quarter left
        emu.now_ms = int(h.env.clock.now().timestamp() * 1000)
        await _snapshot_until(h, lambda s: True)
        assert await _refusal(h) is None


async def test_a_slow_report_is_not_an_outage(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """On the server, 18 of 2,016 checks in a week found the bridge's report 15-17 s old, each a
    failed check and a five-minute warning. A read now waits a little for the next report; an
    older report is still refused at once."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
    adapter = HfmBridgeAdapter(db, h.env.clock, hfm_symbols(), poll_seconds=0.02, slow_wait=2.0)

    async def next_report() -> None:
        await asyncio.sleep(0.2)
        async with db.transaction() as conn:
            await conn.execute(update(bridge_state).values(received_at=h.env.clock.now()))

    h.env.clock.advance(seconds=20)  # 20 s late: slow, then the next report arrives
    task = asyncio.create_task(next_report())
    started = time.monotonic()
    assert (await adapter.report())["account"]["trade_mode"] == "DEMO"
    assert 0.15 < time.monotonic() - started < 2.0
    await task
    h.env.clock.advance(seconds=20)  # 20 s late, and no report follows: silent after the wait
    adapter.slow_wait = 0.3
    started = time.monotonic()
    with pytest.raises(BrokerUnavailableError, match="last reported 20 s ago"):
        await adapter.report()
    assert time.monotonic() - started >= 0.3
    h.env.clock.advance(seconds=40)  # a minute late: an outage, refused at once
    started = time.monotonic()
    with pytest.raises(BrokerUnavailableError, match="last reported 60 s ago"):
        await adapter.report()
    assert time.monotonic() - started < 0.2
