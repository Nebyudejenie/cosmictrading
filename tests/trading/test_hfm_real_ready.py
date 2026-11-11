"""The way from the HFM demo to a real account (S58): a checklist read from evidence, and the
switch to a real account tested without a real order.

Each check passes only on what is recorded (HFM's report, the checks against HFM, the demo's
own trades), never on a setting. A real account reported by MT5 is refused by three layers on
their own: the server's adapter, the server's command hand-out, and the bridge program."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

from sqlalchemy import func, select

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_trading import bridge_commands
from cosmic.market import data as md
from cosmic.reporting import hfm_home
from cosmic.trading import hfm_ready, hfm_test_trade
from cosmic.trading.accounts import get_account
from cosmic.trading.exits import propose_close
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL, MT5Emulator
from tests.trading.test_hfm import XAU, Hfm, bridge_running, build, open_account
from tests.trading.test_hfm_test_trade import _cycles, _snapshot_until, _weekday


async def _checks(h: Hfm) -> dict[str, hfm_ready.Check]:
    async with h.env.db.connect() as conn:
        r = await hfm_ready.checklist(conn, h.env.clock.now())
    return {c.title: c for c in r.checks}


def _status(checks: dict[str, hfm_ready.Check], start: str) -> str:
    return next(c.status for t, c in checks.items() if t.startswith(start))


def _evidence(checks: dict[str, hfm_ready.Check], start: str) -> list[str]:
    return next(c.evidence for t, c in checks.items() if t.startswith(start))


async def _submits(db: Database) -> int:
    async with db.connect() as conn:
        return int(
            await conn.scalar(
                select(func.count())
                .select_from(bridge_commands)
                .where(bridge_commands.c.kind == "SUBMIT")
            )
            or 0
        )


async def _test_trade(h: Hfm) -> None:
    """A test trade the owner's way, then a moment later the check with HFM."""
    async with h.env.db.transaction() as conn:
        entry = await hfm_test_trade.propose(
            conn, actor=Actor.user(h.env.owner.user_id), now=h.env.clock.now()
        )
    await h.env.approve(entry.intent_id)
    await _cycles(h)
    await _snapshot_until(h, lambda s: bool(s.get("positions")))
    async with h.env.db.transaction() as conn:
        account = await get_account(conn, account_id=h.env.account_id)
        exit_ = await propose_close(
            conn,
            account=account,
            inst=await md.instrument_by_key(conn, XAU),
            reason="closed from the home page",
            actor=Actor.user(h.env.owner.user_id),
            now=h.env.clock.now(),
        )
    await h.env.approve(exit_.intent_id)
    await _cycles(h)
    await _snapshot_until(h, lambda s: not s.get("positions"))
    h.env.clock.advance(seconds=5)
    await _snapshot_until(h, lambda s: True)
    assert await h.reconcile() == ()


async def test_the_checklist_passes_only_on_evidence(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        checks = await _checks(h)
        assert _status(checks, "Your HFM demo is connected") == "PASSED"
        assert _status(checks, "Gold prices arrive") == "PASSED"
        # Configured and connected is not proof: no order has filled, too few checks yet.
        assert _status(checks, "An order filled") == "NOT MET"
        assert _status(checks, "A trade closed") == "NOT MET"
        assert _status(checks, "Checked against HFM, reliably") == "NOT MET"
        assert _status(checks, "It recovers by itself") == "NOT MET"
        for blocked in (
            "A Cent demo account",
            "30 days on the Cent demo",
            "The real account on its own bridge",
            "Trade sizes from the real account",
            "Your authorization",
        ):
            assert _status(checks, blocked) == "BLOCKED", blocked

        await _test_trade(h)
        checks = await _checks(h)
        assert _status(checks, "An order filled") == "PASSED"
        (filled,) = _evidence(checks, "An order filled")
        assert "1 oz bought at 4100.3; HFM held its stop-loss at " in filled
        assert _status(checks, "A trade closed") == "PASSED"
        closed = _evidence(checks, "A trade closed")
        assert "closed at 4100" in closed[0] and "our ledger equals HFM's balance" in closed[1]
        async with db.connect() as conn:
            home = await hfm_home.build(conn, h.env.clock.now())
        assert (home.real_passed, home.real_total) == (4, 11)

        # The page, for the owner: every check, with its evidence.
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/golive")).text
            assert "A real HFM account: is it ready?" in page and "4 of 11 checks passed" in page
            assert "HFM held its stop-loss" in page
            home_page = (await c.get("/")).text
            assert "4 of 11 checks passed" in home_page


async def test_recovery_is_shown_from_a_real_outage(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
    h.env.clock.advance(minutes=1)  # the bridge drops
    assert await h.reconcile() != ()
    assert _status(await _checks(h), "It recovers by itself") == "NOT MET"
    async with bridge_running(db, h.env.clock, emu, tmp_path):  # and comes back
        await _snapshot_until(h, lambda s: True)
        assert await h.reconcile() == ()
        checks = await _checks(h)
    assert _status(checks, "It recovers by itself") == "PASSED"
    recovered = _evidence(checks, "It recovers by itself")
    assert recovered[0] == (
        "1 warnings of HFM unreachable, each closed by itself once HFM answered again"
    )
    assert recovered[1].startswith("the longest: ") and "in the last 7 days" in recovered[1]
    assert "could not reach HFM" in _evidence(checks, "Checked against HFM, reliably")[0]


async def test_switching_to_a_real_account_sends_nothing(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """The DEMO-to-REAL switch, without a real order: MT5 logged into a real account while an
    approved demo trade waits to be sent. Nothing reaches MT5, and each layer refuses alone."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path) as bridge:
        await open_account(h)
        await _weekday(h)
        async with db.transaction() as conn:
            entry = await hfm_test_trade.propose(
                conn, actor=Actor.user(h.env.owner.user_id), now=h.env.clock.now()
            )
        await h.env.approve(entry.intent_id)  # approved, not sent yet
        emu.trade_mode, emu.number = ACCOUNT_TRADE_MODE_REAL, 87654321
        await _snapshot_until(h, lambda s: s["account"]["trade_mode"] == "REAL")
        sent, submits = len(emu.sent), await _submits(db)
        await _cycles(h)
        assert len(emu.sent) == sent and not emu.positions and not emu.orders
        # Layers 1 and 2: the adapter made no command, so the hand-out had nothing to give.
        assert await _submits(db) == submits
        async with db.connect() as conn:
            home = await hfm_home.build(conn, h.env.clock.now())
            why = await hfm_test_trade.refusal(conn, h.env.clock.now())
        broker = next(a for a in home.answers if a.question == "Is my broker connected?")
        assert home.status == "PROBLEM" and "a REAL account: every order is refused" in broker.text
        assert why == "the MT5 terminal is not on a demo account"
        checks = await _checks(h)
        assert _status(checks, "Your HFM demo is connected") == "NOT MET"
        # Layer 3: the bridge program on the VM refuses a real account by itself.
        result = bridge.run_command(
            {
                "id": "a-real-order",
                "kind": "SUBMIT",
                "payload": {"symbol": "XAUUSD", "side": "BUY", "volume": "0.01"},
                "expires_at_ms": emu.now_ms + 60_000,
            }
        )
        assert "the bridge trades demo accounts only" in str(result)
        assert len(emu.sent) == sent
    assert D(emu.balance) == D(100_000)  # nothing moved
