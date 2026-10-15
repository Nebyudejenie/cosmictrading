"""HFM end to end (ADR-028): bridge code on the MT5 emulator, the sync, the HFM adapter, the
execution engine and reconciliation — contract check, account opening, buy, stop on the
position, owner exit, stop-out with swap."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from bridge.cosmic_mt5_bridge import Bridge, Config, Journal
from sqlalchemy import func, select

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_execution import fills, orders
from cosmic.db.schema_market import fetch_runs, instruments, quotes, sources
from cosmic.db.schema_trading import bridge_commands, bridge_state, positions
from cosmic.market import data as md
from cosmic.sources.ingest import ensure_registry
from cosmic.trading import authority, ledger
from cosmic.trading.accounts import get_account
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.trading.brokers.hfm_check import CONTRACT_VERSION, run_contract
from cosmic.trading.brokers.mt5_bridge import ADAPTER, HfmBridgeAdapter, hfm_symbols
from cosmic.trading.exits import propose_close
from cosmic.trading.jobs import open_sandbox
from cosmic.trading.modes import Mode
from cosmic.trading.policy import active_policy, change_policy, ensure_default_policy
from cosmic.trading.proposals import latest_packet
from cosmic.trading.sandbox import record_contract_run
from cosmic.trading.services import TradingServices
from cosmic.worker.context import WorkerContext
from tests.helpers import make_user, principal_for
from tests.mt5_emulator import MT5Emulator
from tests.trading_env import CAPTURE, Env

XAU = "CFD:XAU/USD@HFM"


@dataclass
class Hfm:
    env: Env
    emu: MT5Emulator
    adapter: HfmBridgeAdapter
    ctx: WorkerContext

    async def book(self, book: str, asset: str) -> D:
        async with self.env.db.connect() as conn:
            return await ledger.book_balance(conn, self.env.account_id, book, asset)

    async def reconcile(self) -> tuple[str, ...]:
        return (
            await self.env.svc.reconciler.reconcile(self.env.account_id, trigger="t")
        ).mismatches


async def paxg(db: Database, clock: FrozenClock, bid: str, ask: str) -> None:
    """A fresh PAXG/USDT quote: the reference that checks HFM's gold price."""
    now = clock.now()
    async with db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key == "SPOT:PAXG/USDT@BINANCE")
        )
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == "binance_public"))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run, source_id=src, kind="t", started_at=now, finished_at=now, status="SUCCESS"
            )
        )
        await conn.execute(
            quotes.insert().values(
                id=uuid7(),
                instrument_id=inst,
                source_id=src,
                bid=D(bid),
                ask=D(ask),
                last=D(bid),
                source_ts=now,
                ingested_at=now,
                fetch_run_id=run,
            )
        )


@contextlib.asynccontextmanager
async def bridge_running(
    db: Database, clock: FrozenClock, emu: MT5Emulator, tmp: Path
) -> AsyncIterator[Bridge]:
    """The Windows bridge's loop, in process: snapshot → sync → run commands, every 20 ms."""
    cfg = Config("https://x.invalid", "hfm-demo", "terminal64.exe", ("XAUUSD", "#BTCUSD"), "t")
    bridge = Bridge(
        emu, cfg, lambda p: {}, Journal(tmp / "journal.jsonl"), clock=lambda: clock.now()
    )

    async def loop() -> None:
        while True:
            payload = bridge.snapshot()
            payload["results"] = list(bridge.journal.unsent)
            sync = BridgeSync.model_validate(json.loads(json.dumps(payload)))
            commands = await apply_sync(db, clock, sync)
            bridge.journal.unsent = []
            for c in commands:
                bridge.journal.record(bridge.run_command(c))
            await asyncio.sleep(0.02)

    task = asyncio.create_task(loop())
    try:
        for _ in range(250):  # until the first report has arrived, as it has in real use
            async with db.connect() as conn:
                if (await conn.execute(select(bridge_state.c.bridge))).first() is not None:
                    break
            await asyncio.sleep(0.02)
        yield bridge
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def build(settings: Settings, db: Database, emu: MT5Emulator) -> Hfm:
    clock = FrozenClock(CAPTURE)
    emu.now_ms = int(CAPTURE.timestamp() * 1000)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    owner = await make_user(db, clock)
    adapter = HfmBridgeAdapter(
        db, clock, hfm_symbols(), wait=timedelta(seconds=10), poll_seconds=0.02
    )
    svc = TradingServices.build(db, clock, settings, hfm=adapter)
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="test")
    ctx.services["trading"] = svc
    p = principal_for(owner, clock)
    async with db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=p,
            target=Mode.PAPER_TRADING,
            reason="HFM demo for tests",
            now=clock.now(),
            settings=settings,
        )
        _, policy = await active_policy(conn)
        params = policy.model_dump()
        params.update(
            allowed_instruments=(XAU,),
            allowed_venues=("HFM",),
            allowed_quote_currencies=("USD",),
            max_order_notional=D(10_000),
            max_position_notional=D(20_000),
            max_gross_exposure=D(40_000),
        )
        await change_policy(
            conn,
            principal=p,
            new_params=params,
            reason="allow HFM gold for tests",
            now=clock.now(),
            settings=settings,
        )
    await paxg(db, clock, "4100.10", "4100.40")
    env = Env(settings, db, clock, svc, owner, uuid7())
    return Hfm(env, emu, adapter, ctx)


