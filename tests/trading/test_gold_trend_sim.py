"""Robot A, the TEST robot, in the simulation (S102; the owner's master specification of
2026-11-18): 100 USD of simulated money on HFM's prices, the lab's TEST rule unchanged, 1 USD a
trade, 4 USD a day; before each trade its measured probability, expected value, reward to risk,
reasons for and against, and what the connected HFM demo would need; at 36.6 % it asks the
owner. It runs beside the gold reading's simulation and never stops it."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import ROUND_DOWN
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, notifications
from cosmic.db.schema_gold import gold_sim_accounts, gold_sim_decisions
from cosmic.gold import probability, sim, trend_buy, trend_sim
from cosmic.sources.ingest import ensure_registry
from cosmic.trading.policy import ensure_default_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user
from tests.trading.test_gold_sim import HFM_GOLD, WED, World

pytestmark = pytest.mark.keeps_binance_paused

TURN_UP = [4400.0] * 60 + [4400 - 1.5 * k for k in range(1, 8)] + [4400.0]
RISING_HOURS = [4000.0 + k for k in range(400)]


@pytest.fixture
async def w(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> World:
    # The connected demo's own gold specification, as its report gives it: 0.01 lot of 100 oz.
    monkeypatch.setitem(HFM_GOLD, "volume_min", "0.01")
    monkeypatch.setitem(HFM_GOLD, "volume_step", "0.01")
    clock = FrozenClock(WED)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    owner = await make_user(db, clock)
    world = World(db, clock, owner.user_id)
    await world.hfm("4040.00", "4040.30")
    return world


async def _signal(w: World) -> None:
    """Both trends up and the 15-minute momentum just turned up: the TEST rule's BUY."""
    await w.candles("1h", RISING_HOURS, 3.0)
    await w.candles("15m", TURN_UP, 3.0)


async def _start(w: World) -> uuid.UUID:
    async with w.db.transaction() as conn:
        return await trend_sim.start(conn, owner=w.owner, now=w.clock.now())


async def _decision(w: World, account: uuid.UUID) -> Any:
    async with w.db.connect() as conn:
        return (
            await conn.execute(
                select(gold_sim_decisions)
                .where(gold_sim_decisions.c.account_id == account)
                .order_by(gold_sim_decisions.c.bar.desc())
                .limit(1)
            )
        ).one()


async def test_robot_a_asks_before_a_simulated_buy_of_1_usd_and_says_what_the_demo_needs(
    w: World,
) -> None:
    other = await w.account("100")  # the gold reading's simulation, running
    robot = await _start(w)
    async with w.db.connect() as conn:
        assert await sim.running_accounts(conn) == [other]  # each rule its own
        assert await sim.running_accounts(conn, trend_sim.RULE) == [robot]
    await _signal(w)
    assert await trend_sim.decide(w.db, w.clock.now()) == [f"{robot}:ASKED"]
    assert await trend_sim.decide(w.db, w.clock.now()) == []  # once a 15-minute candle
    d = await _decision(w, robot)
    assert (d.decision, d.action) == ("BUY", "ASKED")
    ev = d.evaluation
    assert (ev["mode"], ev["p_win"], ev["p_loss"], ev["band"]) == (
        "SIMULATED",
        "0.366",
        "0.546",
        probability.ASK,
    )
    assert ev["reward_to_risk"] == "1:2" and ev["confidence"] == "LOW"
    # Calibrated on markets as volatile as this one (its stop now, about 12.5: "normal").
    assert d.stop_distance is not None and D("9.46") <= d.stop_distance < D("15.26")
    assert ev["regime"] == {
        "name": "normal",
        "where": "stop 9.46 to 15.26",
        "p_win": "0.347",
        "expected_r": "0.166",
        "trades": 124,
    }
    assert (ev["recent"]["name"], ev["recent"]["p_win"]) == ("2026", "0.294")
    assert D(ev["risk_usd"]) <= D(1) and D(ev["ev_usd"]) == (
        D("0.166") * D(ev["risk_usd"])
    ).quantize(D("0.01"))
    assert any("4-hour trend is UP" in x for x in ev["for"])
    assert any("below 50 %" in x for x in ev["against"])
    assert any(x.startswith("in 2026 it won 29.4 % and lost 0.11 R") for x in ev["against"])
    # The connected demo's smallest trade, 1 ounce, would risk far more than 2 USD: refused there.
    assert ev["demo"]["step_oz"] == "1"
    assert D(ev["demo"]["risk_at_step_usd"]) > 2
    assert ev["demo"]["verdict"].startswith("refused on the connected HFM demo")
    assert "Approve or reject it on the gold page within 5 minutes" in d.reasons[-1]
    async with w.db.connect() as conn:
        told = await conn.scalar(
            select(notifications.c.payload["text"].astext)
            .where(notifications.c.kind == "gold_sim")
            .order_by(notifications.c.created_at.desc())
            .limit(1)
        )
    assert told is not None and told.startswith("MARKET OPPORTUNITY — SIMULATED · Robot A (TEST)")
    assert "Win 34.7 % in normal markets like this one (36.6 % over its 372 trades" in told
    assert "29.4 % in 2026" in told
    # Approved: it opens at HFM's ask, 1 USD at the stop with costs, the rule's own levels.
    async with w.db.transaction() as conn:
        said = await sim.answer(conn, d.id, True, w.owner, w.clock.now())
    assert said.startswith("opened: BUY")
    (p,) = await w.positions(robot)
    distance = d.stop_distance
    per_ounce = distance + 2 * D("0.30")  # the stop plus a spread at entry and at the stop
    assert p.ounces == (D(1) / per_ounce / D("0.01")).to_integral_value(ROUND_DOWN) * D("0.01")
    assert (p.entry_price, p.stop_price) == (D("4040.30"), D("4040.30") - distance)
    assert p.take_profit_price == D("4040.30") + 2 * distance
    assert await w.positions(other) == []  # the other simulation is not touched


