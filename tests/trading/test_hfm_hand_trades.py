"""Trades the owner makes by hand in MT5 are theirs (S59, the owner's choice of 2026-11-04).

On 2026-11-04 the owner traded gold by hand from the MT5 app on the demo the system manages. The
check with HFM failed (fills unknown to the account), a CRITICAL incident that blocks every
account's trades. Now a trade made by hand (no magic number) is booked as the owner's, outside the
system's money; the check stays exact; the system never touches it, while "Is my money
protected?" still counts it. A hand close of the system's own trade is still a disagreement."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders
from cosmic.market import data as md
from cosmic.reporting import hfm_home
from cosmic.trading import hfm_test_trade
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.mt5_bridge import by_hand
from cosmic.trading.exits import propose_close
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_hfm import XAU, Hfm, bridge_running, build, open_account
from tests.trading.test_hfm_test_trade import _cycles, _refusal, _snapshot_until, _weekday


async def _home(h: Hfm) -> hfm_home.HfmHome:
    async with h.env.db.connect() as conn:
        return await hfm_home.build(conn, h.env.clock.now())


async def _system_buy(h: Hfm) -> int:
    """The test trade, approved and filled; HFM's position ticket."""
    async with h.env.db.transaction() as conn:
        r = await hfm_test_trade.propose(
            conn, actor=Actor.user(h.env.owner.user_id), now=h.env.clock.now()
        )
    assert r.status == "PROPOSED", r.problems
    await h.env.approve(r.intent_id)
    await _cycles(h)
    (ticket,) = [t for t, p in h.emu.positions.items() if p.magic != 0]
    return ticket


async def _system_close(h: Hfm) -> None:
    async with h.env.db.transaction() as conn:
        exit_ = await propose_close(
            conn,
            account=await get_account(conn, account_id=h.env.account_id),
            inst=await md.instrument_by_key(conn, XAU),
            reason="closed from the home page",
            actor=Actor.user(h.env.owner.user_id),
            now=h.env.clock.now(),
        )
    await h.env.approve(exit_.intent_id)
    await _cycles(h)


async def _settled(h: Hfm) -> None:
    """Until the bridge reports the emulator as it is now."""
    tickets = set(h.emu.positions)
    deals = len(h.emu.deals)
    await _snapshot_until(
        h,
        lambda s: (
            {p["ticket"] for p in s.get("positions") or []} == tickets
            and len(s.get("deals") or []) == deals
        ),
    )


