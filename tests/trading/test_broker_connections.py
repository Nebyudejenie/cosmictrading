"""Admin → Broker Connections → HFM MT5 (S83): the owner connects, tests, saves, switches and
assigns HFM MT5 accounts from the portal. Each test runs the real bridge program against MT5
emulators, through the real sync endpoint logic; the ten required scenarios are named."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, incidents
from cosmic.db.schema_execution import order_events, orders
from cosmic.db.schema_trading import (
    bridge_commands,
    broker_connection_events,
    broker_connections,
    broker_credentials,
)
from cosmic.trading import connections
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_2, BRIDGE_REAL
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL, MT5Emulator
from tests.trading.broker_helpers import OWNER, connect, terminals, verify_now
from tests.trading.test_hfm import XAU, Hfm, build, open_account

pytestmark = pytest.mark.keeps_binance_paused
CENT_PASSWORD = "Cent-demo-pass-77"
REAL_INVESTOR = "Real-read-only-55"
REAL_MASTER = "Real-trading-pass-99"
LIVE = "HFMarketsGlobal-Live"  # the real server in these tests (an assumption)


def _emus(h: Hfm, emu: MT5Emulator) -> dict[str, MT5Emulator]:
    """Terminal 1 on the existing demo; terminal 2 signed out (a new portable MT5); terminal 3
    (read-only) signed out. HFM knows a second demo (100 USD) and the owner's real account."""
    two = MT5Emulator(number=55555555, signed_in=False, balance=D(100))
    two.add_account(55555555, CENT_PASSWORD, balance="100")
    two.add_account(
        66666666, REAL_MASTER, investor=REAL_INVESTOR, trade_mode=2, balance="100", server=LIVE
    )
    three = MT5Emulator(
        number=66666666, signed_in=False, trade_mode=ACCOUNT_TRADE_MODE_REAL, server=LIVE
    )
    three.known.clear()
    three.add_account(
        66666666, REAL_MASTER, investor=REAL_INVESTOR, trade_mode=2, balance="100", server=LIVE
    )
    for e in (two, three):
        e.now_ms = emu.now_ms
    return {BRIDGE: emu, BRIDGE_2: two, BRIDGE_REAL: three}


async def _texts(db: Database) -> str:
    """Everything the server stored about sign-ins: commands, events, audit, credentials."""
    async with db.connect() as conn:
        rows: list[Any] = []
        rows += list(await conn.execute(select(bridge_commands)))
        rows += list(await conn.execute(select(broker_connection_events)))
        rows += list(await conn.execute(select(audit_events.c.payload)))
        rows += list(await conn.execute(select(broker_credentials)))
        rows += list(await conn.execute(select(broker_connections)))
    return " ".join(repr(tuple(r)) for r in rows)


# ---- 1. Correct credentials · 8. correct account identity -------------------------------------


