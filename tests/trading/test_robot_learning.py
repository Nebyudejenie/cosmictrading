"""Learning mode on the demos (S124, the owner's choice of 2026-11-28, ADR-045): each robot takes
every valid signal of its own logic by itself on its demo, below the 30 % line too; three losing
trades in a row (or the daily loss limit) stop it for the rest of the day only; and its chance of
winning starts from its tested record and moves with each demo trade, told to the owner once per
closed trade. A real account never trades this way."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_gold import robot_controls, robot_tickets
from cosmic.gold import controls, hfm_demo, learning
from cosmic.trading import mandates
from cosmic.trading.accounts import Account
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_2
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_daily import _later, _two_demos
from tests.trading.test_gold_hfm_demo import _cycles, _move
from tests.trading.test_gold_m1 import _last, _minute, _ready, _robot, _setup, _start
from tests.trading.test_gold_sim import World
from tests.trading.test_gold_trend_test import (
    FALLING_HOURS,
    RISING_HOURS,
    TURN_DOWN,
    TURN_UP,
    _decide,
    _decisions,
    _filled,
    _grant,
    _told,
)
from tests.trading.test_hfm import bridge_running, build, next_report, open_account
from tests.trading.test_hfm_two_bridges import bridges_running, open_second

pytestmark = pytest.mark.keeps_binance_paused


def _account(kind: str) -> Account:
    now = datetime(2026, 11, 28, tzinfo=UTC)
    return Account(uuid.uuid4(), "a", kind, "HFM", "x", "USD", "ACTIVE", "OK", None, now)


def test_only_a_demo_account_learns() -> None:
    assert learning.on(_account("SANDBOX"))
    assert not learning.on(_account("LIVE")) and not learning.on(_account("PAPER"))


def test_the_chance_starts_at_the_record_and_moves_with_each_trade() -> None:
    record = learning.M1
    assert learning.learned(record.p_win, 0, 0) == D("0.2690")
    # 30 demo trades weigh as much as the four-year record: 15 won of 30 is halfway between.
    assert learning.learned(record.p_win, 15, 30) == D("0.3845")
    low, high = learning.wilson(15, 30)
    assert (low, high) == (D("0.3315"), D("0.6685"))
    card = learning.Card(record, 3, 1, 2, D("0.10"))
    assert card.label == "LEARNING · DEMO · 3 of 30 trades"
    line = card.line()
    assert line.startswith("Robot B: 3 demo trade(s): won 1 (33.3 %), lost 2 (66.7 %), net +0.10")
    assert "chance of winning now 27.5 %" in line and "tested record 26.9 %" in line
    assert "break-even 33.3 %" in line
    assert learning.Card(record, 30, 9, 21, D(0)).label == "MEASURED · DEMO · 30 trades"


def test_a_day_stop_ends_with_the_day_and_never_pauses_the_robot() -> None:
    m: Any = type("M", (), {"daily_loss_limit": D(6)})()
    c = controls.Controls(mandate_id=uuid.uuid4())
    three = controls.Results(D("-5.70"), D("-5.70"), D("-5.70"), D(0), 3, 3)
    assert controls.judge(m, c, three) is not None  # learning mode off: paused for good
    assert controls.judge(m, c, three, learning=True) is None  # its status stays RUNNING
    why = controls.day_stop(m, c, three)
    assert why is not None and why.startswith("WAIT — stopped for today: 3 losing trades in a")
    assert why.endswith("it starts again by itself at 00:00 UTC (learning mode on the demo)")
    # A new day: yesterday's three in a row do not count today.
    assert (
        controls.day_stop(m, c, controls.Results(D(0), D("-5.70"), D("-5.70"), D(0), 3, 0)) is None
    )
    lost = controls.Results(D("-6.00"), D("-6.00"), D("-6.00"), D(0), 1, 1)
    assert controls.judge(m, c, lost, learning=True) is None
    stop = controls.day_stop(m, c, lost)
    assert stop is not None and "reached the daily loss limit of 6.00" in stop


async def test_robot_b_takes_every_setup_on_its_demo_and_learns_from_each_trade(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await _ready(h, settings)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            assert (await _start(c, "100")).status_code == 303
            m = await _robot(db, h)
            home = (await c.get("/")).text
            assert "Learning · chance of winning" in home and "LEARNING · DEMO · 0 of 30" in home
            assert "no demo trade closed yet" in home and "26.9 %" in home
            assert "Learning mode on the demo" in home
            assert "/gold/robot/approve-next" not in home  # no approval is needed

            # A setup at about 27 %, below the 30 % line: taken by itself, no approval used.
            await _setup(w)
            assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:PROPOSED"]
            await _cycles(h)
            (pos,) = emu.positions.values()
            assert (pos.volume, pos.sl, pos.tp) == (D("0.01"), D("4098.40"), D("4104.30"))
            d = await _last(db)
            assert d.reasons[-1].startswith("LEARNING BUY 1 ounce at most 4100.40")
            assert d.reasons[-1].endswith(
                "sent to HFM's demo by learning mode (your choice of 2026-11-28: every setup "
                "on the demo, by itself)"
            )
            async with db.connect() as conn:
                intent = (
                    await conn.execute(
                        select(trade_intents).where(trade_intents.c.id == d.intent_id)
                    )
                ).one()
                assert list(await conn.execute(select(robot_tickets))) == []
            assert intent.details["learning"] is True and intent.details["practice"] is False
            assert intent.plain_summary.startswith(
                "HFM demo: buy 1 ounce of gold. LEARNING trade on the demo, every setup by itself"
            )

            # HFM's stop-loss closes it: the card counts a loss, and the owner is told once.
            await _move(h, "4098.30", "4098.60")
            await _cycles(h)
            assert not emu.positions
            async with db.transaction() as conn:
                card = await learning.card(conn, m)
                assert await learning.tell_closed(conn, m, h.env.clock.now()) >= 1
                assert await learning.tell_closed(conn, m, h.env.clock.now()) == 0  # once
            assert card is not None and (card.trades, card.won, card.lost) == (1, 0, 1)
            assert card.net < 0 and card.chance == learning.learned(D("0.269"), 0, 1)
            async with db.connect() as conn:
                told = [
                    r[0]
                    for r in await conn.execute(
                        select(notifications.c.payload["text"].astext).where(
                            notifications.c.kind == "gold_sim",
                            notifications.c.dedup_key.like("learning:%"),
                        )
                    )
                ]
            assert told and told[0].startswith(
                f"Robot B (demo, learning mode): trade {intent.trade_code} closed, LOST -"
            )
            assert "1 demo trade(s): won 0 (0.0 %), lost 1 (100.0 %)" in told[0]
            home = (await c.get("/")).text
            assert "LEARNING · DEMO · 1 of 30 trades" in home and "lost 1 (100.0 %)" in home

            # One loss in a row is the limit here: stopped for the rest of the day only.
            async with db.transaction() as conn:
                await conn.execute(
                    update(robot_controls)
                    .where(robot_controls.c.mandate_id == m.id)
                    .values(pause_after_losses=1)
                )
            await _minute(h, w)
            assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:WAIT"]
            stopped = await _last(db)
            assert stopped.reasons[-1].startswith(
                "WAIT — stopped for today: 1 losing trades in a row today"
            )
            async with db.connect() as conn:
                ctl = await controls.get(conn, m.id)
            assert ctl is not None and ctl.status == controls.RUNNING  # never paused

            # The next day it trades again by itself.
            h.env.clock.advance(days=1)
            h.emu.now_ms += 86_400_000
            await _move(h, "4100.00", "4100.30")
            assert await h.reconcile() == ()  # a day later: the account is checked again first
            await _minute(h, w)
            done = await hfm_demo.decide(db, h.env.clock.now())
            assert done == [f"{m.id}:PROPOSED"], (await _last(db)).reasons[-1]


async def test_robot_a_takes_a_signal_below_30_on_its_demo_in_learning_mode(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """The two signals its bands left (a SELL at 26.7 %, a BUY whose record made nothing):
    on its demo in learning mode both are sent, each with HFM holding its stop and target."""
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    emus = {BRIDGE: emu1, BRIDGE_2: emu2}
    async with bridges_running(db, h.env.clock, emus, tmp_path, lanes=True):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        m = await _grant(h)
        await w.candles("1h", FALLING_HOURS, 3.0)
        await w.candles("15m", TURN_DOWN, 3.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:PROPOSED"]
        (sell,) = await _decisions(h)
        assert sell.decision == "SELL" and D("12.16") <= sell.stop_distance < D("17.47")
        assert "won 26.7 %" in sell.reasons[-1]
        assert sell.reasons[-1].endswith(
            "sent to HFM's demo 2 by learning mode (your choice of 2026-11-28: every signal on "
            "the demo, by itself)"
        )
        await _filled(emu2)
        await _cycles(h)  # its fill booked
        (pos,) = emu2.positions.values()
        assert pos.type == 1 and pos.sl is not None and pos.tp is not None
        assert not emu1.positions  # Robot B's terminal is never touched
        assert not any("REJECTED" in t for t in await _told(h))
        async with db.connect() as conn:
            (intent,) = list(
                await conn.execute(
                    select(trade_intents).where(trade_intents.c.id == sell.intent_id)
                )
            )
            mandate = await mandates.get_mandate(conn, uuid.UUID(str(m)))
        assert intent.details["learning"] is True
        assert mandate.strategy_key == mandates.GOLD_TREND_BUY
        _later(h, (emu1, emu2), minutes=15)
        await w.candles("1h", RISING_HOURS, 3.0)
        await w.candles("15m", TURN_UP, 4.8)
        await next_report(emu2)
        # One trade at a time: the open SELL holds the BUY back, whatever its record.
        assert await _decide(h) == [f"{m}:WAIT"]
        *_, buy = await _decisions(h)
        assert buy.reasons[-1] == "WAIT — Robot A holds one trade at a time, and one is open"
