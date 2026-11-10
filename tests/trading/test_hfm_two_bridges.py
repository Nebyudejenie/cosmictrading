"""Two HFM demo accounts, each with its own MT5 terminal and bridge (S74): the hourly robot's
and the daily robot's (the owner's choice, 2026-11-10). Each bridge trades only its own account;
HFM's prices come from the primary bridge only; one MT5 account never serves two bridges."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from bridge.cosmic_mt5_bridge import Bridge, Config, Journal
from pydantic import SecretStr
from sqlalchemy import func, select

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_market import quotes
from cosmic.db.schema_trading import bridge_state
from cosmic.sources.ingest import ensure_registry
from cosmic.trading import ledger
from cosmic.trading.accounts import get_account
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.trading.brokers.hfm_check import CONTRACT_VERSION, run_contract
from cosmic.trading.brokers.mt5_bridge import (
    ADAPTER,
    ADAPTER_2,
    BRIDGE,
    BRIDGE_2,
    HfmBridgeAdapter,
    hfm_symbols,
)
from cosmic.trading.jobs import open_sandbox
from cosmic.trading.sandbox import mt5_account_in_use, record_contract_run
from cosmic.trading.services import TradingServices
from cosmic.web.app import create_app
from tests.helpers import client_for
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_hfm import XAU, Hfm, build, open_account

TOKEN_1 = "bridge-token-" + "1" * 40
TOKEN_2 = "bridge-token-" + "2" * 40


def _snapshot(emu: MT5Emulator, name: str, tmp: Path) -> dict[str, Any]:
    cfg = Config("https://x.invalid", name, "terminal64.exe", ("XAUUSD", "#BTCUSD"), "t")
    bridge = Bridge(
        emu,
        cfg,
        lambda p: {},
        Journal(tmp / f"{name}.jsonl"),
        clock=lambda: datetime.fromtimestamp(emu.now_ms / 1000, tz=UTC),
    )
    return {**bridge.snapshot(), "results": []}


@contextlib.asynccontextmanager
async def bridges_running(
    db: Database, clock: FrozenClock, emus: dict[str, MT5Emulator], tmp: Path
) -> AsyncIterator[None]:
    """Each Windows bridge's loop, in process, each on its own MT5 terminal (emulator)."""

    async def loop(name: str, emu: MT5Emulator) -> None:
        cfg = Config("https://x.invalid", name, "terminal64.exe", ("XAUUSD", "#BTCUSD"), "t")
        bridge = Bridge(
            emu, cfg, lambda p: {}, Journal(tmp / f"{name}.jsonl"), clock=lambda: clock.now()
        )
        while True:
            payload = bridge.snapshot()
            payload["results"] = list(bridge.journal.unsent)
            sync = BridgeSync.model_validate(json.loads(json.dumps(payload)))
            commands = await apply_sync(db, clock, sync)
            bridge.journal.unsent = []
            for c in commands:
                bridge.journal.record(bridge.run_command(c))
            await asyncio.sleep(0.02)

    tasks = [asyncio.create_task(loop(n, e)) for n, e in emus.items()]
    try:
        for _ in range(1000):  # until every bridge has reported once
            async with db.connect() as conn:
                seen = {r.bridge for r in await conn.execute(select(bridge_state.c.bridge))}
            if seen >= set(emus):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("a bridge never reported")
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


async def with_second(settings: Settings, h: Hfm) -> HfmBridgeAdapter:
    """The worker's services with both HFM adapters, as ``trading.jobs.startup`` builds them."""
    second = HfmBridgeAdapter(
        h.env.db,
        h.env.clock,
        hfm_symbols(),
        bridge=BRIDGE_2,
        wait=timedelta(seconds=10),
        poll_seconds=0.02,
        slow_wait=0.2,
    )
    svc = TradingServices.build(
        h.env.db, h.env.clock, settings, hfm=h.adapter, hfm_more={ADAPTER_2: second}
    )
    h.env.svc = svc
    h.ctx.services["trading"] = svc
    return second


async def open_second(h: Hfm, second: HfmBridgeAdapter, capital: str = "20000") -> Any:
    steps = await run_contract(second)
    assert [s.name for s in steps if not s.passed] == [], steps
    async with h.env.db.transaction() as conn:
        await record_contract_run(
            conn,
            steps=steps,
            key_fingerprint=await second.account_fingerprint(),
            code_version=CONTRACT_VERSION,
            started_at=h.env.clock.now(),
            finished_at=h.env.clock.now(),
            actor=Actor.system("test"),
            adapter=ADAPTER_2,
        )
    return await open_sandbox(
        h.ctx,
        {
            "alias": "hfm-demo-2",
            "managed_capital": capital,
            "user_id": str(h.env.owner.user_id),
            "adapter": ADAPTER_2,
        },
    )


