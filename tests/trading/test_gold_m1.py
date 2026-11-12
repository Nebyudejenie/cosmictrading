"""The 1-minute gold rule on the HFM demo (S85, the owner's directive of 2026-11-11), end to end
through the real bridge program against the MT5 emulator:

- START with 100 USD runs the 1-minute rule, with no self-test trades; a setup below the
  owner's 30 % threshold is rejected and recorded, and nothing reaches HFM;
- a PRACTICE trade opens only under the owner's approval given in advance (one press, one
  trade), 1 ounce for 2.00 at risk, with its stop-loss and take-profit both inside the order
  and both held by HFM; HFM's own take-profit closes it and its deal is booked;
- a position whose take-profit HFM does not hold raises a CRITICAL alarm and pauses the robot;
- with a bridge before version 7 (no take-profit at HFM) the rule opens nothing;
- the owner's bands: 50 % or more opens by itself (Automatic), 30 % to 50 % asks;
- every prediction is stored once a candle and scored against HFM's candles, without a trade.
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DBAPIError

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, incidents, notifications
from cosmic.db.schema_execution import approvals, order_events, orders, trade_intents
from cosmic.db.schema_gold import (
    gold_hfm_decisions,
    gold_hfm_selftests,
    gold_prediction_outcomes,
    gold_predictions,
    robot_tickets,
)
from cosmic.gold import controls, hfm_demo, predictions, probability, sim
from cosmic.gold import jobs as gold_jobs
from cosmic.market import data as md
from cosmic.market.hfm_candles import Candle
from cosmic.trading import mandates
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.mt5_emulator import MT5Emulator
from tests.trading.broker_helpers import OWNER, assign_demo_robot
from tests.trading.test_gold_hfm_demo import _cycles, _move, _no_hfm_fee
from tests.trading.test_gold_sim import World
from tests.trading.test_hfm import WEDNESDAY, Hfm, bridge_running, build, open_account
from tests.trading.test_hfm_test_trade import _weekday
from tests.unit.test_gold_probability import _turn

pytestmark = pytest.mark.keeps_binance_paused


async def _setup(w: World, sign: int = 1) -> None:
    """HFM's candles with a 1-minute setup on the last one: the 15- and 5-minute trends agree
    and the 1-minute momentum has just turned with them."""
    await w.candles("15m", [3990 + sign * 1.0 * i for i in range(120)], half=1.0)
    await w.candles("5m", [4000 + sign * 0.4 * i for i in range(120)], half=0.5)
    await w.candles("1m", _turn(sign), 0.3)


async def _minute(h: Hfm, w: World) -> None:
    """A minute later: HFM's clock in step, a new report, and the setup on the new candle."""
    h.env.clock.advance(minutes=1)
    h.emu.now_ms += 60_000
    await asyncio.sleep(0.1)
    await _setup(w)


async def _ready(h: Hfm, settings: Settings) -> None:
    await open_account(h)
    await assign_demo_robot(h.env.db, h.env.clock, settings)
    await _no_hfm_fee(h)  # HFM's measured commission on the owner's demo: 0
    await _weekday(h)  # Wednesday, 10:00 in New York: gold trades


async def _start(c: Any, amount: str = "100", permission: str = "AUTO") -> Any:
    page = (await c.get("/gold/sim")).text
    return await c.post(
        "/gold/sim/start",
        data={
            "csrf_token": csrf_from(page),
            "mode": "HFM_DEMO",
            "amount": amount,
            "automation": "ON",
            "permission": permission,
            "risk_pct": "2",
        },
    )


async def _robot(db: Database, h: Hfm) -> mandates.Mandate:
    async with db.connect() as conn:
        (m,) = await hfm_demo.gold_mandates(conn, h.env.clock.now())
    return m


async def _last(db: Database) -> Any:
    async with db.connect() as conn:
        return (
            await conn.execute(
                select(gold_hfm_decisions).order_by(gold_hfm_decisions.c.bar.desc()).limit(1)
            )
        ).one()


