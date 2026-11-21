"""Robot A's own HFM demo sets itself up, and START when ready (S111), on two MT5 emulators:
terminal 2's safety check runs by itself while the market is open, its account opens by itself,
and Robot A starts from the owner's approval given in advance with their code; never on Robot
B's account, never while the market is closed, and the owner is told at each step."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import notifications, users
from cosmic.db.schema_execution import trading_mandates
from cosmic.db.schema_gold import robot_starts
from cosmic.db.schema_trading import accounts, broker_contract_runs
from cosmic.gold import robot_setup, trend_test
from cosmic.trading import mandates
from cosmic.trading.brokers.mt5_bridge import ADAPTER_2, BRIDGE, BRIDGE_2
from cosmic.trading.policy import active_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_daily import _later, _two_demos
from tests.trading.test_gold_hfm_demo import _no_hfm_fee
from tests.trading.test_hfm import Hfm, next_report, open_account
from tests.trading.test_hfm_two_bridges import bridges_running

pytestmark = pytest.mark.keeps_binance_paused  # the gold robots never read Binance's prices


async def _told(h: Hfm) -> list[str]:
    async with h.env.db.connect() as conn:
        return [
            r[0]
            for r in await conn.execute(
                select(notifications.c.payload["text"].astext)
                .where(notifications.c.kind == "gold_sim")
                .order_by(notifications.c.created_at)
            )
        ]


async def _starts(h: Hfm) -> list[Any]:
    async with h.env.db.connect() as conn:
        return list(await conn.execute(select(robot_starts).order_by(robot_starts.c.armed_at)))


async def _arm(h: Hfm) -> None:
    async with h.env.db.transaction() as conn:
        await robot_setup.arm(
            conn, principal=principal_for(h.env.owner, h.env.clock), now=h.env.clock.now()
        )


async def _tick(h: Hfm, second: Any) -> str | None:
    return await robot_setup.tick(h.env.db, h.env.clock, h.env.settings, second)


async def test_robot_as_demo_sets_itself_up_and_starts_from_the_owners_approval_in_advance(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """The owner approves on the gold page with their code; at the next step the market is
    open: terminal 2's check passes, its account opens with Robot A's 100 USD, and Robot A
    starts with the terms shown. Robot B's terminal is never touched."""
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    async with bridges_running(
        db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path, lanes=True
    ):
        await open_account(h)
        await _no_hfm_fee(h)
        await w.candles("1h", [4000.0 + k for k in range(400)], 3.0)
        await w.candles("15m", [4400.0] * 70, 3.0)
        await next_report(emu2)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/gold/sim")).text
            assert "Robot A&#39;s own HFM demo sets itself up" in page
            assert "START Robot A as soon as its demo is ready" in page
            r = await c.post("/gold/test/arm", data={"csrf_token": csrf_from(page)})
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?armed=test"
            page = (await c.get("/gold/sim?armed=test")).text
            assert "START when ready is approved" in page and "Cancel START when ready" in page
        async with db.connect() as conn:
            _, policy = await active_policy(conn)
        mine = policy.for_account(robot_setup.ALIAS, "SANDBOX")
        assert (mine.max_drawdown_pct, mine.max_open_risk_pct) == (60, 75)
        assert await _tick(h, second) == "started"
        assert await _tick(h, second) is None  # running: nothing more to do
        async with db.connect() as conn:
            acct = (
                await conn.execute(select(accounts).where(accounts.c.alias == robot_setup.ALIAS))
            ).one()
            (tm,) = await trend_test.test_mandates(conn, h.env.clock.now())
            runs = list(
                await conn.execute(
                    select(broker_contract_runs.c.status).where(
                        broker_contract_runs.c.adapter == ADAPTER_2
                    )
                )
            )
            others = list(
                await conn.execute(
                    select(trading_mandates).where(trading_mandates.c.account_id != acct.id)
                )
            )
        assert (acct.kind, acct.broker_adapter, acct.status) == ("SANDBOX", ADAPTER_2, "ACTIVE")
        assert [r.status for r in runs] == ["PASSED"]
        assert tm.account_id == acct.id
        assert (tm.capital, tm.max_loss_per_trade, tm.daily_loss_limit, tm.total_limit) == (
            D(100),
            D(30),
            D(30),
            D(60),
        )
        assert others == []  # nothing granted on Robot B's account
        (start,) = await _starts(h)
        assert (start.ended, start.mandate_id) == ("STARTED", tm.id)
        told = await _told(h)
        assert told[0].startswith("ROBOT A — START WHEN READY approved with your code")
        assert any("safety check PASSED on Robot A's own demo" in t for t in told)
        assert any(
            t.startswith("ROBOT A SETUP — Robot A's own demo account is open (hfm-demo-2, 100.00")
            for t in told
        )
        assert told[-1].startswith("ROBOT A STARTED on its own HFM demo (hfm-demo-2)")
        assert not emu1.positions and not emu2.positions  # nothing traded by the setup