async def test_each_robot_trades_only_its_own_hfm_demo(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu1 = MT5Emulator()
    emu2 = MT5Emulator(login=87654321)  # the second demo account: another MT5 login
    h = await build(settings, db, emu1)
    emu2.now_ms = emu1.now_ms
    second = await with_second(settings, h)
    async with bridges_running(db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path):
        await open_account(h)
        first_id = h.env.account_id
        second_id = await open_second(h, second)
        assert second_id is not None
        async with db.connect() as conn:
            acct = await get_account(conn, account_id=second_id)
            cash = await ledger.book_balance(conn, second_id, "CASH", "USD")
            unmanaged = await ledger.book_balance(conn, second_id, "UNMANAGED", "USD")
        assert (acct.kind, acct.broker_adapter, acct.reconciliation_status) == (
            "SANDBOX",
            ADAPTER_2,
            "OK",
        )
        assert (cash, unmanaged) == (D("20000"), D("80000"))
        assert h.env.svc.brokers.for_account(acct) is second
        # A buy on the second account goes to the second terminal only.
        r = await h.env.propose(
            account_alias="hfm-demo-2",
            instrument_key=XAU,
            quantity=D("1"),
            limit_price=D("4101.00"),
            price_band_low=D("4090.00"),
            price_band_high=D("4110.00"),
            management_plan={
                "stop_loss": {"stop_price": D("4000.00"), "limit_price": D("3990.00")}
            },
            entry_conditions="Buy 1 oz at or below 4,101.",
        )
        assert r.status == "PROPOSED", r.problems
        await h.env.approve(r.intent_id)
        for _ in range(3):
            await h.env.svc.execution.run_cycle()
        assert [p.volume for p in emu2.positions.values()] == [D("0.01")]
        assert not emu1.positions
        for account_id in (first_id, second_id):
            result = await h.env.svc.reconciler.reconcile(account_id, trigger="t")
            assert result.mismatches == (), result.mismatches


async def test_one_mt5_account_never_serves_two_bridges(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu1 = MT5Emulator()
    emu2 = MT5Emulator()  # signed in to the SAME demo account as the first terminal
    h = await build(settings, db, emu1)
    emu2.now_ms = emu1.now_ms
    second = await with_second(settings, h)
    async with bridges_running(db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path):
        await open_account(h)
        fingerprint = await second.account_fingerprint()
        async with db.connect() as conn:
            assert await mt5_account_in_use(conn, fingerprint, ADAPTER_2) == "hfm-demo"
            assert await mt5_account_in_use(conn, fingerprint, ADAPTER) is None
        assert await open_second(h, second) is None
    async with db.connect() as conn:
        detail = await conn.scalar(
            select(incidents.c.detail).where(
                incidents.c.dedup_key == "trading.sandbox_not_opened:hfm-demo-2"
            )
        )
    assert detail is not None and "already hfm-demo's" in json.dumps(detail)


async def test_each_bridge_syncs_only_as_itself_and_only_the_first_feeds_prices(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu1, emu2 = MT5Emulator(), MT5Emulator(login=87654321)
    clock = FrozenClock(datetime.fromtimestamp(emu1.now_ms / 1000, tz=UTC))
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
    only_second = settings.model_copy(update={"hfm_bridge2_token": SecretStr(TOKEN_2)})
    async with client_for(create_app(only_second, db=db, clock=clock)) as c:
        auth2 = {"Authorization": f"Bearer {TOKEN_2}"}
        r = await c.post("/bridge/v1/sync", json=_snapshot(emu2, BRIDGE_2, tmp_path), headers=auth2)
        assert r.status_code == 200 and r.json()["commands"] == []
        async with db.connect() as conn:  # its snapshot is kept, but it is no price feed
            assert await conn.scalar(select(func.count()).select_from(quotes)) == 0
            stored = {r.bridge for r in await conn.execute(select(bridge_state.c.bridge))}
        assert stored == {BRIDGE_2}
        # a token is one bridge's: the second's never syncs as the first
        r = await c.post("/bridge/v1/sync", json=_snapshot(emu1, BRIDGE, tmp_path), headers=auth2)
        assert r.status_code == 403
    both = only_second.model_copy(update={"hfm_bridge_token": SecretStr(TOKEN_1)})
    async with client_for(create_app(both, db=db, clock=clock)) as c:
        auth1 = {"Authorization": f"Bearer {TOKEN_1}"}
        r = await c.post("/bridge/v1/sync", json=_snapshot(emu2, BRIDGE_2, tmp_path), headers=auth1)
        assert r.status_code == 403
        r = await c.post("/bridge/v1/sync", json=_snapshot(emu1, BRIDGE, tmp_path), headers=auth1)
        assert r.status_code == 200
        bad = {"Authorization": "Bearer nope"}
        r = await c.post("/bridge/v1/sync", json=_snapshot(emu1, BRIDGE, tmp_path), headers=bad)
        assert r.status_code == 401
    async with db.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(quotes)) > 0
