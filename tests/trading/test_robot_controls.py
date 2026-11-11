"""Robot controls (S79, the P0 demo directive of 2026-11-11), on the MT5 emulator: the owner's
rules and profit goals for the HFM robot, its status, and the safe shutdown that closes only its
own trades at market and records the final net result."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events
from cosmic.db.schema_execution import trade_intents
from cosmic.gold import controls, hfm_demo
from cosmic.reporting import gold_training, robot_panel
from cosmic.trading import mandates
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_hfm_demo import _cycles, _decisions, _grant, _no_hfm_fee
from tests.trading.test_gold_sim import World
from tests.trading.test_hfm import Hfm, bridge_running, build, open_account

pytestmark = pytest.mark.keeps_binance_paused

AT = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def _mandate(daily_loss: str = "6") -> mandates.Mandate:
    return mandates.Mandate(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        strategy_key=mandates.GOLD_READING,
        timeframe=mandates.HOURLY,
        instruments=(hfm_demo.GOLD,),
        max_loss_per_trade=D(2),
        max_trades_per_day=5,
        max_open_positions=1,
        daily_loss_limit=D(daily_loss),
        status="ACTIVE",
        granted_by=uuid.uuid4(),
        granted_at=AT,
        expires_at=AT.replace(year=2027),
        ended_at=None,
        end_reason=None,
    )


def test_the_rules_judge_the_loss_limit_the_goals_and_the_losing_streak() -> None:
    m = _mandate()
    c = controls.Controls(m.id, daily_target=D(8), session_target=D(20), long_target=D(50))
    calm = controls.Results(D(1), D(1), D(1), D(0), 0)
    assert controls.judge(m, c, calm) is None
    lost = controls.Results(D(-6), D(-6), D(-6), D(0), 3)
    assert controls.judge(m, c, lost) == (
        controls.LOSS_LIMIT,
        "today's net realized result -6.00 reached the daily loss limit of 6.00",
    )
    won = controls.Results(D(8), D(8), D(8), D(0), 0)
    verdict = controls.judge(m, c, won)
    assert verdict is not None and verdict[0] == controls.TARGET_REACHED
    assert "the daily profit goal of 8.00 is reached (net realized: 8.00)" in verdict[1]
    # Floating counts only when the owner chose it.
    floating = controls.Results(D(5), D(5), D(5), D(4), 0)
    assert controls.judge(m, c, floating) is None
    with_floating = controls.Controls(m.id, daily_target=D(8), measure=controls.WITH_FLOATING)
    verdict = controls.judge(m, with_floating, floating)
    assert verdict is not None and "net realized plus floating: 9.00" in verdict[1]
    streak = controls.Results(D(-4), D(-4), D(-4), D(0), 3)
    verdict = controls.judge(m, c, streak)
    assert verdict is not None and verdict[0] == controls.PAUSED
    paused = controls.Controls(m.id, status=controls.PAUSED)
    assert controls.judge(m, paused, streak) is None  # already paused
    assert controls.judge(m, paused, lost) is not None  # the loss limit still shuts it
    # The day ceiling: 4 lost + a 2.00 trade fits 6; 4.50 lost does not.
    assert controls.day_room(m, controls.Results(D(-4), D(-4), D(-4), D(0), 2), D(2)) is None
    why = controls.day_room(m, controls.Results(D("-4.5"), D(0), D(0), D(0), 0), D(2))
    assert why is not None and "would take today past the daily limit of 6.00" in why
    assert controls.check(controls.Controls(m.id, pause_after_losses=0, take_profit_r=D(20))) == [
        "Pause after losses in a row: 1 to 20.",
        "Take-profit: 0.5 to 10 times the risk.",
    ]


async def _later(h: Hfm, w: World, minutes: int = 1) -> None:
    h.env.clock.advance(minutes=minutes)
    h.emu.now_ms += minutes * 60_000  # HFM's clock in step with the server's
    await asyncio.sleep(0.1)  # the bridge's next report carries the new time
    await w.market("BUY")


async def test_a_profit_goal_closes_only_its_trades_at_market_and_stops_the_robot(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h)  # 20,000 at 0.4 %: 80.00 a trade
        async with db.transaction() as conn:
            await controls.create(
                conn,
                controls.Controls(m, daily_target=D(10), measure=controls.WITH_FLOATING),  # type: ignore[arg-type]
                now=h.env.clock.now(),
                actor=Actor.system("test"),
            )
        await w.market("BUY")
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:PROPOSED"]
        (d,) = await _decisions(h)
        async with db.connect() as conn:
            plan = await conn.scalar(
                select(trade_intents.c.management_plan).where(trade_intents.c.id == d.intent_id)
            )
        # The whole risk of an ounce: the 30.00 stop and the 0.1 % limit margin (4.11), so the
        # target is 2 × 34.11 above the ask: 1:2 in money (S79).
        assert d.ounces == D(2) and D(str(plan["take_profit_price"])) == D("4168.52")
        await _cycles(h)
        (pos,) = emu.positions.values()
        assert pos.volume == D("0.02")
        async with db.connect() as conn:  # the robot cockpit (S82) while the trade is open
            panel = await robot_panel.build(conn, h.env.clock.now())
        (robot,) = panel.robots
        (open_trade,) = robot.open
        assert (robot.status, robot.demo, robot.broker_ok, robot.mt5_ok) == (
            "RUNNING",
            True,
            True,
            True,
        )
        assert (open_trade.side, open_trade.ounces, open_trade.stop, open_trade.take_profit) == (
            "BUY",
            D(2),
            D("4070.30"),
            D("4168.52"),
        )
        assert open_trade.at_risk == D("60.00") and robot.at_risk == D("60.00")
        assert robot.goal == ("daily", D(10), open_trade.floating) and robot.trades_today == 1
        emu.move("XAUUSD", "4110.00", "4110.30")  # 2 oz × 9.70 floating: past the 10.00 goal
        await _later(h, w)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:EXIT"]
        async with db.connect() as conn:
            c = await controls.get(conn, m)  # type: ignore[arg-type]
        assert c is not None and c.status == controls.TARGET_REACHED
        assert "the daily profit goal of 10.00 is reached" in c.status_reason
        await _cycles(h)
        assert not emu.positions  # closed at HFM, at market
        assert any(x["position_id"] == pos.ticket for x in emu.deals if x["entry"] == 1)
        for _ in range(20):  # HFM's closing deal booked: the exit is done, nothing in flight
            await _cycles(h, 1)
            async with db.connect() as conn:
                closing = await conn.scalar(
                    select(trade_intents.c.status).where(trade_intents.c.effect == "CLOSE")
                )
            if closing == "EXECUTED":
                break
        assert closing == "EXECUTED"
        await _later(h, w)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:WAIT"]  # no new entry
        *_, last = await _decisions(h)
        assert (
            "target reached" in last.reasons[-1] and "only when you resume it" in last.reasons[-1]
        )
        async with db.connect() as conn:
            c = await controls.get(conn, m)  # type: ignore[arg-type]
            actions = [
                r.action
                for r in await conn.execute(
                    select(audit_events.c.action)
                    .where(audit_events.c.action.like("robot.%"))
                    .order_by(audit_events.c.occurred_at)
                )
            ]
        assert c is not None and c.final_net is not None and c.final_net > 0
        assert actions == ["robot.controls_set", "robot.target_reached", "robot.final_result"]
        assert await h.reconcile() == ()
        # DEMO TRAINING MODE (S80): the closed trade with its execution and market data.
        async with db.connect() as conn:
            rep = await gold_training.build(conn, h.env.clock.now())
        (t,) = rep.trades
        assert (t.rule, t.side, t.ounces, t.outcome, t.entry) == (
            "gold_reading",
            "BUY",
            D(2),
            "WIN",
            D("4100.30"),
        )
        assert t.net == c.final_net and t.spread == D("0.30")
        assert t.take_profit_distance == D("68.22") and t.planned_risk is not None
        assert t.mae is not None and t.mae <= 0 and t.mfe is not None and t.mfe >= 0
        assert t.rule_version.startswith("gold_reading v1") and "trend" in t.regime
        assert (rep.count, rep.win_rate) == (1, D("100.0")) and "not shown" in rep.sharpe_why


async def test_the_owner_sets_the_goals_pauses_and_resumes_the_robot_in_the_portal(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _no_hfm_fee(h)
        await w.market("BUY")
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/gold/sim")).text
            start = {
                "csrf_token": csrf_from(page),
                "mode": "HFM_DEMO",
                "amount": "20000",
                "automation": "ON",
                "permission": "ASK",
                "risk_pct": "0.4",
            }
            assert (await c.post("/gold/sim/start", data=start)).status_code == 303
            async with db.connect() as conn:
                (m,) = await hfm_demo.gold_mandates(conn, h.env.clock.now())
                ctl = await controls.get(conn, m.id)
            assert ctl is not None and (ctl.status, ctl.pause_after_losses) == ("RUNNING", 3)
            page = (await c.get("/gold/sim")).text
            assert "Rules, goals and results" in page and "PAUSE (open trades keep" in page
            rules = {
                "csrf_token": csrf_from(page),
                "mandate_id": str(m.id),
                "pause_after_losses": "3",
                "take_profit_r": "2",
                "daily_target": "4",
                "session_target": "",
                "long_target": "50",
                "measure": "REALIZED",
            }
            r = await c.post("/gold/robot/rules", data=rules)
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?rules=saved"
            bad = await c.post("/gold/robot/rules", data={**rules, "take_profit_r": "20"})
            assert bad.status_code == 422 and "0.5 to 10 times the risk" in bad.text
            pause = {"csrf_token": csrf_from(page), "mandate_id": str(m.id)}
            assert (await c.post("/gold/robot/pause", data=pause)).status_code == 303
            assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m.id}:WAIT"]
            *_, d = await _decisions(h)
            assert d.reasons[-1] == "WAIT — paused: paused by the owner"
            assert (await c.post("/gold/robot/resume", data=pause)).status_code == 303
            async with db.connect() as conn:
                ctl = await controls.get(conn, m.id)
            assert ctl is not None
            assert (ctl.status, ctl.daily_target, ctl.long_target) == ("RUNNING", D(4), D(50))
            assert ctl.session_started_at == h.env.clock.now()  # a new session from the resume
            home = (await c.get("/")).text
            for shown in ("Robot cockpit", "DEMO ACCOUNT", "START DEMO", "EMERGENCY STOP"):
                assert shown in home, shown
            page = await c.get("/gold/training")
            assert page.status_code == 200 and "DEMO TRAINING MODE" in page.text
            csv_ = await c.get("/gold/training.csv")
            assert csv_.status_code == 200 and csv_.text.startswith("opened_utc,closed_utc,")


def test_the_training_report_counts_wins_losses_and_needs_20_days_for_a_sharpe() -> None:
    def trade(net: str, hour: int, regime: str) -> gold_training.Trade:
        at = AT.replace(hour=hour)
        return gold_training.Trade(
            intent_id=uuid.uuid4(),
            rule="gold_reading",
            rule_version="v1",
            timeframe="1h",
            side="BUY",
            opened_at=at,
            closed_at=at,
            ounces=D("0.1"),
            entry=D(4100),
            exit=D(4120),
            spread=D("0.35"),
            stop_distance=D(20),
            take_profit_distance=D(40),
            planned_risk=D(2),
            net=D(net),
            r_multiple=D(net) / 2,
            slippage_bps=D(0),
            mae=D(-1),
            mfe=D(4),
            signal="x",
            regime=regime,
            session=gold_training.session_of(at),
            ai_version="none",
        )

    rep = gold_training.report(
        [trade("4", 8, "UP trend"), trade("-2", 13, "UP trend"), trade("4", 17, "DOWN trend")],
        [("WAIT — paused", 2)],
    )
    assert (rep.count, rep.win_rate, rep.profit_factor, rep.expectancy) == (
        3,
        D("66.7"),
        D("4.00"),
        D("2.00"),
    )
    assert (rep.average_win, rep.average_loss, rep.max_drawdown, rep.expectancy_r) == (
        D("4.00"),
        D("-2.00"),
        D(2),
        D("1.00"),
    )
    assert rep.sharpe is None and "it needs 20" in rep.sharpe_why
    assert rep.by_regime["UP trend"]["count"] == 2 and rep.by_regime["DOWN trend"]["net"] == D(4)
    assert set(rep.by_session) == {
        "London (07–12 UTC)",
        "London + New York (12–16 UTC)",
        "New York (16–21 UTC)",
    }
    assert gold_training.regime_of(["H1 trend UP", "D1 trend UP"], D(20), D(4100)) == (
        "UP trend, normal market"
    )
    assert gold_training.regime_of([], D(10), D(4100)) == "unknown trend, quiet market"