async def test_1_correct_credentials_are_verified_by_hfm_saved_and_shown_in_the_portal(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    emus = _emus(h, emu)
    app = create_app(settings, db=db, clock=h.env.clock)
    async with terminals(db, h.env.clock, settings, emus, tmp_path), client_for(app) as c:
        await full_login(c, h.env.owner, h.env.clock)
        page = await c.get("/brokers/new")
        assert page.status_code == 200 and "HFMarketsGlobal-Demo" in page.text  # discovered
        r = await c.post(
            "/brokers",
            data={
                "csrf_token": csrf_from(page.text),
                "nickname": "Cent demo 100",
                "login": "55555555",
                "server": "HFMarketsGlobal-Demo",
                "account_type": "DEMO",
                "password": CENT_PASSWORD,
                "slot": BRIDGE_2,
            },
        )
        assert r.status_code == 303, r.text[:300]
        detail = r.headers["location"].split("?")[0]
        page = await c.get(detail)
        assert "not verified" in page.text and CENT_PASSWORD not in page.text
        r = await c.post(f"{detail}/test", data={"csrf_token": csrf_from(page.text)})
        assert r.status_code == 303 and "tested=done" in r.headers["location"]
        page = await c.get(r.headers["location"])
        assert "✓ verified by HFM: DEMO account at HF Markets" in page.text, page.text[-3000:]
        r = await c.post(f"{detail}/save", data={"csrf_token": csrf_from(page.text)})
        assert r.status_code == 303
        page = await c.get(detail)
        for shown in ("CONNECTED", "HF Markets", "55555555", "USD", "1:1000", "100.0"):
            assert shown in page.text, shown
        listing = await c.get("/brokers")
        assert "Cent demo 100" in listing.text and "DEMO" in listing.text
        assert CENT_PASSWORD not in listing.text + page.text
    assert emus[BRIDGE_2].signed_in and emus[BRIDGE_2].number == 55555555
    assert emus[BRIDGE].number == 12345678  # the other terminal was never touched
    assert CENT_PASSWORD not in await _texts(db)  # stored only encrypted, never in a command
    assert CENT_PASSWORD not in (tmp_path / f"{BRIDGE_2}.jsonl").read_text()  # bridge journal


async def _add(
    db: Database,
    h: Hfm,
    settings: Settings,
    *,
    login: int,
    password: str,
    slot: str = BRIDGE_2,
    server: str = "HFMarketsGlobal-Demo",
    declared: str = "DEMO",
    nickname: str = "account",
) -> Any:
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
            now=h.env.clock.now(),
        )
    return cid


async def _get(db: Database, cid: Any) -> connections.Connection:
    async with db.connect() as conn:
        return await connections.get(conn, cid)


async def _last_event(db: Database, cid: Any) -> Any:
    async with db.connect() as conn:
        return (await connections.events(conn, cid, 1))[0]


async def _open_incident(db: Database, prefix: str) -> list[Any]:
    async with db.connect() as conn:
        return list(
            await conn.execute(
                select(incidents.c.dedup_key, incidents.c.severity).where(
                    incidents.c.dedup_key.like(f"{prefix}%"), incidents.c.status != "RESOLVED"
                )
            )
        )


# ---- 2. Incorrect password · 3. Wrong server ----------------------------------------------------