async def _approve_next(c: Any, m: mandates.Mandate) -> Any:
    page = (await c.get("/")).text
    return await c.post(
        "/gold/robot/approve-next",
        data={"csrf_token": csrf_from(page), "mandate_id": str(m.id)},
    )


async def test_start_with_100_runs_the_1_minute_rule_and_a_practice_trade_needs_my_approval(
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
            small = await _start(c, "50")  # 1.00 a trade: HFM's 1 ounce risks 2.00
            assert small.status_code == 200 and "Nothing was started on HFM" in small.text
            assert "from an amount of 100.00" in small.text
            r = await _start(c, "100")
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=hfm"
            m = await _robot(db, h)
            assert (m.strategy_key, m.capital, m.max_loss_per_trade, m.daily_loss_limit) == (
                mandates.GOLD_M1,
                D(100),
                D("2.00"),
                D("6.00"),
            )
            assert m.money_cap == D(5000)  # 1 ounce of gold for 100 USD: the demo's own cap
            async with db.connect() as conn:  # no self-test, so no artificial trade at START
                assert (await conn.execute(select(gold_hfm_selftests))).first() is None
            sent = len(emu.sent)

            # A setup at about 27 %: below the 30 % threshold, so rejected; nothing is sent.
            await _setup(w)
            assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:WAIT"]
            d = await _last(db)
            assert d.decision == "BUY" and d.stop_distance == D("2.00")
            assert d.reasons[0].startswith("Probability that the take-profit comes before")
            assert "break-even 33.3%" in d.reasons[0] and "M15 UP, M5 UP" in d.reasons[0]
            assert d.reasons[-1].startswith("REJECTED — BUY at 2") and "30 %" in d.reasons[-1]
            async with db.connect() as conn:
                (p,) = list(await conn.execute(select(gold_predictions)))
            assert p.setup == "BUY" and D("0.2614") <= p.p_buy <= D("0.2704")
            assert p.model == "m1-prob-1" and (p.bid, p.ask) == (D("4100.00"), D("4100.30"))
            assert len(emu.sent) == sent and not emu.positions

            # The owner approves the NEXT trade, with their code: one press, one trade.
            home = (await c.get("/")).text
            for shown in (
                "Direction · probability",
                "UP · BUY 2",  # the trends' direction, and that trade's measured probability
                "no prediction scored yet",
                "Prediction accuracy",
                "Next trade",
                "Approve the next PRACTICE trade (below your 30 % threshold)",
                "of today's risk budget left",
            ):
                assert shown in home, shown
            r = await _approve_next(c, m)
            assert r.status_code == 303 and r.headers["location"] == "/?approved=next"
            again = await _approve_next(c, m)
            assert again.status_code == 409 and "already waiting" in again.text
            home = (await c.get("/?approved=next")).text
            assert "Your approval is waiting" in home and "PRACTICE trade" in home

            await _minute(h, w)
            assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:PROPOSED"]
            await _cycles(h)
            (pos,) = emu.positions.values()
            # 1 ounce, filled at the ask; 2.00 from the limit to the stop; 4.00 to the target:
            # both protections inside the order, and both on HFM's own position record.
            assert (pos.volume, pos.price_open) == (D("0.01"), D("4100.30"))
            assert (pos.sl, pos.tp) == (D("4098.40"), D("4104.30"))
            d = await _last(db)
            assert d.reasons[-1].startswith(
                "PRACTICE BUY 1 ounce at most 4100.40, stop-loss 4098.40, take-profit 4104.30, "
                "both held by HFM; probability 2"
            )
            assert "at most 2.00 to lose, 4.00 to win (risk to reward 1:2)" in d.reasons[-1]
            assert "that the take-profit comes first (7" in d.reasons[-1]  # and the stop-loss
            assert d.reasons[-1].endswith(
                "sent to HFM's demo under the approval you gave in advance"
            )
            async with db.connect() as conn:
                intent = (
                    await conn.execute(
                        select(trade_intents).where(trade_intents.c.id == d.intent_id)
                    )
                ).one()
                (ticket,) = list(await conn.execute(select(robot_tickets)))
                (approval,) = list(
                    await conn.execute(select(approvals).where(approvals.c.intent_id == intent.id))
                )
                entry = (
                    await conn.execute(
                        select(orders).where(
                            orders.c.intent_id == intent.id, orders.c.purpose == "ENTRY"
                        )
                    )
                ).one()
                armed = [
                    a.action
                    for a in await conn.execute(
                        select(audit_events.c.action).where(
                            audit_events.c.action.like("robot.trade_%")
                        )
                    )
                ]
            assert intent.details["practice"] is True and intent.details["model"] == "m1-prob-1"
            assert intent.details["rule"] == "gold_m1" and intent.details["band"] == "REJECT"
            assert intent.plain_summary.startswith("HFM demo: buy 1 ounce of gold. PRACTICE trade")
            assert (ticket.ended, ticket.intent_id, ticket.practice) == ("USED", intent.id, True)
            assert approval.channel == "MANDATE" and entry.status == "FILLED"
            assert armed == ["robot.trade_approved_in_advance", "robot.trade_approval_used"]
            assert await h.adapter.take_profit_state(entry.client_order_id, D("4104.30")) == "HELD"

            # The approval is used up: the next setup is not taken.
            await _minute(h, w)
            assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:WAIT"]
            assert len(emu.positions) == 1
            home = (await c.get("/")).text
            assert "4098.4" in home and "4104.3" in home  # the open trade's stop and target
            report = (await c.get("/gold/training")).text
            assert "Probability calibration" in report and "m1-prob-1" in report

        # HFM closes it at its own take-profit; its deal is booked as the trade's exit.
        await _move(h, "4104.40", "4104.70")
        await _cycles(h)
        assert not emu.positions and emu.deals[-1]["reason"] == 5
        async with db.connect() as conn:
            closed = (
                await conn.execute(
                    select(orders).where(
                        orders.c.intent_id == intent.id, orders.c.purpose == "EXIT"
                    )
                )
            ).one()
            ctl = await controls.get(conn, m.id)
            assert ctl is not None
            res = await controls.results(conn, m, ctl, h.env.clock.now(), [], D(0), D(0))
        assert closed.status == "FILLED" and closed.avg_fill_price == D("4104.40")
        assert D("4.00") <= res.today <= D("4.10")  # 4.10 less the emulator's commission
        assert await h.reconcile() == ()


async def test_a_take_profit_hfm_does_not_hold_is_an_alarm_and_pauses_the_robot(
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
            assert (await _start(c)).status_code == 303
            m = await _robot(db, h)
            assert (await _approve_next(c, m)).status_code == 303
        await _setup(w)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:PROPOSED"]
        await _cycles(h)
        (pos,) = emu.positions.values()
        pos.tp = D(0)  # HFM's position record loses its take-profit
        await asyncio.sleep(0.2)
        await _cycles(h, 2)
        async with db.connect() as conn:
            alarms = list(
                await conn.execute(
                    select(incidents.c.severity, incidents.c.status, incidents.c.title).where(
                        incidents.c.dedup_key.like("trading.take_profit_not_held:%")
                    )
                )
            )
        assert [(a.severity, a.status) for a in alarms] == [("CRITICAL", "OPEN")], alarms
        assert "HFM does not hold the take-profit" in alarms[0].title
        assert pos.sl == D("4098.40")  # the stop-loss is still HFM's
        # No new entry: the robot is paused until the owner resumes it.
        await _minute(h, w)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:WAIT"]
        async with db.connect() as conn:
            ctl = await controls.get(conn, m.id)
        assert ctl is not None and ctl.status == controls.PAUSED
        assert "HFM does not hold the take-profit of T-" in ctl.status_reason
        assert (await _last(db)).reasons[-1].startswith("WAIT — paused")
        # Meanwhile the robot watches the target itself and closes there, the stop still on.
        await _move(h, "4104.40", "4104.70")
        await _cycles(h)
        assert not emu.positions
        await _cycles(h, 2)
        async with db.connect() as conn:
            still = await conn.scalar(
                select(incidents.c.status).where(
                    incidents.c.dedup_key.like("trading.take_profit_not_held:%")
                )
            )
        assert still == "RESOLVED"
        assert await h.reconcile() == ()


async def test_a_practice_trade_closed_by_hand_is_booked_at_once_and_the_robot_runs_on(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S86: the owner may close the robot's trade from the phone. HFM's own deal is booked as
    the trade's exit in the next cycle: no stop is sent for a position that is gone, no
    "protective order" alarm is raised, so nothing pauses the robot, and the result counts."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await _ready(h, settings)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            assert (await _start(c)).status_code == 303
            m = await _robot(db, h)
            assert (await _approve_next(c, m)).status_code == 303
        await _setup(w)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:PROPOSED"]
        await _cycles(h)
        (ticket,) = emu.positions
        sent = len(emu.sent)
        emu.by_hand("SELL", "0.01", position=ticket)  # closed from the phone, at the bid
        for _ in range(200):  # until the bridge has reported HFM's deal
            if await h.adapter.hand_closes(h.env.account_id):
                break
            await asyncio.sleep(0.02)
        await _cycles(h)
        async with db.connect() as conn:
            alarms = [
                r.title
                for r in await conn.execute(
                    select(incidents.c.title).where(incidents.c.category == "TRADING")
                )
            ]
            closed = (await conn.execute(select(orders).where(orders.c.purpose == "EXIT"))).one()
            stops = [
                o.status
                for o in await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))
            ]
            ctl = await controls.get(conn, m.id)
            assert ctl is not None
            res = await controls.results(conn, m, ctl, h.env.clock.now(), [], D(0), D(0))
        assert alarms == [] and len(emu.sent) == sent  # no alarm; nothing more sent to HFM
        assert closed.status == "FILLED" and closed.client_order_id.endswith("-H1")
        assert closed.avg_fill_price == D("4100.00") and stops == ["CANCELLED"]
        assert ctl.status == controls.RUNNING
        assert D("-0.40") <= res.today <= D("-0.30")  # bought at the ask, sold at the bid
        await _minute(h, w)  # and the robot decides on: no approval waits, so it waits
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:WAIT"]
        assert not (await _last(db)).reasons[-1].startswith("WAIT — paused")
        assert await h.reconcile() == ()


async def _armed(h: Hfm, settings: Settings, db: Database) -> mandates.Mandate:
    """The robot started with 100 and the owner's approval of its next trade waiting."""
    await _ready(h, settings)
    app = create_app(settings, db=db, clock=h.env.clock)
    async with client_for(app) as c:
        await full_login(c, h.env.owner, h.env.clock)
        assert (await _start(c)).status_code == 303
        m = await _robot(db, h)
        assert (await _approve_next(c, m)).status_code == 303
    return m


async def _entry_events(db: Database) -> tuple[Any, dict[str, Any]]:
    """The entry order, and its events' details by status."""
    async with db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.purpose == "ENTRY"))).one()
        events = {
            e.to_status: e.detail or {}
            for e in await conn.execute(
                select(order_events).where(order_events.c.order_id == entry.id)
            )
        }
    return entry, events


