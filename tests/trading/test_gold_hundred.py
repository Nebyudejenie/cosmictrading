"""Everything at 100 USD (S76; the owner, 2026-11-10: "please fix all thing to 100"): one press
with the owner's code stops the HFM demo robot (10,000 here) and runs the robot with 100 USD on
HFM's live prices in the simulation, 2 % a trade, take-profit at twice the risk."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events
from cosmic.db.schema_gold import gold_sim_accounts
from cosmic.gold import hfm_demo, sim
from cosmic.trading import killswitch
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_hfm_demo import _grant
from tests.trading.test_gold_sim import WED, World
from tests.trading.test_hfm import bridge_running, build, open_account

pytestmark = pytest.mark.keeps_binance_paused


async def test_the_100_usd_robot_starts_isolated_and_never_touches_the_hfm_robot(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        hfm_robot = await _grant(h)  # the HFM robot, 20,000 at 0.4 %: 2 oz a trade here
        await w.market("BUY")
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/gold/sim")).text
            assert "Start the 100 USD robot" in page
            r = await c.post("/gold/hundred", data={"csrf_token": csrf_from(page)})
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=hundred"
            async with db.connect() as conn:
                running = await hfm_demo.gold_mandates(conn, h.env.clock.now())
                (account,) = list(
                    await conn.execute(
                        select(gold_sim_accounts).where(gold_sim_accounts.c.status.in_(sim.ACTIVE))
                    )
                )
                chosen = await sim.settings_of(conn, account.id)
                started = (
                    await conn.execute(
                        select(audit_events.c.payload).where(
                            audit_events.c.action == "gold_sim.started_100_usd"
                        )
                    )
                ).one()
            # The HFM robot runs on, untouched; the 100 USD robot is its own account.
            assert [m.id for m in running] == [hfm_robot] and running[0].capital == D(20000)
            assert account.starting_balance == D(100)
            assert (chosen.amount, chosen.risk_pct, chosen.take_profit_r, chosen.permission) == (
                D(100),
                D(2),
                D(2),
                "AUTO",
            )
            assert started.payload["daily_loss"] == "6.00"
            assert started.payload["pause_after_losses"] == 3
            # Both decide on the same candle, each for itself: the 100 USD robot opens a
            # simulated buy, the HFM robot its own order at HFM; neither touches the other's.
            assert await sim.decide(db, h.env.clock.now()) == [f"{account.id}:BUY"]
            assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{hfm_robot}:PROPOSED"]
            (p,) = await w.positions(account.id)
            assert p.ounces * (p.entry_price - p.stop_price) <= D(2)
            page = (await c.get("/gold/sim?started=hundred")).text
            assert "✓ The 100 USD robot started" in page
            assert 'action="/gold/hundred"' not in page  # running: no second offer


@pytest.fixture
async def w(settings: Settings, db: Database) -> World:
    from cosmic.core.clock import FrozenClock
    from cosmic.sources.ingest import ensure_registry
    from cosmic.trading.policy import ensure_default_policy
    from tests.helpers import make_user

    clock = FrozenClock(WED)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    world = World(db, clock, (await make_user(db, clock)).user_id)
    await world.hfm("4040.00", "4040.30")
    return world


async def test_the_emergency_stop_halts_the_100_usd_robot(w: World) -> None:
    acct = await w.account("100")
    await w.market("BUY")
    async with w.db.transaction() as conn:
        await killswitch.activate(
            conn, actor=Actor.system("test"), reason="drill", now=w.clock.now()
        )
    assert await sim.decide(w.db, w.clock.now()) == [f"{acct}:WAIT"]
    (d,) = await w.decisions(acct)
    assert "the system is in emergency stop: no new trade" in d.reasons[-1]
    assert await w.positions(acct) == []


# ---- the owner's 100 USD rules (S77) ------------------------------------------------------------


def test_the_100_usd_rules_never_risk_more_than_2_and_target_twice_that() -> None:
    s = sim.defaults(D(100))
    assert (s.risk_pct, s.daily_loss, s.take_profit_r, s.max_positions) == (
        D(2),
        D("6.00"),
        D(2),
        1,
    )
    fresh = sim.fresh_state(D(100))
    for stop in ("8", "15", "25", "40", "120"):
        z = sim.size(s, fresh, D(stop), D("4100.30"), D(1000), sim.costs(D("0.35")))
        assert z.per_ounce == D(stop) + D("0.70")  # two spreads of slippage counted
        risk = z.ounces * z.per_ounce
        assert risk <= D(2)  # never above the owner's 2 USD
        if z.ounces:
            assert risk > D(2) - z.per_ounce * sim.STEP  # the largest size that fits
    # 120 a stop: the smallest trade (0.01 oz) would lose 1.21, it still fits; 250 would not.
    huge = sim.size(s, fresh, D(250), D("4100.30"), D(1000), sim.costs(D("0.35")))
    assert huge.ounces == 0 and huge.why_not is not None
    # After losses the base is the money left: the risk never grows after a loss.
    after = sim.size(s, sim.fresh_state(D(80)), D(15), D("4100.30"), D(1000), sim.costs(D("0.35")))
    assert after.ounces * after.per_ounce <= D("1.60")


def test_a_trade_that_could_pass_the_6_usd_day_waits() -> None:
    s = sim.defaults(D(100))
    z = sim.size(s, sim.fresh_state(D(100)), D(15), D("4100.30"), D(1000), sim.costs(D("0.35")))
    risk = z.ounces * z.per_ounce  # 0.12 oz × 15.70 = 1.884
    lost_4 = sim.State(D(96), D(0), D(96), D(0), D(96), D(100), D(-4), [])
    lost_5 = sim.State(D(95), D(0), D(95), D(0), D(95), D(100), D(-5), [])
    assert risk == D("1.8840")
    assert sim.day_room(s, lost_4, z) is None  # 4 + 1.88 stays within 6
    why = sim.day_room(s, lost_5, z)
    assert why is not None and "would take today past your daily limit of 6.00" in why


async def test_three_losing_trades_in_a_row_pause_the_robot_until_the_owner_resumes(
    settings: Settings, db: Database
) -> None:
    from datetime import UTC, datetime

    from cosmic.core.clock import FrozenClock
    from cosmic.db.schema import notifications
    from cosmic.sources.ingest import ensure_registry
    from cosmic.trading.policy import ensure_default_policy
    from tests.helpers import make_user

    clock = FrozenClock(datetime(2026, 10, 14, 10, 0, tzinfo=UTC))  # a Wednesday, gold open
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    owner = await make_user(db, clock)
    w = World(db, clock, owner.user_id)

    async def lose_one(acct: object) -> None:
        clock.advance(minutes=1)
        await w.hfm("4040.00", "4040.30")
        await w.market("BUY")
        assert await sim.decide(db, clock.now()) == [f"{acct}:BUY"]
        clock.advance(seconds=30)
        await w.hfm("4010.00", "4010.30")  # the stop (4,010.30) is hit at the bid
        assert await sim.watch(db, clock.now()) == ["BUY:STOP_LOSS"]

    async def status(acct: object) -> tuple[str, str]:
        async with db.connect() as conn:
            row = (
                await conn.execute(
                    select(gold_sim_accounts.c.status, gold_sim_accounts.c.status_reason).where(
                        gold_sim_accounts.c.id == acct
                    )
                )
            ).one()
        return row.status, row.status_reason or ""

    acct = await w.account("100")
    await w.hfm("4040.00", "4040.30")
    for _ in range(2):
        await lose_one(acct)
        assert (await status(acct))[0] == sim.RUNNING
    await lose_one(acct)  # the third in a row: about 5.45 lost today, within the 6.00
    state, why = await status(acct)
    assert state == sim.PAUSED and "3 losing trades in a row" in why
    async with db.connect() as conn:
        texts = [
            r.payload.get("text", "")
            for r in await conn.execute(
                select(notifications.c.payload).where(notifications.c.kind == "gold_sim")
            )
        ]
    assert any("3 losing trades in a row, so the robot paused itself" in t for t in texts)
    clock.advance(minutes=1)
    await w.hfm("4040.00", "4040.30")
    await w.market("BUY")
    assert await sim.decide(db, clock.now()) == []  # paused: nothing opens
    clock.advance(days=1)  # the next day; the owner switches it on again
    async with db.transaction() as conn:
        await sim.automation(conn, acct, True, clock.now())  # type: ignore[arg-type]
    await lose_one(acct)  # one loss after the restart does not pause it again
    assert (await status(acct))[0] == sim.RUNNING
