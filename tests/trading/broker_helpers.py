"""Test helpers for Broker Connections (S83): MT5 terminals (emulators) behind their bridges, in
process, and the owner's steps to connect an account and assign the Demo Robot."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from bridge.cosmic_mt5_bridge import Bridge, Config, Journal
from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_trading import bridge_state
from cosmic.trading import connections
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.trading.brokers.mt5_bridge import BRIDGE_REAL
from tests.mt5_emulator import MT5Emulator

OWNER = Actor.system("owner-in-test")


@contextlib.asynccontextmanager
async def terminals(
    db: Database,
    clock: FrozenClock,
    settings: Settings | None,
    emus: dict[str, MT5Emulator],
    tmp: Path,
    *,
    silent: set[str] | None = None,
) -> AsyncIterator[set[str]]:
    """Each terminal's bridge loop: snapshot → sync (with the server's settings, so a sign-in
    gets its password) → commands. A terminal named in the yielded set stops reporting (MT5 or
    the bridge computer down) until removed again. The REAL terminal's bridge is read-only."""
    quiet = silent if silent is not None else set()

    async def loop(name: str, emu: MT5Emulator) -> None:
        cfg = Config(
            "https://x.invalid",
            name,
            "terminal64.exe",
            ("XAUUSD", "#BTCUSD"),
            "t",
            read_only=name == BRIDGE_REAL,
        )
        bridge = Bridge(emu, cfg, lambda p: {}, Journal(tmp / f"{name}.jsonl"), lambda: clock.now())
        while True:
            if name not in quiet:
                payload = bridge.snapshot()
                payload["results"] = list(bridge.journal.unsent)
                sync = BridgeSync.model_validate(json.loads(json.dumps(payload)))
                commands = await apply_sync(db, clock, sync, settings)
                bridge.journal.unsent = []
                for c in commands:
                    bridge.journal.record(bridge.run_command(c), keep=c["kind"] != "RATES")
            await asyncio.sleep(0.02)

    tasks = [asyncio.create_task(loop(n, e)) for n, e in emus.items()]
    try:
        for _ in range(1000):
            async with db.connect() as conn:
                seen = {r.bridge for r in await conn.execute(select(bridge_state.c.bridge))}
            if seen >= set(emus) - quiet:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("a terminal never reported")
        yield quiet
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


async def verify_now(
    db: Database, clock: FrozenClock, connection_id: uuid.UUID, *, leave_open: bool = False
) -> str:
    async with db.transaction() as conn:
        c = await connections.get(conn, connection_id)
        command = await connections.start_test(
            conn, c, actor=OWNER, now=clock.now(), leave_open=leave_open
        )
    return await connections.wait(db, command, 10)


async def connect(
    db: Database,
    clock: FrozenClock,
    settings: Settings,
    *,
    slot: str = "hfm-demo",
    login: int = 12345678,
    server: str = "HFMarketsGlobal-Demo",
    password: str = "demo-pass",  # noqa: S107 (the MT5 emulator's test password)
    declared: str = "DEMO",
    nickname: str = "HFM demo",
    execution: bool = True,
) -> uuid.UUID:
    """The owner's wizard: add, test (HFM verifies), save, and switch DEMO execution on."""
    async with db.transaction() as conn:
        cid = await connections.create(
            conn,
            settings,
            nickname=nickname,
            login=str(login),
            server=server,
            declared_type=declared,
            password=password,
            slot=slot,
            actor=OWNER,
            now=clock.now(),
        )
    status = await verify_now(db, clock, cid)
    assert status == "DONE", status
    async with db.transaction() as conn:
        c = await connections.get(conn, cid)
        assert c.verified_type == declared, (c.status, c.last_error)
        await connections.save(conn, cid, actor=OWNER, now=clock.now())
        if execution and declared == "DEMO":
            await connections.set_execution(conn, cid, True, actor=OWNER, now=clock.now())
    return cid


async def assign_demo_robot(
    db: Database, clock: FrozenClock, settings: Settings, **kw: object
) -> uuid.UUID:
    """Connect the account (the primary terminal's by default) and assign the Demo Robot."""
    cid = await connect(db, clock, settings, **kw)  # type: ignore[arg-type]
    async with db.transaction() as conn:
        await connections.assign(conn, "DEMO_ROBOT", cid, actor=OWNER, now=clock.now())
    return cid
