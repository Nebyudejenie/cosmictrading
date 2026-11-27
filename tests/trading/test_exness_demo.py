"""The portal's BUY and SELL tested on the owner's Exness demo (S119, ADR-042).

The owner's choice of 2026-11-25: their Exness demo is signed in on terminal 3, the real
account's terminal, and traded through that terminal's second bridge, bound in its own config to
that one demo (bridge 15). Here, end to end with the emulated MT5: the demo bridge reports
Exness's own gold (XAUUSD on its Pro demo) under its own venue; its broker contract passes; the demo is set up
as an account; the risk policy allows it with one change; the Manual Trading page works the PDF
plan's trade out on the demo; it is sent only after the owner approves it with their code, with
its stop-loss and take-profit held by Exness; and nothing at all is ever queued for the real
account's bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import bridge.cosmic_mt5_bridge as bridge_program
from bridge.cosmic_mt5_bridge import Bridge, Config, Journal, _fingerprint
from sqlalchemy import func, select

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import jobs
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_trading import bridge_commands, ledger_entries, ledger_txns
from cosmic.trading import exness_demo_trade, ledger
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.brokers.base import SubmitRequest
from cosmic.trading.brokers.hfm_check import CONTRACT_VERSION, gold_of, run_contract
from cosmic.trading.brokers.mt5_bridge import (
    ADAPTER_EXNESS,
    BRIDGE_EXNESS,
    BRIDGE_REAL,
    BRIDGES,
    HfmBridgeAdapter,
    symbols_of,
)
from cosmic.trading.jobs import open_sandbox
from cosmic.trading.policy import active_policy, change_policy, exness_demo_changes
from cosmic.trading.sandbox import record_contract_run
from cosmic.trading.services import TradingServices
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL, DEAL_REASON_TP, MT5Emulator
from tests.trading.test_hfm import WEDNESDAY, Hfm, build, paxg

GOLD = "CFD:XAU/USD@EXNESS"


def _exness_demo() -> MT5Emulator:
    """An Exness demo as MT5 reports it: Exness's company and server; a Pro account, whose
    gold is XAUUSD (the owner's, 2026-11-25)."""
    emu = MT5Emulator(
        server="Exness-MT5Trial9",
        company="Exness Technologies Ltd",
        balance=D(2_000),
        instant={"XAUUSD"},  # Exness Pro: instant execution (2026-11-26: "10004 requote")
    )
    # Exness's server clock is UTC (production, 2026-11-26: offset 0); its price a second old,
    # so the bridge confirms the offset from a price that has just arrived (bridge 13).
    emu.server_offset_ms = 1_000
    return emu


@contextlib.asynccontextmanager
async def _demo_bridge(h: Hfm, emu: MT5Emulator, tmp: Path) -> AsyncIterator[Bridge]:
    """Terminal 3's demo bridge, bound to its demo, in process every 20 ms (report only)."""
    cfg = Config(
        "https://x.invalid",
        BRIDGE_EXNESS,
        "terminal64.exe",
        ("XAUUSD",),
        "t",
        only_account=_fingerprint(emu.number, emu.server),
    )
    bridge = Bridge(
        emu, cfg, lambda p: {}, Journal(tmp / "exness.jsonl"), clock=lambda: h.env.clock.now()
    )

    async def loop() -> None:
        while True:
            if bridge.on_own_account():
                payload = bridge.snapshot()
                payload["results"] = bridge.journal.waiting()
                sync = BridgeSync.model_validate(json.loads(json.dumps(payload)))
                commands = await apply_sync(h.env.db, h.env.clock, sync)
                bridge.journal.delivered(payload["results"])
                for c in commands:
                    bridge.journal.record(bridge.run_command(c))
                emu.reports += 1
            await asyncio.sleep(0.02)

    task = asyncio.create_task(loop())
    try:
        yield bridge
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _reported(h: Hfm, emu: MT5Emulator, rounds: int = 3) -> None:
    start = emu.reports
    for _ in range(1000):
        if emu.reports >= start + rounds:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the demo bridge never reported")


async def _intents(db: Database) -> list[Any]:
    async with db.connect() as conn:
        return list(await conn.execute(select(trade_intents).order_by(trade_intents.c.created_at)))


async def _to_real(db: Database) -> int:
    async with db.connect() as conn:
        n = await conn.scalar(
            select(func.count())
            .select_from(bridge_commands)
            .where(bridge_commands.c.bridge == BRIDGE_REAL)
        )
    return int(n or 0)


async def _services(
    settings: Settings, db: Database
) -> tuple[Hfm, MT5Emulator, HfmBridgeAdapter, TradingServices]:
    """The trading services with the Exness demo's adapter, on a Wednesday morning in New
    York (gold trades)."""
    h = await build(settings, db, MT5Emulator())
    emu = _exness_demo()
    adapter = HfmBridgeAdapter(
        db,
        h.env.clock,
        symbols_of(BRIDGES[BRIDGE_EXNESS]),
        bridge=BRIDGE_EXNESS,
        wait=timedelta(seconds=10),
        poll_seconds=0.02,
        slow_wait=0.2,
    )
    assert gold_of(adapter) == (GOLD, "XAUUSD")  # Exness's instrument, never HFM's
    svc = TradingServices.build(
        db, h.env.clock, settings, hfm=h.adapter, hfm_more={ADAPTER_EXNESS: adapter}
    )
    h.env.svc = svc
    h.ctx.services["trading"] = svc
    h.env.clock.set(WEDNESDAY)
    emu.now_ms = h.emu.now_ms = int(WEDNESDAY.timestamp() * 1000)
    await paxg(db, h.env.clock, "4100.10", "4100.40")
    return h, emu, adapter, svc


async def _set_up(
    h: Hfm, emu: MT5Emulator, adapter: HfmBridgeAdapter, settings: Settings, db: Database
) -> uuid.UUID:
    """The demo as the owner sets it up: its broker contract, the account, the policy."""
    await _reported(h, emu)
    # 1. Its broker contract on this very demo, then the demo set up as an account.
    steps = await run_contract(adapter)
    assert [s.name for s in steps if not s.passed] == [], steps
    async with db.transaction() as conn:
        await record_contract_run(
            conn,
            steps=steps,
            key_fingerprint=await adapter.account_fingerprint(),
            code_version=CONTRACT_VERSION,
            started_at=h.env.clock.now(),
            finished_at=h.env.clock.now(),
            actor=Actor.system("test"),
            adapter=ADAPTER_EXNESS,
        )
    account_id = await open_sandbox(
        h.ctx,
        {
            "alias": "exness-demo",
            "managed_capital": "2000",
            "user_id": str(h.env.owner.user_id),
            "adapter": ADAPTER_EXNESS,
        },
    )
    assert account_id is not None
    h.env.account_id = account_id
    # 2. The policy allows it with one change, only what is missing.
    async with db.transaction() as conn:
        version, policy = await active_policy(conn)
        changes = exness_demo_changes(policy, "exness-demo")
        assert set(changes) >= {
            "allowed_venues",
            "allowed_instruments",
            "account_limits",
            "allowed_order_types",  # S123: a pending order at a level
        }
        params = policy.model_dump() | changes | {"allowed_quote_currencies": ("USD",)}
        await change_policy(
            conn,
            principal=principal_for(h.env.owner, h.env.clock),
            new_params=params,
            reason="the owner's Exness demo, tested",
            now=h.env.clock.now(),
            settings=settings,
            expected_version=version,
        )
        _, after = await active_policy(conn)
        assert exness_demo_changes(after, "exness-demo") == {}
    # A second later a new price arrives: the bridge confirms Exness's clock from it.
    emu.now_ms += 1_000
    h.env.clock.advance(seconds=1)
    await _reported(h, emu)
    async with db.connect() as conn:
        assert await exness_demo_trade.refusal(conn, h.env.clock.now()) is None
    return account_id


async def test_the_owners_exness_demo_trades_from_the_portal_and_the_real_bridge_never_does(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, emu, adapter, svc = await _services(settings, db)
    async with _demo_bridge(h, emu, tmp_path):
        account_id = await _set_up(h, emu, adapter, settings, db)
        # 3. The page works the plan's trade out on the demo; proposed, not sent.
        app = create_app(settings, db=db, clock=h.env.clock)
        sent = len(emu.sent)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/manual/demo")).text
            assert "Your Exness demo" in page and "DEMO · VIRTUAL MONEY" in page
            r = await c.post(
                "/manual/demo/trade", data={"csrf_token": csrf_from(page), "side": "SELL"}
            )
            assert r.status_code == 303, r.text
            assert r.headers["location"].startswith("/approvals/")
        (entry,) = await _intents(db)
        assert entry.status == "PROPOSED" and len(emu.sent) == sent
        assert entry.side == "SELL" and entry.time_in_force == "IOC"
        assert entry.plain_summary.startswith("DEMO (virtual money): sell ")
        # Gold rises 1 USD while the owner types their code (S122: 2026-11-27, a BUY was held
        # back by a 1 USD dip in 14 s): within 0.1 %, the trade is still sent.
        emu.move("XAUUSD", "4101.000", "4101.300")
        emu.now_ms += 15_000  # the demo's price is stored every 15 s
        h.env.clock.advance(seconds=16)
        await _reported(h, emu)
        # 4. Approved with the owner's code: sent through the demo bridge, Exness's own gold,
        # its stop-loss and take-profit held by Exness.
        await h.env.approve(entry.id)
        for _ in range(4):
            await svc.execution.run_cycle()
        (pos,) = emu.positions.values()
        assert pos.symbol == "XAUUSD" and pos.sl > pos.price_open > pos.tp > 0
        assert pos.price_open == D("4101.000")  # at the market, after the move
        # 5. Exness closes it at its take-profit, at a 3-decimal price, and credits the profit
        # in whole cents (S121: 2026-11-26, 17.789 credited as 17.79, the check failed on
        # 0.001). The check books that rounding, labelled, and the books equal Exness's.
        emu.cent_profits = True
        emu._close(pos, pos.volume, pos.price_open - D("9.703"), pos.magic, "tp", DEAL_REASON_TP)
        await _reported(h, emu)
        assert emu.balance.as_tuple().exponent >= -2  # whole cents, as Exness books them
        result = await svc.reconciler.reconcile(account_id, trigger="test")
        assert result.passed, result.mismatches
        async with db.connect() as conn:
            rounding = list(
                await conn.execute(
                    select(ledger_entries.c.amount, ledger_entries.c.book)
                    .join(ledger_txns, ledger_txns.c.id == ledger_entries.c.txn_id)
                    .where(ledger_txns.c.ref_type == "venue_cent_rounding")
                )
            )
            cash = await ledger.book_balance(conn, account_id, "CASH", "USD")
        assert sorted((r.book, r.amount) for r in rounding) == [
            ("CAPITAL", D("0.003")),
            ("CASH", D("-0.003")),
        ]
        assert cash == emu.balance  # Exness's own balance, to the cent
    assert await _to_real(db) == 0  # nothing, ever, for the real account's bridge


def test_a_demo_trades_price_range_is_a_tenth_of_a_percent_either_way_never_past_its_stop() -> None:
    tick = D("0.001")
    # A BUY: from 0.1 % under the bid up to its own limit; a SELL: its limit up to 0.1 % over the ask.
    assert exness_demo_trade.band(
        "BUY", D("3664.638"), D("3664.978"), D("3668.643"), D("3640"), tick
    ) == (D("3660.974"), D("3668.643"))
    assert exness_demo_trade.band(
        "SELL", D("3664.638"), D("3664.978"), D("3660.973"), D("3690"), tick
    ) == (D("3660.973"), D("3668.642"))
    # A near stop-loss: the range never reaches past halfway to it.
    assert exness_demo_trade.band(
        "BUY", D("3664.638"), D("3664.978"), D("3668.643"), D("3662.000"), tick
    ) == (D("3663.319"), D("3668.643"))
    assert exness_demo_trade.band(
        "SELL", D("3664.638"), D("3664.978"), D("3660.973"), D("3667.000"), tick
    ) == (D("3660.973"), D("3665.989"))


def test_a_pending_orders_kind_its_price_range_and_its_hours() -> None:
    """S123: what a pending order at a level is, the range gold may be in when it is sent, and
    that it never waits across gold's daily break."""
    bid, ask, tick = D("4100.00"), D("4100.30"), D("0.01")
    kind = exness_demo_trade.pending_kind
    assert kind("BUY", D("4105"), bid, ask) == "STOP"  # waits for gold to rise to it
    assert kind("BUY", D("4099"), bid, ask) == "LIMIT"  # to fall to it
    assert kind("BUY", D("4100.50"), bid, ask) is None  # within a spread: the market
    assert kind("SELL", D("4103"), bid, ask) == "LIMIT"
    assert kind("SELL", D("4097"), bid, ask) == "STOP"
    assert kind("SELL", D("4100.10"), bid, ask) is None
    band = exness_demo_trade.pending_band
    assert band("BUY", "STOP", D("4105"), bid, ask, D("4095"), tick) == (D("4095.90"), D("4105"))
    assert band("SELL", "STOP", D("4095"), bid, ask, D("4105"), tick) == (D("4095"), D("4104.40"))
    # A limit's level side: 0.1 % past it, never past halfway to its stop-loss.
    assert band("BUY", "LIMIT", D("4090"), bid, ask, D("4080"), tick) == (
        D("4085.91"),
        D("4104.40"),
    )
    assert band("SELL", "LIMIT", D("4110"), bid, ask, D("4112"), tick) == (
        D("4095.90"),
        D("4111.00"),
    )
    # 10:00 in New York: 4 hours stay open; 8 would cross the daily break from 17:00.
    ends = exness_demo_trade.session_ends
    assert ends(WEDNESDAY, WEDNESDAY + timedelta(hours=4)) is None
    assert "daily break" in (ends(WEDNESDAY, WEDNESDAY + timedelta(hours=8)) or "")


async def _cycles(svc: TradingServices, n: int = 4) -> None:
    for _ in range(n):
        await svc.execution.run_cycle()


async def _later(h: Hfm, emu: MT5Emulator, seconds: int) -> None:
    """Time passes for both clocks, and the demo bridge reports what MT5 holds then."""
    emu.now_ms += seconds * 1000
    h.env.clock.advance(seconds=seconds)
    await _reported(h, emu)


async def test_a_pending_order_waits_at_exness_and_opens_when_gold_reaches_its_price(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S123, the owner's request of 2026-11-27: a pending order, BUY when gold rises to the
    price they type. Worked out from the plan at that price, approved with their code, it waits
    at Exness with its stop-loss and take-profit, and opens when gold gets there."""
    h, emu, adapter, svc = await _services(settings, db)
    app = create_app(settings, db=db, clock=h.env.clock)
    async with _demo_bridge(h, emu, tmp_path), client_for(app) as c:
        await _set_up(h, emu, adapter, settings, db)
        await full_login(c, h.env.owner, h.env.clock)
        page = (await c.get("/manual/demo")).text
        assert "Pending order: opens at your price" in page
        r = await c.post(
            "/manual/demo/pending",
            data={"csrf_token": csrf_from(page), "side": "BUY", "level": "4105", "hours": "4"},
        )
        assert r.status_code == 303 and r.headers["location"].startswith("/approvals/"), r.text
        (entry,) = await _intents(db)
        assert entry.status == "PROPOSED" and entry.order_type == "STOP_LIMIT"
        assert entry.time_in_force == "GTD" and entry.limit_price == D("4105")
        assert "pending order, buy when gold rises to 4105" in entry.plain_summary
        assert entry.intent_expires_at == h.env.clock.now() + timedelta(hours=4)
        await h.env.approve(entry.id)
        await _cycles(svc)
        (o,) = emu.orders.values()  # waiting at Exness, nothing open
        assert o["type"] == emu.ORDER_TYPE_BUY_STOP and not emu.positions
        assert D(repr(o["sl"])) < D("4105") < D(repr(o["tp"]))
        page = (await c.get("/manual/demo")).text
        assert "WAITING AT EXNESS" in page and "buy when gold rises to" in page
        assert "Not now: a trade on the Exness demo waits" in page  # one at a time
        emu.move("XAUUSD", "4104.000", "4104.300")  # not there yet
        await _later(h, emu, 15)
        await _cycles(svc)
        assert not emu.positions and len(emu.orders) == 1
        emu.move("XAUUSD", "4105.000", "4105.300")  # there: Exness opens it
        await _later(h, emu, 15)
        await _cycles(svc)
        (pos,) = emu.positions.values()
        assert pos.long and pos.sl < D("4105") < pos.tp and not emu.orders
        async with db.connect() as conn:
            assert await exness_demo_trade.pending_orders(conn) == []
            status = await conn.scalar(
                select(trade_intents.c.status).where(trade_intents.c.id == entry.id)
            )
        assert status == "EXECUTED"
    assert await _to_real(db) == 0


async def test_a_pending_order_is_cancelled_by_the_owner_or_when_its_time_is_up(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: Any
) -> None:
    """S123: a SELL when gold rises to a price waits at Exness as a limit. The owner's Cancel
    removes it; else it is removed when its time is up. What makes no sense is refused before
    anything is proposed, and an older bridge is never asked to wait at a level."""
    h, emu, adapter, svc = await _services(settings, db)
    app = create_app(settings, db=db, clock=h.env.clock)
    async with _demo_bridge(h, emu, tmp_path), client_for(app) as c:
        account_id = await _set_up(h, emu, adapter, settings, db)
        await full_login(c, h.env.owner, h.env.clock)
        token = csrf_from((await c.get("/manual/demo")).text)

        async def pending(side: str, level: str, hours: str) -> Any:
            data = {"csrf_token": token, "side": side, "level": level, "hours": hours}
            return await c.post("/manual/demo/pending", data=data)

        for level, hours, why in (
            ("4100.10", "1", "within a spread of the price"),
            ("4300", "1", "more than 3 % from the price"),
            ("4103", "8", "choose a shorter time"),  # 10:00 in New York: the daily break
            ("4103", "2", "how long it waits"),
        ):
            r = await pending("SELL", level, hours)
            assert r.status_code == 422 and why in r.text, (level, r.text)
        assert await _intents(db) == []
        # 1. Placed, then cancelled by the owner.
        r = await pending("SELL", "4103", "1")
        assert r.status_code == 303, r.text
        (first,) = await _intents(db)
        assert first.order_type == "LIMIT" and "sell when gold rises to 4103" in first.plain_summary
        await h.env.approve(first.id)
        await _cycles(svc)
        (o,) = emu.orders.values()
        assert o["type"] == emu.ORDER_TYPE_SELL_LIMIT
        async with db.connect() as conn:
            (waiting,) = await exness_demo_trade.pending_orders(conn)
        assert waiting.cancellable and waiting.until == first.intent_expires_at
        r = await c.post("/manual/demo/cancel", data={"csrf_token": token, "order_id": waiting.id})
        assert r.status_code == 303 and r.headers["location"] == "/manual/demo?cancelling=1"
        async with db.connect() as conn:
            (job,) = await conn.execute(
                select(jobs.c.payload).where(jobs.c.kind == "trading.cancel_order")
            )
        assert job.payload["order_id"] == waiting.id
        await svc.execution.request_cancel(uuid.UUID(waiting.id), reason="the owner's Cancel")
        await _later(h, emu, 2)
        await _cycles(svc)
        assert not emu.orders and not emu.positions
        # 2. Placed again; its hour passes and it is cancelled at Exness.
        r = await pending("SELL", "4103", "1")
        assert r.status_code == 303, r.text
        second = (await _intents(db))[-1]
        await h.env.approve(second.id)
        await _cycles(svc)
        assert len(emu.orders) == 1
        await _later(h, emu, 3_600)
        await _cycles(svc)
        assert not emu.orders and not emu.positions
        async with db.connect() as conn:
            assert await exness_demo_trade.pending_orders(conn) == []
        # 3. An older bridge would fill an order at a level at once: never asked to.
        monkeypatch.setattr(bridge_program, "VERSION", "16")
        before = h.env.clock.now()
        await full_login(c, h.env.owner, h.env.clock)  # an hour later: signed in again
        token = csrf_from((await c.get("/manual/demo")).text)
        emu.now_ms += int((h.env.clock.now() - before).total_seconds() * 1000)  # one time
        await _later(h, emu, 2)
        r = await pending("BUY", "4105", "1")
        assert r.status_code == 422 and "older than version 17" in r.text, r.text
        sent = len(emu.sent)
        out = await adapter.submit_order(
            account_id,
            SubmitRequest(
                client_order_id="ct-T-20261014-OLD1-E",
                instrument_key=GOLD,
                side="BUY",
                order_type="STOP_LIMIT",
                quantity=D(1),
                limit_price=D("4105"),
                stop_price=D("4105"),
                time_in_force="GTD",
                expires_at=None,
            ),
        )
        assert not out.accepted and "version 17" in (out.reject_reason or "")
        assert len(emu.sent) == sent
    assert await _to_real(db) == 0


async def test_the_demo_bridge_stays_silent_on_the_real_account_and_nothing_is_offered(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """The owner signs their real account back in on terminal 3: the demo bridge reports
    nothing of it, refuses every command, and the page offers no demo trade."""
    h = await build(settings, db, MT5Emulator())
    emu = _exness_demo()
    async with _demo_bridge(h, emu, tmp_path) as bridge:
        await _reported(h, emu, 1)
        emu.trade_mode = ACCOUNT_TRADE_MODE_REAL  # the real account, signed in by hand
        before = emu.reports
        await asyncio.sleep(0.2)
        assert emu.reports == before and not bridge.on_own_account()
        h.env.clock.advance(seconds=30)
        async with db.connect() as conn:
            why = await exness_demo_trade.refusal(conn, h.env.clock.now())
        assert why is not None
    assert await _to_real(db) == 0
    assert mt5_bridge.SLOTS[BRIDGE_REAL].adapter is None  # the real account's: never an adapter
    assert BRIDGE_REAL not in BRIDGES