async def open_account(h: Hfm, capital: str = "20000") -> None:
    steps = await run_contract(h.adapter)
    assert [s.name for s in steps if not s.passed] == [], steps
    async with h.env.db.transaction() as conn:
        await record_contract_run(
            conn,
            steps=steps,
            key_fingerprint=await h.adapter.account_fingerprint(),
            code_version=CONTRACT_VERSION,
            started_at=h.env.clock.now(),
            finished_at=h.env.clock.now(),
            actor=Actor.system("test"),
            adapter=ADAPTER,
        )
    account_id = await open_sandbox(
        h.ctx,
        {
            "alias": "hfm-demo",
            "managed_capital": capital,
            "user_id": str(h.env.owner.user_id),
            "adapter": ADAPTER,
        },
    )
    assert account_id is not None
    h.env.account_id = account_id


async def buy_gold(h: Hfm) -> Any:
    r = await h.env.propose(
        account_alias="hfm-demo",
        instrument_key=XAU,
        quantity=D("1"),
        limit_price=D("4101.00"),
        price_band_low=D("4090.00"),
        price_band_high=D("4110.00"),
        management_plan={"stop_loss": {"stop_price": D("4000.00"), "limit_price": D("3990.00")}},
        entry_conditions="Buy 1 oz at or below 4,101.",
    )
    assert r.status == "PROPOSED", r.problems
    await h.env.approve(r.intent_id)
    for _ in range(3):  # entry sent and filled; fill imported; stop set on the position
        await h.env.svc.execution.run_cycle()
    return r


async def test_contract_check_and_opening(settings: Settings, db: Database, tmp_path: Path) -> None:
    emu = MT5Emulator(server_offset_ms=3 * 3_600_000)  # HFM's clock: UTC+3
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        async with db.connect() as conn:
            acct = await get_account(conn, account_id=h.env.account_id)
        assert (acct.kind, acct.venue_key, acct.broker_adapter) == ("SANDBOX", "HFM", ADAPTER)
        assert acct.reconciliation_status == "OK"
        assert await h.book("CASH", "USD") == D("20000")
        assert await h.book("UNMANAGED", "USD") == D("80000")
        assert not emu.orders  # the check's resting order was cancelled
        async with db.connect() as conn:
            cancels = await conn.scalar(
                select(func.count())
                .select_from(bridge_commands)
                .where(bridge_commands.c.kind == "CANCEL")
            )
        assert cancels == 1  # once confirmed, the clean-up does not cancel again