async def test_the_order_leaves_for_hfm_in_the_same_step_as_its_prediction(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S87 (the owner's rule of 2026-11-12: the trading request leaves within one second of the
    prediction). The decision and its order are one step: no trading cycle runs here at all.
    The order travels on the bridge's fast lane, HFM holds the stop-loss and the take-profit
    from the fill, and the order's record says how long each part took."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        m = await _armed(h, settings, db)
        await _setup(w)
        sent = len(emu.sent)
        # The step's own database work, counted until the order reaches MT5: what its speed
        # is made of (in production a statement takes about 0.7 ms and a connection about 2).
        mine: ContextVar[bool] = ContextVar("mine", default=False)
        work = {"statements": 0, "connections": 0}
        at_mt5: dict[str, Any] = {}
        engine = db.engine.sync_engine

        def statement(*_: Any) -> None:
            work["statements"] += int(mine.get())

        def connection(*_: Any) -> None:
            work["connections"] += int(mine.get())

        event.listen(engine, "before_cursor_execute", statement)
        event.listen(engine, "checkout", connection)
        real = emu.order_send

        def order_send(request: dict[str, Any]) -> Any:
            at_mt5.update(work, at=time.monotonic())
            return real(request)

        emu.order_send = order_send  # type: ignore[method-assign]
        mine.set(True)
        started = time.monotonic()
        assert await gold_jobs.hfm_now(h.ctx, h.env.clock.now(), selftest=False) == [
            f"{m.id}:PROPOSED"
        ]
        mine.set(False)
        event.remove(engine, "before_cursor_execute", statement)
        event.remove(engine, "checkout", connection)
        # HFM has the order, with both protections, before any cycle ran.
        (pos,) = emu.positions.values()
        assert len(emu.sent) == sent + 1
        assert (pos.volume, pos.sl, pos.tp) == (D("0.01"), D("4098.40"), D("4104.30"))
        entry, events = await _entry_events(db)
        speed = events["ACKNOWLEDGED"]
        assert speed["speed_lane"] == "fast"  # not with the report
        total = int(speed["speed_total_ms"])
        assert total == sum(
            int(speed[k]) for k in ("speed_handed_ms", "speed_network_ms", "speed_bridge_ms")
        )
        assert 0 <= int(speed["speed_bridge_ms"]) < 1000 and "speed_hfm_answer_ms" in speed
        async with db.connect() as conn:
            intent = (
                await conn.execute(
                    select(trade_intents).where(trade_intents.c.id == entry.intent_id)
                )
            ).one()
        assert intent.details["send_within_ms"] == 1000
        assert intent.details["decided_at"] == h.env.clock.now().isoformat()
        # From the decision to MT5: every risk check and record on the way. The budget keeps
        # the step from growing slower unnoticed (measured 2026-11-12: 150 and 17).
        print(
            f"decision to MT5: {(at_mt5['at'] - started) * 1000:.0f} ms here, "
            f"{at_mt5['statements']} statements, {at_mt5['connections']} connections"
        )
        assert at_mt5["statements"] <= 170 and at_mt5["connections"] <= 26, at_mt5
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:  # and the dashboard says it
            await full_login(c, h.env.owner, h.env.clock)
            home = (await c.get("/")).text
        assert "an order leaves for HFM within 1 second of its prediction" in home
        assert "The fast path to HFM is up" in home
        assert f"The last order left {total} ms after its prediction; HFM answered in" in home
        await _cycles(h)  # the cycles then book the fill and find nothing to repair
        assert await h.reconcile() == ()


async def test_the_trade_is_checked_against_the_price_it_was_decided_on(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """Production, 2026-11-12 13:04 UTC: the owner's approved practice sell was refused by the
    price check ("R10: mid 3936.20 outside approved band 3935.57–3936.17") and their approval
    was used up. The check read HFM's price as the reports store it every 15 seconds; the
    robot had decided on HFM's newest price, 0.36 away. The price decided on is now stored as
    HFM's quote at the decision, so the checks and the trade use the same price."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        m = await _armed(h, settings, db)
        # Two seconds after the last stored quote (4,100.00 / 4,100.30) HFM's price is 0.40 up.
        h.env.clock.advance(seconds=2)
        emu.move("XAUUSD", "4100.40", "4100.70")
        emu.now_ms = int(h.env.clock.now().timestamp() * 1000)
        for _ in range(200):  # until the bridge has sent it
            async with db.connect() as conn:
                price = await sim.price_now(conn, h.env.clock.now())
                stored = await md.latest_quote(conn, (await _gold(conn)).id, h.env.clock.now())
            if price is not None and price.bid == D("4100.40"):
                break
            await asyncio.sleep(0.02)
        assert stored.bid == D("4100.00")  # the checks' price is the older one, as in production
        await _setup(w)
        assert await gold_jobs.hfm_now(h.ctx, h.env.clock.now(), selftest=False) == [
            f"{m.id}:PROPOSED"
        ]  # was REFUSED: mid 4100.15 outside approved band 4100.20–4100.80
        (pos,) = emu.positions.values()
        assert (pos.price_open, pos.sl, pos.tp) == (D("4100.70"), D("4098.80"), D("4104.70"))
        async with db.connect() as conn:
            stored = await md.latest_quote(conn, (await _gold(conn)).id, h.env.clock.now())
            (ticket,) = list(await conn.execute(select(robot_tickets)))
        assert (stored.bid, stored.ask) == (D("4100.40"), D("4100.70"))  # HFM's own tick
        assert ticket.ended == "USED"
        await _cycles(h)
        assert await h.reconcile() == ()


async def _gold(conn: Any) -> Any:
    return await md.instrument_by_key(conn, hfm_demo.GOLD)


async def test_the_robot_decides_the_moment_a_candle_completes(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S87: between its every-second checks the worker looks for a newly complete 1-minute
    candle ten times a second; the moment there is one the robot decides, and its order is at
    HFM, with no other step of the worker run."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        m = await _armed(h, settings, db)
        seen = await gold_jobs._newest(h.ctx)
        watching = asyncio.create_task(gold_jobs.watch_candles(h.ctx, seen, time.monotonic() + 20))
        await asyncio.sleep(0.3)
        assert not emu.positions  # nothing new yet: nothing decided
        await _setup(w)  # HFM's candles, with a setup on the newest
        for _ in range(400):
            if emu.positions:
                break
            await asyncio.sleep(0.02)
        watching.cancel()
        (pos,) = emu.positions.values()
        assert (pos.sl, pos.tp) == (D("4098.40"), D("4104.30"))
        d = await _last(db)
        assert d.action == "PROPOSED" and d.mandate_id == m.id
        assert await gold_jobs._newest(h.ctx) == d.bar != seen


async def test_an_order_that_cannot_leave_within_a_second_is_not_sent(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S87: late is never sent. The server refuses to hand over an order whose second has
    passed, and the robot opens nothing while the fast path is not up, before the owner's
    approval is used."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        m = await _armed(h, settings, db)
        await _setup(w)
        sent = len(emu.sent)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:PROPOSED"]
        h.env.clock.advance(milliseconds=1200)  # the order's step comes 1.2 s late
        emu.now_ms += 1200
        await asyncio.sleep(0.1)
        await h.env.svc.execution.execute_approved()
        await _cycles(h)
        entry, events = await _entry_events(db)
        assert entry.status == "REJECTED" and "too late" in events["REJECTED"]["reason"]
        assert len(emu.sent) == sent and not emu.positions  # nothing reached HFM


async def test_the_robot_opens_nothing_while_the_fast_path_is_not_up(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S87: with a bridge before version 8 an order cannot leave within a second (it waits for
    the next report): NO TRADE, said in the decision, and the owner's approval keeps waiting."""
    monkeypatch.setattr("bridge.cosmic_mt5_bridge.VERSION", "7")
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        m = await _armed(h, settings, db)
        await _setup(w)
        sent = len(emu.sent)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:WAIT"]
        why = (await _last(db)).reasons[-1]
        assert why.startswith("NO TRADE — an order could not leave for HFM within one second")
        assert "must be version 8 (the fast path); it is version 7" in why
        async with db.connect() as conn:
            (ticket,) = list(await conn.execute(select(robot_tickets)))
            # the path's own words, from its state
            now = h.env.clock.now()
            assert await hfm_demo.fast_path_problem(conn, now, 8) == (
                "the bridge's fast path has not connected yet"
            )
        assert ticket.ended is None and len(emu.sent) == sent  # the approval still waits
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            home = (await c.get("/")).text
        assert "Not possible now: the HFM bridge on the Windows computer must be version 8" in home
        assert "No order measured yet." in home


async def test_with_a_bridge_before_version_7_the_rule_opens_nothing(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bridge.cosmic_mt5_bridge.VERSION", "6")  # it cannot send a take-profit
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await _ready(h, settings)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            assert (await _start(c)).status_code == 303
            m = await _robot(db, h)
            assert (await _approve_next(c, m)).status_code == 303
        sent = len(emu.sent)
        await _setup(w)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:WAIT"]
        why = (await _last(db)).reasons[-1]
        assert why.startswith("NO TRADE — the HFM bridge") and "it is version 6" in why
        assert len(emu.sent) == sent
        async with db.connect() as conn:  # the approval was not used up
            assert await predictions.waiting(conn, m.id, h.env.clock.now(), expire=False)


@pytest.mark.parametrize(
    ("calibration", "permission", "first", "said"),
    [
        ((0.5, 0.0), "AUTO", "PROPOSED", "opened by your rule (50 % or more)"),  # 62 %
        ((-0.6, 0.0), "AUTO", "WAIT", "ASK — BUY at 35.4% needs your approval"),  # 35 %
        ((0.5, 0.0), "ASK", "WAIT", "ASK — BUY at 62.2% needs your approval"),  # Ask me first
    ],
)
async def test_the_owners_bands_decide_automatic_at_50_and_ask_below(
    settings: Settings,
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calibration: tuple[float, float],
    permission: str,
    first: str,
    said: str,
) -> None:
    """With a model that says 62 % or 35 % (a test model: the real one never does)."""
    model = replace(probability.MODEL, version="test-model", calibration=calibration)
    monkeypatch.setattr(probability, "MODEL", model)
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await _ready(h, settings)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            assert (await _start(c, permission=permission)).status_code == 303
            m = await _robot(db, h)
            await _setup(w)
            assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:{first}"]
            assert said in (await _last(db)).reasons[-1]
            if first == "PROPOSED":
                await _cycles(h)
                (pos,) = emu.positions.values()
                assert (pos.sl, pos.tp) == (D("4098.40"), D("4104.30"))
                async with db.connect() as conn:
                    details = await conn.scalar(select(trade_intents.c.details))
                    assert (await conn.execute(select(robot_tickets))).first() is None
                assert details["practice"] is False and details["band"] == "AUTO"
                return
            async with db.connect() as conn:  # the owner is told, at most once an hour
                told = [
                    n.payload["text"]
                    for n in await conn.execute(
                        select(notifications.c.payload).where(notifications.c.kind == "gold_sim")
                    )
                ]
            assert any("needed your approval and none was waiting" in t for t in told)
            assert not emu.positions
            assert (await _approve_next(c, m)).status_code == 303
            home = (await c.get("/")).text  # the approval waits: the button gives way to it
            assert "Your approval is waiting" in home and "/gold/robot/approve-next" not in home
        await _minute(h, w)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:PROPOSED"]
        await _cycles(h)
        assert len(emu.positions) == 1
        async with db.connect() as conn:
            details = await conn.scalar(select(trade_intents.c.details))
            (ticket,) = list(await conn.execute(select(robot_tickets)))
        assert details["practice"] is False  # 30 % or more: a trade, not a practice trade
        assert (ticket.ended, ticket.practice) == ("USED", False)


T = WEDNESDAY


def _row(bid: str, ask: str) -> Any:
    return SimpleNamespace(bid=D(bid), ask=D(ask), bar=T)


def _bars(*hl: tuple[str, str]) -> list[Candle]:
    return [
        Candle(T + timedelta(minutes=k + 1), D(lo), D(hi), D(lo), D(hi), D(1), True)
        for k, (hi, lo) in enumerate(hl)
    ]


def test_an_outcome_is_what_hfms_candles_did_to_the_models_trade_shape() -> None:
    m, p, now = probability.MODEL, _row("4100.00", "4100.30"), T + timedelta(minutes=10)
    quiet = ("4100.80", "4099.60")
    # A buy: the bid must reach 4104.30 (the ask + 4.00) before it falls to 4098.40.
    up = _bars(quiet, ("4104.30", "4100.00"))
    assert predictions._outcome("BUY", p, m, up, now) == ("TP", T + timedelta(minutes=3), 2)
    down = _bars(quiet, ("4100.10", "4098.40"))
    assert predictions._outcome("BUY", p, m, down, now) == ("SL", T + timedelta(minutes=3), 2)
    both = _bars(("4104.50", "4098.30"))  # both inside one candle: counted as the stop
    assert predictions._outcome("BUY", p, m, both, now)[0] == "SL"
    # A short closes at the ask: its stop is hit when the bid reaches 4101.60, its target when
    # the bid falls to 4095.70 (a spread below 4096.00).
    assert predictions._outcome("SELL", p, m, _bars(("4101.60", "4100.00")), now)[0] == "SL"
    assert predictions._outcome("SELL", p, m, _bars(("4100.40", "4095.70")), now)[0] == "TP"
    assert predictions._outcome("SELL", p, m, _bars(("4100.40", "4095.71")), now) is None
    assert predictions._outcome("BUY", p, m, _bars(quiet), now) is None  # undecided so far
    late = T + timedelta(minutes=246)
    assert predictions._outcome("BUY", p, m, _bars(quiet), late) == ("NONE", late, 240)


async def test_predictions_are_stored_once_a_candle_and_scored_without_a_trade(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        sent = len(emu.sent)  # the account's opening check placed and cancelled one order
        await _setup(w)
        now = h.env.clock.now()
        await predictions.tick(db, now)  # no robot runs: the forward test does not need one
        await predictions.tick(db, now)
        async with db.connect() as conn:
            (p,) = list(await conn.execute(select(gold_predictions)))
            assert (await predictions.accuracy(conn)).scored == 0
        assert p.setup == "BUY" and p.bar == now - timedelta(minutes=1)
        # Three minutes later HFM's candles have risen past the buy's target (and so past the
        # short's stop): both outcomes are known, and no order was ever sent.
        h.env.clock.advance(minutes=3)
        emu.now_ms += 180_000
        await asyncio.sleep(0.1)
        await w.candles("1m", [*_turn(), 4105.0, 4105.0, 4105.0][3:], 0.3)
        async with db.transaction() as conn:
            assert await predictions.score(conn, h.env.clock.now()) == 2
            assert await predictions.score(conn, h.env.clock.now()) == 0  # once
        async with db.connect() as conn:
            got = {
                o.side: (o.outcome, o.minutes)
                for o in await conn.execute(select(gold_prediction_outcomes))
            }
            acc = await predictions.accuracy(conn)
        assert got == {"BUY": ("TP", 1), "SELL": ("SL", 1)}
        assert (acc.scored, acc.happened, acc.setups, acc.setups_happened) == (
            2,
            D("0.5000"),
            1,
            D("1.0000"),
        )
        assert acc.predicted is not None and D("0.26") <= acc.predicted <= D("0.28")
        assert len(emu.sent) == sent  # nothing was sent to HFM to learn it
        # A prediction is never rewritten: the application's role may not even try.
        with pytest.raises(DBAPIError, match="permission denied for table gold_predictions"):
            async with db.transaction() as conn:
                await conn.execute(update(gold_predictions).values(p_buy=D("0.9")))


async def test_an_approval_in_advance_is_one_at_a_time_lapses_and_is_sealed(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    clock = h.env.clock
    async with bridge_running(db, clock, emu, tmp_path):
        await _ready(h, settings)
        app = create_app(settings, db=db, clock=clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, clock)
            assert (await _start(c)).status_code == 303
        m = await _robot(db, h)
        async with db.transaction() as conn:
            first = await predictions.arm(conn, m, actor=OWNER, now=clock.now())
            with pytest.raises(ConflictError, match="already waiting"):
                await predictions.arm(conn, m, actor=OWNER, now=clock.now())
            assert await predictions.cancel(conn, m.id, clock.now())
            assert not await predictions.cancel(conn, m.id, clock.now())
            second = await predictions.arm(conn, m, actor=OWNER, now=clock.now())
        clock.advance(minutes=61)
        async with db.transaction() as conn:
            assert await predictions.waiting(conn, m.id, clock.now()) is None  # lapsed
        async with db.connect() as conn:
            ended = {t.id: t.ended for t in await conn.execute(select(robot_tickets))}
        assert ended == {first: "CANCELLED", second: "EXPIRED"}
        with pytest.raises(DBAPIError, match="sealed"):
            async with db.transaction() as conn:
                await conn.execute(text("UPDATE robot_tickets SET ended = NULL, ended_at = NULL"))
