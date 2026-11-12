"""POST /bridge/v1/sync with the bridge's real snapshot (from the MT5 emulator), ADR-028; and
POST /bridge/v1/fast, the fast lane (S87), with the bridge's real request."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from bridge.cosmic_mt5_bridge import Bridge, Config, Journal
from pydantic import SecretStr
from sqlalchemy import func, select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_market import instruments, quotes, venue_status, venues
from cosmic.db.schema_trading import bridge_commands, bridge_fast, bridge_state
from cosmic.market import data as md
from cosmic.sources.ingest import ensure_registry
from cosmic.web.app import create_app
from tests.helpers import client_for
from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL, MT5Emulator

TOKEN = "bridge-token-" + "b" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _bridge(emu: MT5Emulator, tmp_path: Path) -> Bridge:
    cfg = Config("https://x.invalid", "hfm-demo", "terminal64.exe", ("XAUUSD", "#BTCUSD"), TOKEN)
    return Bridge(
        emu,
        cfg,
        lambda p: {},
        Journal(tmp_path / "j.jsonl"),
        clock=lambda: datetime.fromtimestamp(emu.now_ms / 1000, tz=UTC),
    )


def snapshot(emu: MT5Emulator, tmp_path: Path) -> dict[str, Any]:
    return {**_bridge(emu, tmp_path).snapshot(), "results": []}


def fast(emu: MT5Emulator, tmp_path: Path, hold_ms: int = 0) -> dict[str, Any]:
    """The bridge's own fast-lane request (version 8), after a report has set its clock."""
    bridge = _bridge(emu, tmp_path)
    bridge.snapshot()
    bridge.fast_offer = {"hold_ms": hold_ms}
    return bridge.fast_request()


async def _pending(db: Database, clock: FrozenClock, kind: str) -> Any:
    cid = uuid7()
    async with db.transaction() as conn:
        await conn.execute(
            bridge_commands.insert().values(
                id=cid,
                bridge="hfm-demo",
                kind=kind,
                payload={"client_order_id": "ct-T-20261112-AB12-E"} if kind == "SUBMIT" else {},
                status="PENDING",
                created_at=clock.now(),
                expires_at=clock.now() + timedelta(seconds=20),
            )
        )
    return cid


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
    # S38: the same price with the Cent account's terms, for the paper rehearsal; a mirror
    # never counts as a second, independent price in the cross-venue check.
    async with db.connect() as conn:
        cent = await md.instrument_by_key(conn, "CFD:XAU/USD@HFM_CENT")
        cq = await md.latest_quote(conn, cent.id, clock.now())
        dev = await md.cross_source_deviation(conn, "XAU/USD", clock.now())
    assert (cq.bid, cq.ask) == (q.bid, q.ask)
    assert (cent.lot_size, cent.min_qty, cent.tick_size) == (D("0.01"), D("0.01"), D("0.01"))
    assert "CFD:XAU/USD@HFM_CENT" not in {v.canonical_key for v in dev.quotes}


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