async def test_buy_stop_and_exit_through_the_bridge(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        entry = await buy_gold(h)
        async with db.connect() as conn:
            packet = (await latest_packet(conn, entry.intent_id)).packet
            rows = {
                o.purpose: o
                for o in await conn.execute(
                    select(orders).where(orders.c.intent_id == entry.intent_id)
                )
            }
            held = await conn.scalar(
                select(positions.c.quantity).where(positions.c.account_id == h.env.account_id)
            )
        # What the owner approved describes HFM as it is: a demo account, a CFD in the broker's
        # hours with an overnight swap, and a broker stop that fills at the market price.
        assert packet["account"]["environment"] == "DEMO"
        assert packet["plain_language"]["account"] == (
            "hfm-demo (DEMO — HFM MetaTrader 5 demo account, virtual funds)"
        )
        assert packet["instrument"]["asset_class"] == "CFD"
        assert "weekends" in packet["instrument"]["session"]
        assert "long -78.56, short 0.0 per lot" in packet["costs"]["funding_borrow"]
        assert packet["management_plan"]["stop_loss"]["type"].startswith("broker stop-loss")
        assert "gap" in packet["risk"]["assumptions"]
        assert rows["ENTRY"].status == "FILLED" and rows["ENTRY"].is_simulated
        assert held == D("1")
        (pos,) = emu.positions.values()
        assert pos.sl == D("4000.00") and rows["STOP_LOSS"].status in ("ACKNOWLEDGED", "SUBMITTED")
        assert await h.book("CASH", "USD") == D("20000") - D("4100.30") - D("0.03")
        assert await h.reconcile() == ()
        # The owner closes it: the stop is removed first, then the long is sold (IOC).
        async with db.transaction() as conn:
            acct = await get_account(conn, account_id=h.env.account_id)
            inst = await md.instrument_by_key(conn, XAU)
            exit_ = await propose_close(
                conn,
                account=acct,
                inst=inst,
                reason="close the HFM test position",
                actor=Actor.user(h.env.owner.user_id),
                now=h.env.clock.now(),
            )
        assert exit_.status == "PROPOSED", exit_.problems
        await h.env.approve(exit_.intent_id)
        for _ in range(4):
            await h.env.svc.execution.run_cycle()
        assert not emu.positions and all(
            s.get("type") != emu.ORDER_TYPE_SELL or "position" in s for s in emu.sent
        )
        async with db.connect() as conn:
            held = await conn.scalar(
                select(positions.c.quantity).where(positions.c.account_id == h.env.account_id)
            )
        assert held == 0
        assert await h.reconcile() == ()  # cash and holdings match the broker exactly


async def test_a_stop_out_with_swap_is_booked_and_reconciled(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        entry = await buy_gold(h)
        emu.rollover("XAUUSD", "-78.56")  # one night of swap on 0.01 lot
        emu.move("XAUUSD", "3999.00", "3999.30")  # the stop on the position is touched
        for _ in range(3):
            await h.env.svc.execution.run_cycle()
        async with db.connect() as conn:
            stop = (
                await conn.execute(
                    select(orders).where(
                        orders.c.intent_id == entry.intent_id, orders.c.purpose == "STOP_LOSS"
                    )
                )
            ).one()
            sold = (await conn.execute(select(fills).where(fills.c.order_id == stop.id))).one()
        assert stop.status == "FILLED" and sold.quantity == D("1")
        assert sold.fee == D("0.03") + D("0.7856")  # commission and the night's swap
        assert await h.reconcile() == ()


@pytest.mark.parametrize("mode", ["REAL"])
async def test_a_real_account_is_never_traded(
    settings: Settings, db: Database, tmp_path: Path, mode: str
) -> None:
    from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL

    emu = MT5Emulator(trade_mode=ACCOUNT_TRADE_MODE_REAL)
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        steps = await run_contract(h.adapter)
    assert steps and not steps[-1].passed and not emu.sent


async def test_the_check_never_leaves_its_order_resting(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    from cosmic.trading.brokers.base import BrokerUnavailableError

    emu = MT5Emulator()
    h = await build(settings, db, emu)

    async def broken(*_a: object, **_k: object) -> list[object]:
        raise BrokerUnavailableError("listing failed (test)")

    h.adapter.list_open_orders = broken  # type: ignore[method-assign]
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        steps = await run_contract(h.adapter)
    assert not steps[-1].passed and not emu.orders  # failed, and the order was removed