async def test_robot_a_closes_after_24_hours_and_counts_the_days_trades(w: World) -> None:
    robot = await _start(w)
    await _signal(w)
    await trend_sim.decide(w.db, w.clock.now())
    d = await _decision(w, robot)
    async with w.db.transaction() as conn:
        await sim.answer(conn, d.id, True, w.owner, w.clock.now())
    w.clock.advance(hours=24)
    await w.hfm("4045.00", "4045.30")
    await w.candles("1h", RISING_HOURS, 3.0)
    await w.candles("15m", [4400.0] * 70, 3.0)  # no new signal
    assert await trend_sim.decide(w.db, w.clock.now()) == [f"{robot}:WAIT"]
    (p,) = await w.positions(robot)
    assert (p.status, p.exit_reason, p.exit_price) == ("CLOSED", "RULE", D("4045.00"))
    last = await _decision(w, robot)
    assert any("after 24 hours, as the rule says" in r for r in last.reasons)
    async with w.db.connect() as conn:
        today = await trend_sim.today(conn, robot, w.clock.now())
    assert (today.completed, today.target) == (1, 10)
    assert today.short_of_target is not None and "never forced" in today.short_of_target


def test_the_rules_record_by_market_volatility() -> None:
    """By the stop it uses (2 × ATR14 of the 15-minute candles): its record in thirds of its
    372 trades. In volatile markets it made nothing: the owner sees that before a trade."""
    assert trend_sim.regime(D("8")).name == "calm"
    assert trend_sim.regime(D("9.46")).name == "normal"
    volatile = trend_sim.regime(D("16.2"))  # gold on 2026-11-18
    assert (volatile.name, volatile.p_win, volatile.expected_r) == (
        "volatile",
        D("0.306"),
        D("-0.002"),
    )
    assert trend_sim.band(volatile.p_win, volatile.expected_r) == probability.ASK
    assert sum(r.trades for r in trend_sim.REGIMES) == trend_sim.TRADES


def test_the_owners_bands_and_rules() -> None:
    assert trend_sim.band(D("0.366"), D("0.177")) == probability.ASK
    assert trend_sim.band(D("0.55"), D("0.1")) == probability.AUTO
    assert trend_sim.band(D("0.55"), D("-0.1")) == probability.ASK  # no positive value: asks
    assert trend_sim.band(D("0.25"), D("1")) == probability.REJECT
    s = trend_sim.settings()
    assert (s.amount, s.risk_pct, s.daily_loss, s.allow_sell, s.max_positions) == (
        D(100),
        D(1),
        D(4),
        False,
        1,
    )
    assert timedelta(hours=24) == trend_buy.HOLD