async def test_without_an_approval_the_demo_opens_and_waits_for_the_owners_start(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, second, emu1, emu2, _ = await _two_demos(settings, db)
    async with bridges_running(
        db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path, lanes=True
    ):
        await open_account(h)
        await next_report(emu2)
        assert await _tick(h, second) == "account opened"
        async with db.connect() as conn:
            assert await trend_test.test_mandates(conn, h.env.clock.now()) == []
            setup = await robot_setup.status(conn, h.env.clock.now())
        assert setup.stage == "READY"
        told = await _told(h)
        assert told[-1].endswith(
            "Press START Robot A on the gold page, with your code, when you want it to trade."
        )


async def test_the_setup_waits_while_the_market_is_closed(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """Only terminal 2 reports: no fresh HFM price (the weekend), so no check is tried."""
    h, second, _, emu2, _ = await _two_demos(settings, db)
    async with bridges_running(db, h.env.clock, {BRIDGE_2: emu2}, tmp_path, lanes=True):
        await next_report(emu2)
        assert await _tick(h, second) == "market closed"
        async with db.connect() as conn:
            runs = await conn.scalar(select(broker_contract_runs.c.id).limit(1))
            setup = await robot_setup.status(conn, h.env.clock.now())
        assert runs is None
        assert setup.stage == "WAITING" and "gold's market is closed" in setup.text


async def test_terminal_2_on_robot_bs_account_never_opens_and_the_owner_is_told(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """Each robot its own account (the owner's choice of 2026-11-21): terminal 2 signed in to
    the account Robot B's was opened on is refused, and the owner is told what to do, once a
    day."""
    h, second, emu1, _, _ = await _two_demos(settings, db)
    same = MT5Emulator()  # the same MT5 login and server as terminal 1's
    same.now_ms = emu1.now_ms
    async with bridges_running(
        db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: same}, tmp_path, lanes=True
    ):
        await open_account(h)
        await next_report(same)
        assert await _tick(h, second) == "waiting"
        assert await _tick(h, second) == "waiting"
        async with db.connect() as conn:
            opened = await conn.scalar(
                select(accounts.c.id).where(accounts.c.broker_adapter == ADAPTER_2)
            )
        assert opened is None
        told = await _told(h)
        assert len(told) == 1
        assert "terminal 2 is signed in to hfm-demo's account (Robot B's)" in told[0]


async def test_start_when_ready_is_one_lapses_can_be_cancelled_and_is_refused_for_a_lost_owner(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, second, emu1, emu2, _ = await _two_demos(settings, db)
    async with bridges_running(
        db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path, lanes=True
    ):
        await open_account(h)
        await _arm(h)
        with pytest.raises(ConflictError, match="already waiting"):
            await _arm(h)
        async with db.transaction() as conn:  # sealed: only its ending is ever written
            with pytest.raises(Exception, match="sealed"):
                async with conn.begin_nested():
                    await conn.execute(
                        update(robot_starts).values(terms={"max_loss_per_trade": "300"})
                    )
            assert await robot_setup.cancel(
                conn,
                actor=principal_for(h.env.owner, h.env.clock).actor,
                now=h.env.clock.now(),
                why="cancelled by you",
            )
        await _arm(h)
        _later(h, (emu1, emu2), days=3, minutes=1)  # past its life, before the demo is ready
        assert await _tick(h, second) in ("waiting", "market closed")
        ends = [(s.ended, s.reason) for s in await _starts(h)]
        assert ends == [
            ("CANCELLED", "cancelled by you"),
            ("EXPIRED", "its demo was not ready in time"),
        ]
        assert any(t.startswith("ROBOT A — your START WHEN READY lapsed") for t in await _told(h))
        # The owner who approved can no longer approve trades: the start is refused, said.
        await _arm(h)
        async with db.transaction() as conn:
            await conn.execute(
                update(users).where(users.c.id == h.env.owner.user_id).values(is_active=False)
            )
    async with bridges_running(
        db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path, lanes=True
    ):
        await next_report(emu2)
        assert await _tick(h, second) == "start refused"
        *_, last = await _starts(h)
        assert last.ended == "REFUSED"
        assert "can no longer approve trades" in (last.reason or "")
        async with db.connect() as conn:
            assert await trend_test.test_mandates(conn, h.env.clock.now()) == []
        # (an owner who can no longer sign in is told nothing: no owner is active to tell)


async def test_robot_a_cannot_be_granted_from_an_approval_in_advance_of_another_rule(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, _, emu1, emu2, _ = await _two_demos(settings, db)
    async with bridges_running(db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path):
        await open_account(h)
        terms = mandates.terms_from_money(
            account_alias="hfm-demo",
            money=D(100),
            care="NORMAL",
            mode="AUTO",
            instruments=("CFD:XAU/USD@HFM",),
            strategy_key=mandates.GOLD_M1,
            timeframe=mandates.HOURLY,
            days=mandates.UNTIL_STOPPED_DAYS,
            risk_pct=D(2),
            open_positions=1,
        )
        async with db.transaction() as conn:
            with pytest.raises(ConflictError, match="only Robot A is started"):
                await mandates.grant_approved_in_advance(
                    conn,
                    owner=h.env.owner.user_id,
                    actor=robot_setup.ACTOR,
                    terms=terms,
                    now=h.env.clock.now(),
                    settings=h.env.settings,
                )
    assert timedelta(days=3) == robot_setup.LIFE