async def test_trades_by_hand_are_the_owners_and_the_check_stays_exact(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        # As on 2026-11-04: a buy and a sell by hand, then a buy left open with its stop.
        first = emu.by_hand("BUY", "0.05")
        emu.move("XAUUSD", "4103.00", "4103.30")
        emu.by_hand("SELL", "0.05", position=first)
        emu.by_hand("BUY", "0.02", sl="4090.00")
        await _settled(h)
        assert await h.reconcile() == ()  # was: 3 unknown fills, a cash gap and a position
        hand = emu.balance - D(100_000)  # what the owner's trades did to HFM's balance
        assert hand != 0
        assert await h.book("CASH", "USD") == D(20_000)  # the system's money, untouched
        assert await h.book("UNMANAGED", "USD") == D(80_000) + hand
        assert await h.reconcile() == ()  # booked once: checked again, nothing new
        assert await h.book("UNMANAGED", "USD") == D(80_000) + hand

        home = await _home(h)
        (pos,) = home.positions
        assert pos.own and home.own_open == 1 and home.own_result == hand
        assert not home.gold_held  # no close is offered for the owner's trade
        protected = next(a for a in home.answers if a.question == "Is my money protected?")
        assert protected.ok is True  # it has its stop at HFM
        assert await _refusal(h) is None  # the owner's open trade does not stop a test

        # The system trades beside it, and only its own trade is its.
        ticket = await _system_buy(h)
        await _settled(h)
        assert await h.reconcile() == ()
        home = await _home(h)
        assert home.gold_held and sorted(p.own for p in home.positions) == [False, True]
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            await _snapshot_until(h, lambda s: True)
            page = (await c.get("/")).text
        assert "(yours, by hand)" in page and "Your own trades, made by hand in MT5:" in page
        assert "Close the gold trade" in page
        await _system_close(h)
        assert ticket not in emu.positions and len(emu.positions) == 1  # the owner's stays
        await _settled(h)
        assert await h.reconcile() == ()
        # HFM's balance to the cent: the owner's open trade is in it only once it closes.
        assert await h.book("CASH", "USD") + await h.book("UNMANAGED", "USD") == emu.balance


async def test_a_hand_close_of_the_systems_trade_is_still_a_disagreement(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """Fail closed: the owner may trade beside the system, not inside its trades."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        ticket = await _system_buy(h)
        emu.by_hand("SELL", "0.01", position=ticket)  # closed from the phone
        await _settled(h)
        mismatches = await h.reconcile()
        assert any("FILLS" in m or "POSITIONS" in m for m in mismatches), mismatches
        assert (await _home(h)).own_deals == 0  # not booked as the owner's


async def test_the_systems_stop_out_tagged_by_hand_stays_the_systems(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """HFM may tag the deal of a stop-out with magic 0, as it tagged the owner's stop-out."""
    emu = MT5Emulator(sl_deal_magic_zero=True)
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        await _system_buy(h)
        emu.move("XAUUSD", "3950.00", "3950.30")  # below the stop 3 % down
        assert not emu.positions and emu.deals[-1]["magic"] == 0
        await _settled(h)
        await _cycles(h)
        async with db.connect() as conn:
            stop = await conn.scalar(
                select(orders.c.status).where(
                    orders.c.account_id == h.env.account_id, orders.c.purpose == "STOP_LOSS"
                )
            )
        assert stop == "FILLED"
        assert await h.reconcile() == ()
        home = await _home(h)
        assert home.own_deals == 0 and home.own_open == 0


# HFM's real report of 2026-11-04 07:10 UTC (the owner's trades from the MT5 app; prices and
# results as HFM reported them, no account details): every deal and the open sell are theirs.
REPORT_2026_11_04 = {
    "deals": [
        {"ticket": t, "position_id": pid, "type": ty, "entry": e, "magic": 0, "reason": r}
        for t, pid, ty, e, r in (
            (1236348936, 1434268512, 0, 0, 1),
            (1236353428, 1434282494, 0, 0, 1),
            (1236355660, 1434282494, 1, 1, 1),
            (1236358902, 1434268512, 1, 1, 4),  # its stop-loss, tagged magic 0
            (1236360366, 1434308629, 0, 0, 1),
            (1236363720, 1434308629, 1, 1, 4),
            (1236364693, 1434325206, 0, 0, 1),
            (1236366107, 1434325206, 1, 1, 4),
            (1236366943, 1434336834, 1, 0, 1),  # a sell that opens a short
        )
    ],
    "positions": [{"ticket": 1434336834, "type": 1, "magic": 0, "sl": "0.0"}],
}


def test_hfms_real_report_of_the_owners_trades_is_all_theirs() -> None:
    deals, positions = by_hand(REPORT_2026_11_04)
    assert len(deals) == 9 and positions == {1434336834}
    # A system position beside them stays the system's, its magic-0 stop-out too.
    report = {
        "deals": [
            *REPORT_2026_11_04["deals"],
            {"ticket": 1, "position_id": 9, "type": 0, "entry": 0, "magic": 77, "reason": 3},
            {"ticket": 2, "position_id": 9, "type": 1, "entry": 1, "magic": 0, "reason": 4},
        ],
        "positions": REPORT_2026_11_04["positions"],
    }
    deals, _ = by_hand(report)
    assert 1 not in deals and 2 not in deals and len(deals) == 9


async def test_a_trade_by_hand_without_a_stop_is_said_and_where_to_fix_it(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        emu.by_hand("BUY", "0.02")  # no stop-loss
        await _settled(h)
        assert await h.reconcile() == ()
        home = await _home(h)
    protected = next(a for a in home.answers if a.question == "Is my money protected?")
    assert protected.ok is False and home.status == "PROBLEM"
    assert "A trade you made by hand in MT5 has no stop-loss: set one in MT5, or close it." in (
        home.attention
    )
    assert not any("see Portfolio" in t for t in home.attention)  # Portfolio is the system's