async def test_2_3_a_wrong_password_or_server_is_refused_with_advice_and_the_terminal_restored(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    emus = _emus(h, emu)
    emus[BRIDGE_2].add_account(77777777, "Right-pass-1", balance="100")
    async with terminals(db, h.env.clock, settings, emus, tmp_path):
        first = await connect(
            db,
            h.env.clock,
            settings,
            slot=BRIDGE_2,
            login=55555555,
            password=CENT_PASSWORD,
            nickname="cent",
        )
        async with db.transaction() as conn:
            await connections.disconnect(conn, first, actor=OWNER, now=h.env.clock.now())
        wrong = await _add(db, h, settings, login=77777777, password="Wrong-pass-1")
        assert await verify_now(db, h.env.clock, wrong) == "DONE"
        c, e = await _get(db, wrong), await _last_event(db, wrong)
        assert (c.status, c.verified_type, c.login_failures, e.code) == ("FAILED", None, 1, "-6")
        assert "the login number, the password or the server is wrong" in e.message
        assert "was signed back in to the account it had" in e.message
        assert emus[BRIDGE_2].number == 55555555 and emus[BRIDGE_2].signed_in  # restored
        async with db.transaction() as conn:  # the owner corrects the password in the portal
            await connections.edit(
                conn,
                settings,
                wrong,
                nickname="second",
                login="77777777",
                server="HFMarketsGlobal-Demo",
                password="Right-pass-1",
                actor=OWNER,
                now=h.env.clock.now(),
            )
        assert await verify_now(db, h.env.clock, wrong) == "DONE"
        c = await _get(db, wrong)
        assert (c.verified_type, c.login_failures) == ("DEMO", 0)
        assert emus[BRIDGE_2].number == 77777777
        async with db.transaction() as conn:
            await connections.disconnect(conn, wrong, actor=OWNER, now=h.env.clock.now())
        bad_server = await _add(
            db, h, settings, login=55555555, password=CENT_PASSWORD, server="HFMarketsGlobal-Demo9"
        )
        assert await verify_now(db, h.env.clock, bad_server) == "DONE"
        e = await _last_event(db, bad_server)
        assert e.code == "-6" and "server" in e.message
    texts = await _texts(db)
    for secret in ("Wrong-pass-1", "Right-pass-1", CENT_PASSWORD):
        assert secret not in texts


# ---- 4. Disconnected terminal · 5. Network interruption · 6. Reconnection ----------------------


async def test_4_5_6_a_closed_terminal_a_lost_network_and_the_reconnection(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    emus = _emus(h, emu)
    clock = h.env.clock
    async with terminals(db, clock, settings, emus, tmp_path) as quiet:
        cid = await connect(
            db,
            clock,
            settings,
            slot=BRIDGE_2,
            login=55555555,
            password=CENT_PASSWORD,
            nickname="cent",
        )
        await connections.check_health(db, clock.now())
        assert (await _get(db, cid)).health == "OK"
        # 5. Network: MT5 runs but has lost HFM's server.
        emus[BRIDGE_2].connected = False
        await _settle()
        await connections.check_health(db, clock.now())
        c = await _get(db, cid)
        assert (c.health, c.health_reason) == (
            "DEGRADED",
            "MT5 has lost its link to HFM's server (network)",
        )
        async with db.connect() as conn:
            assert await connections.entry_problem(conn, BRIDGE_2, clock.now()) == (
                "MT5 has lost its link to HFM's server"
            )
        assert await _open_incident(db, f"broker.connection:{cid}")
        # 6. Reconnection: MT5 reaches HFM again by itself.
        emus[BRIDGE_2].connected = True
        await _settle()
        await connections.check_health(db, clock.now())
        assert (await _get(db, cid)).health == "OK"
        assert not await _open_incident(db, f"broker.connection:{cid}")
        # 4. The terminal (or the bridge computer) stops: no report at all.
        quiet.add(BRIDGE_2)
        clock.advance(minutes=2)
        await connections.check_health(db, clock.now())
        c = await _get(db, cid)
        assert c.health == "DOWN" and "has not reported for 1" in (c.health_reason or "")
        async with db.connect() as conn:
            assert "has not reported" in (
                await connections.entry_problem(conn, BRIDGE_2, clock.now()) or ""
            )
        async with db.transaction() as conn:  # a test now is never taken: said in words
            command = await connections.start_test(conn, c, actor=OWNER, now=clock.now())
        clock.advance(minutes=2)
        quiet.discard(BRIDGE_2)
        await _settle()
        assert await connections.wait(db, command, 5) == "EXPIRED"
        e = await _last_event(db, cid)
        assert e.code == "NO_BRIDGE" and "did not take the request" in e.message
        await connections.check_health(db, clock.now())
        assert (await _get(db, cid)).health == "OK"  # reporting again: healthy


async def _settle() -> None:
    import asyncio

    await asyncio.sleep(0.4)  # a few bridge cycles, even on a busy machine


# ---- 7. Restart recovery ------------------------------------------------------------------------


async def test_7_a_terminal_restarted_signed_out_is_signed_in_again_never_locking_the_account(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    emus = _emus(h, emu)
    clock = h.env.clock
    async with terminals(db, clock, settings, emus, tmp_path):
        cid = await connect(
            db,
            clock,
            settings,
            slot=BRIDGE_2,
            login=55555555,
            password=CENT_PASSWORD,
            nickname="cent",
        )
        emus[BRIDGE_2].restart(signed_in=False)  # a fresh MT5 after a VM restart
        clock.advance(minutes=3)
        await _settle()
        found = await connections.check_health(db, clock.now())
        assert "cent: signing in again" in found
        async with db.connect() as conn:
            command = await conn.scalar(
                select(bridge_commands.c.id)
                .where(bridge_commands.c.kind == "LOGIN")
                .order_by(bridge_commands.c.created_at.desc())
                .limit(1)
            )
        assert await connections.wait(db, command, 5) == "DONE"
        assert emus[BRIDGE_2].signed_in and emus[BRIDGE_2].number == 55555555
        await _settle()
        await connections.check_health(db, clock.now())
        assert (await _get(db, cid)).health == "OK"
        # The password was changed at HFM: three refusals, then no more tries (no lock-out).
        emus[BRIDGE_2].known[(55555555, "HFMarketsGlobal-Demo")]["password"] = "changed-at-hfm"
        emus[BRIDGE_2].saved.clear()
        tries = 0
        for _ in range(5):
            emus[BRIDGE_2].restart(signed_in=False)
            clock.advance(minutes=3)
            await _settle()
            if "cent: signing in again" in await connections.check_health(db, clock.now()):
                tries += 1
                await _settle()
                await _settle()
        assert tries == 1  # one refusal is enough: never tried again, so never locked out
        c = await _get(db, cid)
        assert (c.status, c.login_failures) == ("FAILED", 1)
        assert await _open_incident(db, f"broker.password_refused:{cid}")
    assert CENT_PASSWORD not in await _texts(db)


# ---- 9. DEMO or REAL, from HFM · the read-only REAL account -------------------------------------


async def test_9_the_type_comes_from_hfm_and_a_real_account_stays_read_only(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    emus = _emus(h, emu)
    clock = h.env.clock
    async with terminals(db, clock, settings, emus, tmp_path):
        # A REAL account declared DEMO: HFM says REAL; refused, nothing usable.
        lie = await _add(
            db, h, settings, login=66666666, password=REAL_MASTER, nickname="lie", server=LIVE
        )
        assert await verify_now(db, clock, lie) == "DONE"
        c, e = await _get(db, lie), await _last_event(db, lie)
        assert (c.status, c.verified_type, e.code) == ("FAILED", None, "WRONG_TYPE")
        assert "HFM reports a REAL account, not DEMO" in e.message
        with pytest.raises(ConflictError):
            async with db.transaction() as conn:
                await connections.save(conn, lie, actor=OWNER, now=clock.now())
        async with db.transaction() as conn:  # the owner removes the wrong entry, adds it again
            await connections.disconnect(conn, lie, actor=OWNER, now=clock.now())
        # The REAL account with its investor password on the read-only terminal: verified REAL.
        real = await _add(
            db,
            h,
            settings,
            login=66666666,
            password=REAL_INVESTOR,
            slot=BRIDGE_REAL,
            declared="REAL",
            nickname="real 100",
            server=LIVE,
        )
        assert await verify_now(db, clock, real) == "DONE"
        c = await _get(db, real)
        assert c.verified_type == "REAL" and c.password_kind == "INVESTOR", c.last_error
        async with db.transaction() as conn:
            await connections.save(conn, real, actor=OWNER, now=clock.now())
        with pytest.raises(ConflictError, match="REAL execution cannot be switched on here"):
            async with db.transaction() as conn:
                await connections.set_execution(conn, real, True, actor=OWNER, now=clock.now())
        with pytest.raises(IntegrityError):  # and the database refuses it whatever the code
            async with db.transaction() as conn:
                await conn.execute(
                    update(broker_connections)
                    .where(broker_connections.c.id == real)
                    .values(execution_enabled=True)
                )
        with pytest.raises(ConflictError, match="Demo Robot can only use"):
            async with db.transaction() as conn:
                await connections.assign(conn, "DEMO_ROBOT", real, actor=OWNER, now=clock.now())
        async with db.transaction() as conn:
            await connections.assign(conn, "LIVE_ROBOT", real, actor=OWNER, now=clock.now())
            ra = await connections.robot_account(conn, "LIVE_ROBOT", clock.now())
        assert ra is not None and "live trading is not enabled" in (ra.problem or "")
        # The read-only terminal never receives a trading command, and refuses one anyway.
        async with db.transaction() as conn:
            await conn.execute(
                bridge_commands.insert().values(
                    id=connections.uuid7(),
                    bridge=BRIDGE_REAL,
                    kind="SUBMIT",
                    payload={"symbol": "XAUUSD"},
                    status="PENDING",
                    created_at=clock.now(),
                    expires_at=clock.now() + timedelta(seconds=20),
                )
            )
        await _settle()
        async with db.connect() as conn:
            pending = await conn.scalar(
                select(bridge_commands.c.status).where(bridge_commands.c.kind == "SUBMIT")
            )
        assert pending == "PENDING"  # never handed out
        from bridge.cosmic_mt5_bridge import Bridge, Config, Journal

        ro = Bridge(
            emus[BRIDGE_REAL],
            Config("https://x", BRIDGE_REAL, "t", ("XAUUSD",), "t", read_only=True),
            lambda p: {},
            Journal(tmp_path / "ro.jsonl"),
        )
        out = ro.run_command(
            {"id": "c1", "kind": "SUBMIT", "expires_at_ms": 10**15, "payload": {"symbol": "XAUUSD"}}
        )
        assert out["status"] == "FAILED" and "read-only" in out["error"]
        assert emus[BRIDGE_REAL].sent == []  # nothing ever reached MT5
        # Signed in with the trading (master) password instead: refused, and said at once.
        async with db.transaction() as conn:
            await connections.disconnect(conn, real, actor=OWNER, now=clock.now())
        emus[BRIDGE_REAL].restart(signed_in=False)
        master = await _add(
            db,
            h,
            settings,
            login=66666666,
            password=REAL_MASTER,
            slot=BRIDGE_REAL,
            declared="REAL",
            nickname="real master",
            server=LIVE,
        )
        await _settle()
        assert await verify_now(db, clock, master) == "DONE"
        assert (await _last_event(db, master)).code == "TRADING_PASSWORD"
        await _settle()
        assert ("trading.real_terminal_can_trade:hfm-real", "CRITICAL") in await _open_incident(
            db, "trading.real_terminal_can_trade"
        )
    texts = await _texts(db)
    assert REAL_MASTER not in texts and REAL_INVESTOR not in texts


# ---- 10. Switching and position isolation -------------------------------------------------------


async def test_10_switching_is_refused_while_positions_are_open_and_never_touches_them(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    emus = _emus(h, emu)
    emus[BRIDGE_2].add_account(77777777, "Right-pass-1", balance="100")
    clock = h.env.clock
    two = emus[BRIDGE_2]
    async with terminals(db, clock, settings, emus, tmp_path):
        first = await connect(
            db,
            clock,
            settings,
            slot=BRIDGE_2,
            login=55555555,
            password=CENT_PASSWORD,
            nickname="cent",
        )
        ticket = two.by_hand("BUY", "0.01", sl="4050.00")  # a position open on this account
        await _settle()
        with pytest.raises(ConflictError, match="1 open position"):
            async with db.transaction() as conn:
                await connections.disconnect(conn, first, actor=OWNER, now=clock.now())
        async with db.transaction() as conn:  # the owner leaves it at HFM, with its stop
            await connections.disconnect(conn, first, actor=OWNER, now=clock.now(), leave_open=True)
        second = await _add(db, h, settings, login=77777777, password="Right-pass-1")
        with pytest.raises(ConflictError, match="Nothing was sent"):
            await verify_now(db, clock, second)
        async with db.connect() as conn:
            logins = await conn.scalar(
                select(text("count(*)"))
                .select_from(bridge_commands)
                .where(bridge_commands.c.payload["connection_id"].astext == str(second))
            )
        assert logins == 0 and two.number == 55555555  # refused before anything was sent
        assert await verify_now(db, clock, second, leave_open=True) == "DONE"
        assert two.number == 77777777 and (await _get(db, second)).verified_type == "DEMO"
        kept = two._books[(55555555, "HFMarketsGlobal-Demo")]["positions"][ticket]
        assert (kept.volume, kept.sl) == (D("0.01"), D("4050.00"))  # untouched at HFM
        assert two.sent == []  # no order, close or modification was ever sent to MT5
        e = await _last_event(db, first)
        assert e.kind == "DISCONNECTED" and "left at HFM with their stop-loss" in e.message


# ---- 8. The account's identity, again, before every order -------------------------------------


async def _entry(h: Hfm, plan: dict[str, Any]) -> Any:
    h.emu.now_ms = int(h.env.clock.now().timestamp() * 1000) + 1000  # HFM's clock, not behind
    r = await h.env.propose(
        account_alias="hfm-demo",
        instrument_key=XAU,
        quantity=D("1"),
        limit_price=D("4101.00"),
        price_band_low=D("4090.00"),
        price_band_high=D("4110.00"),
        management_plan=plan,
        entry_conditions="Buy 1 oz at or below 4,101.",
    )
    assert r.status == "PROPOSED", r.problems
    await h.env.approve(r.intent_id)
    for _ in range(3):
        await h.env.svc.execution.run_cycle()
    return r


async def _entry_order(db: Database, intent_id: Any) -> Any:
    async with db.connect() as conn:
        return (
            await conn.execute(
                select(orders).where(orders.c.intent_id == intent_id, orders.c.purpose == "ENTRY")
            )
        ).one()


STOP = {"stop_loss": {"stop_price": D("4000.00"), "limit_price": D("3990.00")}}


async def test_8_the_demo_robot_starts_only_on_its_verified_account_checked_before_each_order(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    clock = h.env.clock
    async with terminals(db, clock, settings, {BRIDGE: emu}, tmp_path):
        await open_account(h)
        app = create_app(settings, db=db, clock=clock)
        async with client_for(app) as c:  # START is refused until the Demo Robot has an account
            await full_login(c, h.env.owner, clock)
            page = (await c.get("/gold/sim")).text
            assert "No account is assigned to the Demo Robot" in page
            r = await c.post(
                "/gold/sim/start",
                data={
                    "csrf_token": csrf_from(page),
                    "mode": "HFM_DEMO",
                    "amount": "20000",
                    "automation": "ON",
                    "permission": "ASK",
                    "risk_pct": "0.4",
                },
            )
            assert r.status_code == 409 and "Assign the Demo Robot" in r.text
        cid = await connect(db, clock, settings)  # the primary terminal's own account
        async with db.transaction() as conn:
            await connections.assign(conn, "DEMO_ROBOT", cid, actor=OWNER, now=clock.now())
        async with client_for(app) as c:
            await full_login(c, h.env.owner, clock)
            page = (await c.get("/gold/sim")).text
            assert "The Demo Robot trades: HFM demo" in page and "12345678" in page
            assert "Checked now, and again before every order" in page
        ok = await _entry(h, STOP)  # identity verified: sent and filled
        assert (await _entry_order(db, ok.intent_id)).status == "FILLED"
        async with db.transaction() as conn:  # the owner switches DEMO execution off
            await connections.set_execution(conn, cid, False, actor=OWNER, now=clock.now())
        off = await _entry(h, STOP)
        order = await _entry_order(db, off.intent_id)
        assert order.status == "REJECTED"
        async with db.connect() as conn:
            reasons = " ".join(
                str(r.detail)
                for r in await conn.execute(
                    select(order_events.c.detail).where(order_events.c.order_id == order.id)
                )
            ) + str(order.last_error)
        assert "DEMO execution is off for HFM demo" in reasons
        # The bridge itself refuses an order meant for another account, sending nothing.
        from bridge.cosmic_mt5_bridge import Bridge, Config, Journal

        b = Bridge(
            emu,
            Config("https://x", BRIDGE, "t", ("XAUUSD",), "t"),
            lambda p: {},
            Journal(tmp_path / "x.jsonl"),
        )
        sent = len(emu.sent)
        out = b.run_command(
            {
                "id": "c9",
                "kind": "SUBMIT",
                "expires_at_ms": 10**15,
                "payload": {"expected_account": "0" * 16, "symbol": "XAUUSD"},
            }
        )
        assert out["status"] == "FAILED" and "another account" in out["error"]
        assert len(emu.sent) == sent


# ---- The take-profit held by HFM, and its close booked as the exit ------------------------------


async def test_the_take_profit_is_held_by_hfm_and_its_close_is_booked_as_the_trades_exit(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    clock = h.env.clock
    async with terminals(db, clock, settings, {BRIDGE: emu}, tmp_path):
        await open_account(h)
        await connect(db, clock, settings)
        r = await _entry(h, {**STOP, "take_profit_price": D("4110.00")})
        entry = await _entry_order(db, r.intent_id)
        assert entry.status == "FILLED"
        (pos,) = emu.positions.values()
        assert (pos.sl, pos.tp) == (D("4000.00"), D("4110.00"))  # both held by HFM
        assert await h.adapter.take_profit_state(entry.client_order_id, D("4110.00")) == "HELD"
        emu.move("XAUUSD", "4110.50", "4110.80")  # HFM closes it at the take-profit itself
        clock.advance(seconds=2)
        await _settle()
        assert not emu.positions and emu.deals[-1]["reason"] == 5
        for _ in range(100):  # until the bridge has reported HFM's close
            state = await h.adapter.take_profit_state(entry.client_order_id, D("4110.00"))
            if state == "TAKEN":
                break
            await _settle()
        assert state == "TAKEN"
        for _ in range(3):
            await h.env.svc.execution.run_cycle()
        async with db.connect() as conn:
            tps = await conn.scalar(
                select(text("count(*)"))
                .select_from(orders)
                .where(orders.c.intent_id == r.intent_id, orders.c.purpose == "TAKE_PROFIT")
            )
        assert tps == 0  # the robot never sent its own: HFM had it
        assert await h.reconcile() == ()  # booked from HFM's deal: our books equal HFM's
        async with db.connect() as conn:
            exit_ = (
                await conn.execute(
                    select(orders).where(
                        orders.c.intent_id == r.intent_id, orders.c.purpose == "EXIT"
                    )
                )
            ).one()
            actions = {a.action for a in await conn.execute(select(audit_events.c.action))}
        assert exit_.status == "FILLED" and exit_.avg_fill_price == D("4110.50")
        assert exit_.client_order_id == entry.client_order_id.rsplit("-", 1)[0] + "-H1"
        assert "trade.closed_at_take_profit" in actions


async def test_the_signed_in_demo_behind_bridge_6_is_verified_from_its_own_report(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the bridge computer has bridge 7: the demo MT5 is already signed in to is verified
    from the bridge's own report (fingerprint, HFM's account mode); nothing is sent, no password
    used. An account not signed in there needs bridge 7 and says so."""
    monkeypatch.setattr("bridge.cosmic_mt5_bridge.VERSION", "6")
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    clock = h.env.clock
    async with terminals(db, clock, settings, {BRIDGE: emu}, tmp_path):
        cid = await _add(db, h, settings, login=12345678, password="demo-pass", slot=BRIDGE)
        async with db.transaction() as conn:
            c = await connections.get(conn, cid)
            assert await connections.start_test(conn, c, actor=OWNER, now=clock.now()) is None
        c, e = await _get(db, cid), await _last_event(db, cid)
        assert c.verified_type == "DEMO" and "bridge's own report (bridge version 6)" in e.message
        async with db.transaction() as conn:
            await connections.save(conn, cid, actor=OWNER, now=clock.now())
            await connections.set_execution(conn, cid, True, actor=OWNER, now=clock.now())
            await connections.assign(conn, "DEMO_ROBOT", cid, actor=OWNER, now=clock.now())
            ra = await connections.robot_account(conn, "DEMO_ROBOT", clock.now())
        assert (
            ra is not None
            and ra.problem == "no HFM account is opened on Terminal 1 · demo · HFM's prices yet"
        )
        async with db.connect() as conn:
            assert await conn.scalar(select(text("count(*)")).select_from(bridge_commands)) == 0
        other = await _add(db, h, settings, login=99999999, password="another-1", slot=BRIDGE_2)
        async with db.transaction() as conn:
            c = await connections.get(conn, other)
            with pytest.raises(ConflictError, match="Terminal 2 · demo has never reported"):
                await connections.start_test(conn, c, actor=OWNER, now=clock.now())