async def test_the_owner_starts_robot_a_approves_its_trade_and_switches_it_off(
    w: World, settings: Settings, db: Database
) -> None:
    other = await w.account("100")
    app = create_app(settings, db=db, clock=w.clock)
    owner = await make_user(db, w.clock, username="gold-owner-a")
    async with client_for(app) as c:
        await full_login(c, owner, w.clock)
        page = (await c.get("/gold/sim")).text
        assert "Robot A — TEST robot" in page and "START Robot A in the simulation" in page
        r = await c.post("/gold/testsim/start", data={"csrf_token": csrf_from(page)})
        assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=testsim"
        page = (await c.get("/gold/sim?started=testsim")).text
        assert "✓ Robot A started in the simulation" in page and "Trades today: 0 of 10" in page
        async with db.connect() as conn:
            (robot,) = await sim.running_accounts(conn, trend_sim.RULE)
            assert await sim.running_accounts(conn) == [other]
        await _signal(w)
        await trend_sim.decide(db, w.clock.now())
        page = (await c.get("/gold/sim")).text
        assert "Robot A asks you" in page and "Approve this simulated trade" in page
        d = await _decision(w, robot)
        r = await c.post(
            "/gold/sim/answer",
            data={"csrf_token": csrf_from(page), "decision": str(d.id), "answer": "APPROVE"},
        )
        assert r.status_code == 303
        assert len(await w.positions(robot)) == 1
        page = (await c.get("/gold/sim")).text
        assert "Open: BUY" in page and "Robot A OFF" in page
        r = await c.post("/gold/testsim/off", data={"csrf_token": csrf_from(page)})
        assert r.status_code == 303
        page = (await c.get("/gold/sim")).text  # its record, the closed trade in it (S104)
        assert "Its record: the last 20 and 50 completed trades" in page
        assert "NORMAL: the high-performance mode needs 50 completed trades" in page
    async with db.connect() as conn:
        status = await conn.scalar(
            select(gold_sim_accounts.c.status).where(gold_sim_accounts.c.id == robot)
        )
        actions = [
            a
            for (a,) in await conn.execute(
                select(audit_events.c.action).where(audit_events.c.action.like("gold_sim.test_%"))
            )
        ]
        assert await sim.running_accounts(conn) == [other]  # still running
    assert status == "STOPPED"
    (p,) = await w.positions(robot)
    assert (p.status, p.exit_reason) == ("CLOSED", "OWNER")
    assert sorted(actions) == ["gold_sim.test_robot_off", "gold_sim.test_robot_started"]


def _closed(results: list[str], *, risk_oz: str = "0.10", stop: str = "10") -> list[sim.Position]:
    """Closed BUYs, newest first (as ``sim.positions`` lists them), each risking 1.00 at its stop."""
    out = []
    for k, r in enumerate(results):
        out.append(
            sim.Position(
                uuid.uuid4(),
                "BUY",
                D(risk_oz),
                D("4000"),
                WED - timedelta(hours=k),
                D("4000") - D(stop),
                D("4000") + 2 * D(stop),
                D("0.30"),
                D(0),
                "CLOSED",
                result=D(r),
            )
        )
    return out


def test_the_rolling_record_of_the_last_20_and_50_trades() -> None:
    """S104, the owner's section 8: win rate, profit factor, expectancy (USD and R), the worst
    drop and the net, over the last 20 and 50 completed trades; display only."""
    # newest first: +2, -1, -1, +2, -1 (oldest)
    w = trend_sim.window(_closed(["2", "-1", "-1", "2", "-1"]), 20)
    assert (w.trades, w.net, w.win_rate) == (5, D("1.00"), D("0.400"))
    assert w.profit_factor == D("1.33") and w.expectancy == D("0.20")
    assert w.expectancy_r == D("0.200")  # each trade risked 1.00 at its stop
    assert w.max_drawdown == D("2.00")  # oldest first: -1, +2, -1, -1 (peak 1, then -1), +2
    assert trend_sim.window([], 20).trades == 0
    assert trend_sim.window(_closed(["1"] * 30), 20).trades == 20  # the last 20 only
    few = trend_sim.Performance(w, w)
    assert few.mode.startswith("NORMAL: the high-performance mode needs 50 completed trades")
    losing = trend_sim.window(_closed(["-1"] * 40 + ["2"] * 10), 50)  # -20 net
    assert trend_sim.Performance(losing, losing).mode.startswith("CAUTION")
    usual = trend_sim.window(_closed(["2"] * 20 + ["-1"] * 30), 50)
    assert trend_sim.Performance(usual, usual).mode.startswith("NORMAL: 40.0 % won")
    high = trend_sim.window(_closed(["2"] * 41 + ["-1"] * 9), 50)
    said = trend_sim.Performance(high, high).mode
    assert said.startswith("HIGH PERFORMANCE: 82.0 % won") and "risk a trade stays the same" in said