async def test_prices_are_stored_in_utc_and_not_without_an_offset(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator(server_offset_ms=3 * 3_600_000)  # the broker's clock: UTC+3
    app, clock = await setup(settings, db, emu)
    body = snapshot(emu, tmp_path)
    assert body["server_utc_offset_ms"] == 10_800_000
    async with client_for(app) as c:
        unknown = {**body, "server_utc_offset_ms": None}
        assert (await c.post("/bridge/v1/sync", json=unknown, headers=AUTH)).status_code == 200
        async with db.connect() as conn:
            assert not await conn.scalar(select(func.count()).select_from(quotes))
        assert (await c.post("/bridge/v1/sync", json=body, headers=AUTH)).status_code == 200
    async with db.connect() as conn:
        inst = await md.instrument_by_key(conn, "CFD:XAU/USD@HFM")
        q = await md.latest_quote(conn, inst.id, clock.now())
    assert q.source_ts == clock.now() and q.bid == D("4100.0")  # UTC, not the broker's clock


async def test_a_failing_cent_mirror_never_stops_hfms_own_sync(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S38 review fix: HFM's prices, status and commands come first; the mirror after, guarded."""
    from cosmic.sources.connectors import bridge as connectors

    async def broken(self: object, source_symbol: str) -> object:
        raise RuntimeError("mirror down")

    monkeypatch.setattr(connectors.CentTermsConnector, "fetch_quote", broken)
    monkeypatch.setattr(connectors.CentTermsConnector, "fetch_instrument", broken)
    emu = MT5Emulator()
    app, clock = await setup(settings, db, emu)
    async with client_for(app) as c:
        r = await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)
    assert r.status_code == 200
    async with db.connect() as conn:
        inst = await md.instrument_by_key(conn, "CFD:XAU/USD@HFM")
        q = await md.latest_quote(conn, inst.id, clock.now())
        status, _ = await md.latest_venue_status(conn, "HFM", clock.now())
    assert (q.bid, q.ask) == (D("4100.0"), D("4100.3")) and status == "OPERATIONAL"


async def test_the_fast_lane_needs_its_token_and_carries_orders_only(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S87: prices up, orders down. Nothing but an order travels on it, and an order only
    beside a fresh report of the demo account; everything else still goes with the report."""
    emu = MT5Emulator()
    async with client_for(create_app(settings, db=db)) as c:  # no token configured
        assert (await c.post("/bridge/v1/fast", json={}, headers=AUTH)).status_code == 404
    app, clock = await setup(settings, db, emu)
    async with client_for(app) as c:
        body = fast(emu, tmp_path)
        assert (await c.post("/bridge/v1/fast", json=body)).status_code == 401
        assert (await c.post("/bridge/v1/fast", json={"x": 1}, headers=AUTH)).status_code == 422
        other = {**body, "bridge": "hfm-demo-2"}  # a token is one bridge's
        assert (await c.post("/bridge/v1/fast", json=other, headers=AUTH)).status_code == 403
        order = await _pending(db, clock, "SUBMIT")
        # No report yet: the prices are kept, but no order goes out (fail closed).
        r = await c.post("/bridge/v1/fast", json=body, headers=AUTH)
        assert r.status_code == 200 and r.json()["commands"] == []
        assert isinstance(r.json()["received_ms"], int) and isinstance(r.json()["replied_ms"], int)
        async with db.connect() as conn:
            kept = (await conn.execute(select(bridge_fast))).one()
        assert kept.ticks["XAUUSD"]["bid"] == "4100.0" and kept.server_utc_offset_ms == 0
        # The report: it offers the lane, and while the lane is up it leaves the order to it.
        r = await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)
        assert r.json()["fast"] == {"path": "/bridge/v1/fast", "hold_ms": 250}
        assert r.json()["commands"] == []
        # Beside that fresh demo report: the order goes out on the lane; candles, a sign-in and
        # a ping do not (they go with the next report).
        others = [await _pending(db, clock, k) for k in ("RATES", "VERIFY", "PING")]
        r = await c.post("/bridge/v1/fast", json=body, headers=AUTH)
        assert [(x["id"], x["kind"]) for x in r.json()["commands"]] == [(str(order), "SUBMIT")]
        again = await c.post("/bridge/v1/fast", json=body, headers=AUTH)
        assert again.json()["commands"] == []  # handed out once
        # Its result comes back on the lane.
        done = {"command_id": str(order), "status": "DONE", "result": {"status": "FILLED"}}
        await c.post("/bridge/v1/fast", json={**body, "results": [done]}, headers=AUTH)
        async with db.connect() as conn:
            rows = {r.id: r for r in await conn.execute(select(bridge_commands))}
        assert rows[order].status == "DONE" and rows[order].result == {"status": "FILLED"}
        assert {rows[o].status for o in others} == {"PENDING"}
        # The lane silent for more than a second: the report carries orders again, as before
        # bridge 8, with everything else that waited.
        late = await _pending(db, clock, "SUBMIT")
        clock.advance(seconds=2)
        r = await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)
        assert {x["id"] for x in r.json()["commands"]} == {str(late), *map(str, others)}
        # A report gone stale: no order on the lane until the bridge reports again.
        stale = await _pending(db, clock, "SUBMIT")
        clock.advance(seconds=16)
        r = await c.post("/bridge/v1/fast", json=body, headers=AUTH)
        assert r.json()["commands"] == []
        async with db.connect() as conn:
            assert (
                await conn.scalar(
                    select(bridge_commands.c.status).where(bridge_commands.c.id == stale)
                )
            ) == "PENDING"


async def test_the_fast_lane_never_serves_a_non_demo_account(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator(trade_mode=ACCOUNT_TRADE_MODE_REAL)
    app, clock = await setup(settings, db, emu)
    async with client_for(app) as c:
        await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)
        await _pending(db, clock, "SUBMIT")
        r = await c.post("/bridge/v1/fast", json=fast(emu, tmp_path), headers=AUTH)
    assert r.status_code == 200 and r.json()["commands"] == []


async def test_a_held_request_answers_the_moment_an_order_waits(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S87: the bridge keeps a request open; the server answers when an order arrives, not
    when the hold ends."""
    emu = MT5Emulator()
    app, clock = await setup(settings, db, emu)
    async with client_for(app) as c:
        await c.post("/bridge/v1/sync", json=snapshot(emu, tmp_path), headers=AUTH)

        async def order_soon() -> Any:
            await asyncio.sleep(0.15)
            return await _pending(db, clock, "SUBMIT")

        started = time.monotonic()
        held, order = await asyncio.gather(
            c.post("/bridge/v1/fast", json=fast(emu, tmp_path, hold_ms=1000), headers=AUTH),
            order_soon(),
        )
        took = time.monotonic() - started
        assert [x["id"] for x in held.json()["commands"]] == [str(order)]
        assert 0.15 <= took < 0.9, took  # at the order, not at the end of the hold
        started = time.monotonic()
        idle = await c.post("/bridge/v1/fast", json=fast(emu, tmp_path, hold_ms=200), headers=AUTH)
        assert idle.json()["commands"] == [] and time.monotonic() - started >= 0.2
