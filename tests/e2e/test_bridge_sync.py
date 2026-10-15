"""POST /bridge/v1/sync with the bridge's real snapshot (from the MT5 emulator), ADR-028."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from bridge.cosmic_mt5_bridge import Bridge, Config, Journal
from pydantic import SecretStr
from sqlalchemy import func, select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_market import instruments, quotes, venue_status, venues
from cosmic.db.schema_trading import bridge_commands, bridge_state
from cosmic.market import data as md
from cosmic.sources.ingest import ensure_registry
from cosmic.web.app import create_app
from tests.helpers import client_for
from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL, MT5Emulator

TOKEN = "bridge-token-" + "b" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def snapshot(emu: MT5Emulator, tmp_path: Path) -> dict[str, Any]:
    cfg = Config("https://x.invalid", "hfm-demo", "terminal64.exe", ("XAUUSD", "#BTCUSD"), TOKEN)
    bridge = Bridge(
        emu,
        cfg,
        lambda p: {},
        Journal(tmp_path / "j.jsonl"),
        clock=lambda: datetime.fromtimestamp(emu.now_ms / 1000, tz=UTC),
    )
    return {**bridge.snapshot(), "results": []}


async def setup(settings: Settings, db: Database, emu: MT5Emulator) -> tuple[Any, FrozenClock]:
    clock = FrozenClock(datetime.fromtimestamp(emu.now_ms / 1000, tz=UTC))
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
    app = create_app(
        settings.model_copy(update={"hfm_bridge_token": SecretStr(TOKEN)}), db=db, clock=clock
    )
    return app, clock


async def test_the_endpoint_needs_its_token(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    async with client_for(create_app(settings, db=db)) as c:  # no token configured
        assert (await c.post("/bridge/v1/sync", json={}, headers=AUTH)).status_code == 404
    app, _ = await setup(settings, db, emu)
    async with client_for(app) as c:
        body = snapshot(emu, tmp_path)
        assert (await c.post("/bridge/v1/sync", json=body)).status_code == 401
        wrong = {"Authorization": "Bearer nope"}
        assert (await c.post("/bridge/v1/sync", json=body, headers=wrong)).status_code == 401
        assert (await c.post("/bridge/v1/sync", json={"x": 1}, headers=AUTH)).status_code == 422


async def test_sync_stores_prices_status_and_rules(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    app, clock = await setup(settings, db, emu)
    async with client_for(app) as c:
        r = await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)
        assert r.status_code == 200 and r.json()["commands"] == []
        r2 = await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)
        assert r2.status_code == 200
    async with db.connect() as conn:
        inst = await md.instrument_by_key(conn, "CFD:XAU/USD@HFM")
        q = await md.latest_quote(conn, inst.id, clock.now())
        status, _ = await md.latest_venue_status(conn, "HFM", clock.now())
        n_quotes = await conn.scalar(
            select(func.count()).select_from(quotes).where(quotes.c.instrument_id == inst.id)
        )
        state = (await conn.execute(select(bridge_state))).one()
        statuses = await conn.scalar(
            select(func.count())
            .select_from(venue_status)
            .join(venues, venues.c.id == venue_status.c.venue_id)
            .where(venues.c.key == "HFM")
        )
        meta, asset_class = (
            await conn.execute(
                select(instruments.c.metadata, instruments.c.asset_class).where(
                    instruments.c.id == inst.id
                )
            )
        ).one()
    assert (q.bid, q.ask) == (D("4100.0"), D("4100.3")) and status == "OPERATIONAL"
    assert n_quotes == 1 and statuses == 1  # the second sync came too soon to be stored again
    assert (inst.lot_size, inst.tick_size, asset_class) == (D("1.00"), D("0.01"), "CFD")
    assert meta["contract_size"] == "100.0" and state.snapshot["account"]["trade_mode"] == "DEMO"


async def test_commands_go_out_once_and_results_come_back(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    app, clock = await setup(settings, db, emu)
    cid = uuid7()
    async with db.transaction() as conn:
        await conn.execute(
            bridge_commands.insert().values(
                id=cid,
                bridge="hfm-demo",
                kind="PING",
                payload={},
                status="PENDING",
                created_at=clock.now(),
                expires_at=clock.now() + timedelta(seconds=20),
            )
        )
    async with client_for(app) as c:
        first = (await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)).json()
        again = (await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)).json()
        result = {"command_id": str(cid), "status": "DONE", "result": {"demo": True}}
        body = {**snapshot(emu, tmp_path), "results": [result]}
        assert (await c.post("/bridge/v1/sync", json=body, headers=AUTH)).status_code == 200
    assert [x["id"] for x in first["commands"]] == [str(cid)] and again["commands"] == []
    async with db.connect() as conn:
        row = (await conn.execute(select(bridge_commands))).one()
    assert row.status == "DONE" and row.result == {"demo": True}


async def test_a_non_demo_account_gets_no_commands(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator(trade_mode=ACCOUNT_TRADE_MODE_REAL)
    app, clock = await setup(settings, db, emu)
    async with db.transaction() as conn:
        await conn.execute(
            bridge_commands.insert().values(
                id=uuid7(),
                bridge="hfm-demo",
                kind="PING",
                payload={},
                status="PENDING",
                created_at=clock.now(),
                expires_at=clock.now() + timedelta(seconds=20),
            )
        )
    async with client_for(app) as c:
        r = await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)
    assert r.json()["commands"] == []
    async with db.connect() as conn:
        keys = {row.dedup_key for row in await conn.execute(select(incidents.c.dedup_key))}
        status, _ = await md.latest_venue_status(conn, "HFM", clock.now())
    assert "trading.bridge_not_demo:hfm-demo" in keys and status == "DEGRADED"