async def _auto(w: World, robot: uuid.UUID) -> None:
    async with w.db.transaction() as conn:
        await trend_sim.set_permission(conn, robot, True, w.owner, w.clock.now())


async def test_approved_once_robot_a_opens_by_itself_where_its_record_made_money(w: World) -> None:
    """The owner (2026-11-18): "if one time I approve ... automatically start trade". In a market
    like those where the rule made money (here "normal", +0.166 R a trade), it opens by itself."""
    robot = await _start(w)
    await _auto(w, robot)
    await _signal(w)
    assert await trend_sim.decide(w.db, w.clock.now()) == [f"{robot}:BUY"]
    d = await _decision(w, robot)
    assert d.action == "OPENED" and d.evaluation["regime"]["name"] == "normal"
    assert d.reasons[-1].endswith("Opened by itself: you approved Robot A's trades in advance")
    (p,) = await w.positions(robot)
    assert p.status == "OPEN" and p.take_profit_price == D("4040.30") + 2 * d.stop_distance
    async with w.db.connect() as conn:
        told = await conn.scalar(
            select(notifications.c.payload["text"].astext)
            .where(notifications.c.kind == "gold_sim")
            .order_by(notifications.c.created_at.desc())
            .limit(1)
        )
        s = await sim.settings_of(conn, robot)
    assert told is not None and told.startswith("OPENED — SIMULATED · Robot A (TEST)")
    assert s is not None and (s.permission, s.risk_pct, s.daily_loss) == ("AUTO", D(1), D(4))


async def test_approved_once_it_still_asks_where_its_record_made_nothing(w: World) -> None:
    """In volatile markets (a stop of 15.26 or more) the rule made −0.002 R a trade: the owner's
    rule opens by itself only with a positive expected value, so it asks, and says why."""
    robot = await _start(w)
    await _auto(w, robot)
    await w.candles("1h", RISING_HOURS, 3.0)
    await w.candles("15m", TURN_UP, 4.6)  # wider candles: a stop above 15.26
    assert await trend_sim.decide(w.db, w.clock.now()) == [f"{robot}:ASKED"]
    d = await _decision(w, robot)
    assert d.stop_distance >= D("15.26") and d.evaluation["regime"]["name"] == "volatile"
    assert (
        "It asks although you approved its trades in advance: in volatile markets"
        in (d.reasons[-1])
    )
    assert await w.positions(robot) == []


async def test_the_owner_approves_robot_as_trades_once_and_takes_it_back(
    w: World, settings: Settings, db: Database
) -> None:
    app = create_app(settings, db=db, clock=w.clock)
    owner = await make_user(db, w.clock, username="gold-owner-b")
    async with client_for(app) as c:
        await full_login(c, owner, w.clock)
        page = (await c.get("/gold/sim")).text
        await c.post("/gold/testsim/start", data={"csrf_token": csrf_from(page)})
        page = (await c.get("/gold/sim")).text
        assert (
            "Asks you before each trade." in page
            and "Approve once: Robot A trades by itself" in page
        )
        r = await c.post("/gold/testsim/auto", data={"csrf_token": csrf_from(page)})
        assert r.status_code == 303 and r.headers["location"] == "/gold/sim?auto=on"
        page = (await c.get("/gold/sim?auto=on")).text
        assert "✓ Robot A now trades by itself" in page and "Trades by itself" in page
        async with db.connect() as conn:
            (robot,) = await sim.running_accounts(conn, trend_sim.RULE)
            s = await sim.settings_of(conn, robot)
        assert s is not None and s.permission == "AUTO"
        r = await c.post("/gold/testsim/ask", data={"csrf_token": csrf_from(page)})
        assert r.status_code == 303
    async with db.connect() as conn:
        s = await sim.settings_of(conn, robot)
        actions = sorted(
            a
            for (a,) in await conn.execute(
                select(audit_events.c.action).where(audit_events.c.action.like("gold_sim.test_%"))
            )
        )
    assert s is not None and s.permission == "ASK" and s.risk_pct == D(1)
    assert actions == [
        "gold_sim.test_robot_auto_off",
        "gold_sim.test_robot_auto_on",
        "gold_sim.test_robot_started",
    ]
