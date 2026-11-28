"""Robot A on its own HFM demo (S98, the TEST robot; S110, the owner's choice of 2026-11-20, "real
demo orders at 1 oz"), on two MT5 emulators: the lab's best candidate as real orders on the second
demo. BUY and SELL, once per 15-minute candle, HFM's smallest trade and never more, its stop and
target with the order, within the mandate's loss a trade; judged on its record in markets as
volatile as now (below 30 % rejected; opened by itself only where that record made money);
closed after 24 hours; never on terminal 1."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, notifications
from cosmic.db.schema_execution import trading_mandates
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.gold import controls, learning, sim, trend_sim, trend_test
from cosmic.gold import jobs as gold_jobs
from cosmic.reporting import daily as daily_report
from cosmic.trading import mandates
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_2
from cosmic.trading.policy import active_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.trading.test_gold_daily import _later, _two_demos
from tests.trading.test_gold_hfm_demo import _cycles, _no_hfm_fee
from tests.trading.test_hfm import XAU, Hfm, next_report, open_account
from tests.trading.test_hfm_two_bridges import bridges_running, open_second

pytestmark = pytest.mark.keeps_binance_paused  # the gold robots never read Binance's prices


@pytest.fixture(autouse=True)
def _the_owners_bands(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are of the owner's bands, which decide wherever learning mode is off; on a
    demo it is on since S124 (``tests/trading/test_robot_learning.py``)."""
    monkeypatch.setattr(learning, "KINDS", frozenset())


TURN_UP = [4400.0] * 60 + [4400 - 1.5 * k for k in range(1, 8)] + [4400.0]
TURN_DOWN = [4400.0] * 60 + [4400 + 1.5 * k for k in range(1, 8)] + [4400.0]
RISING_HOURS = [4000.0 + k for k in range(400)]
FALLING_HOURS = [4400.0 - k for k in range(400)]
ASK, BID = D("4100.30"), D("4100.00")  # the emulator's gold


async def _grant(h: Hfm, *, money: str = "100", risk: str = "30") -> object:
    """What START grants (the owner's choice of 2026-11-20): 30 a trade at most, 30 a day, 60 in
    all."""
    await _no_hfm_fee(h)
    terms = mandates.terms_from_money(
        account_alias="hfm-demo-2",
        money=D(money),
        care="NORMAL",
        mode="AUTO",
        instruments=(XAU,),
        strategy_key=mandates.GOLD_TREND_BUY,
        timeframe=mandates.HOURLY,
        days=mandates.UNTIL_STOPPED_DAYS,
        risk_pct=D(risk),
        open_positions=1,
        daily_loss=trend_test.DAILY_LOSS,
        total_loss=trend_test.TOTAL_LOSS,
    )
    async with h.env.db.transaction() as conn:
        return await mandates.grant(
            conn,
            principal=principal_for(h.env.owner, h.env.clock),
            terms=terms,
            now=h.env.clock.now(),
            settings=h.env.settings,
        )


async def _decide(h: Hfm) -> list[str]:
    """Robot A's step once the reports have stored a fresh price and status."""
    for _ in range(100):
        done = await gold_jobs.test_now(h.ctx, h.env.clock.now())
        if done:
            return done
        await asyncio.sleep(0.05)
    return []


async def _decisions(h: Hfm) -> list[Any]:
    async with h.env.db.connect() as conn:
        return list(
            await conn.execute(select(gold_hfm_decisions).order_by(gold_hfm_decisions.c.at))
        )


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


async def _filled(emu: Any) -> None:
    for _ in range(50):  # sent in the same step; HFM's answer comes with the next reports
        if emu.positions:
            return
        await asyncio.sleep(0.05)


async def _closed(emu: Any) -> None:
    for _ in range(50):
        if not emu.positions:
            return
        await asyncio.sleep(0.05)


async def test_robot_a_buys_one_ounce_in_the_uptrend_and_closes_after_24_hours(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """A normal market (its BUY side won 34.7 % and made +0.166 R there): 1 ounce, its risk
    within the 30 a trade, sent at once with HFM holding the stop and the 1:2 target."""
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    emus = {BRIDGE: emu1, BRIDGE_2: emu2}
    async with bridges_running(db, h.env.clock, emus, tmp_path, lanes=True):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        m = await _grant(h)
        await w.candles("1h", RISING_HOURS, 3.0)
        await w.candles("15m", TURN_UP, 3.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:PROPOSED"]
        assert await trend_test.decide(db, h.env.clock.now()) == []  # once a 15-minute candle
        (d,) = await _decisions(h)
        assert (d.decision, d.action, d.ounces) == ("BUY", "PROPOSED", 1)  # 0.01 lot, never more
        assert d.reasons[0].startswith("Robot A (m15-trend-buy-1 and m15-trend-sell-1): both")
        assert D("9.46") <= d.stop_distance < D("15.26")  # a normal market for its BUY side
        assert "in normal markets like this one" in d.reasons[-1]
        assert "won 34.7 % and made +0.166 R a trade" in d.reasons[-1]
        assert d.reasons[-1].endswith("sent to HFM's demo 2")
        assert d.stop_distance + trend_test.SLIPPAGE <= 30  # within the most a trade may lose
        await _filled(emu2)
        (pos,) = emu2.positions.values()
        assert pos.volume == D("0.01")  # 1 ounce: lots of 100 ounces
        assert pos.sl == ASK - d.stop_distance  # below the ask, held by HFM
        assert pos.tp == ASK + 2 * d.stop_distance  # 1:2
        assert not emu1.positions  # the other robots' terminal is never touched
        await _cycles(h)
        assert await h.reconcile() == ()
        # The daily report says it (S110): HFM's own balance, the signal, what is open.
        async with db.connect() as conn:
            lines = await daily_report.robot_lines(conn, h.env.clock.now())
        assert lines[0].startswith("Robot A (HFM demo 2) [DEMO · real orders, virtual money]")
        assert "USD at HFM" in lines[0]
        assert lines[1].startswith("  last 24 h: 1 signal(s), 1 sent to HFM")
        assert lines[1].endswith("open: 1 trade, its stop and target held by HFM")
        assert not any(x.startswith("Robot B") for x in lines)  # Robot B made no decision
        # 24 hours later it is still open: the robot closes it at HFM's price.
        _later(h, (emu1, emu2), hours=24)
        await w.candles("1h", RISING_HOURS, 3.0)
        await w.candles("15m", [4400.0] * 70, 3.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:EXIT"]
        await _closed(emu2)
        assert not emu2.positions
        *_, last = await _decisions(h)
        assert last.reasons[-1].startswith("closing the buy of 1 ounce after 24 hours")


async def test_robot_a_sells_where_its_record_made_money_and_closes_it(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """A volatile market for its SELL side (34.7 %, +0.200 R): a genuine short of 1 ounce, the
    stop above the bid and the target below it, closed by a buy after 24 hours."""
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    emus = {BRIDGE: emu1, BRIDGE_2: emu2}
    async with bridges_running(db, h.env.clock, emus, tmp_path, lanes=True):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        m = await _grant(h)
        await w.candles("1h", FALLING_HOURS, 3.0)
        await w.candles("15m", TURN_DOWN, 4.8)  # wider candles: a stop of 17.47 or more
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:PROPOSED"]
        (d,) = await _decisions(h)
        assert (d.decision, d.action, d.ounces) == ("SELL", "PROPOSED", 1)
        assert d.stop_distance >= D("17.47")
        assert "in volatile markets like this one" in d.reasons[-1]
        assert "won 34.7 % and made +0.200 R a trade" in d.reasons[-1]
        await _filled(emu2)
        (pos,) = emu2.positions.values()
        assert pos.volume == D("0.01") and pos.type == 1  # a short (MT5's POSITION_TYPE_SELL)
        assert pos.sl == BID + d.stop_distance  # above the bid, held by HFM
        assert pos.tp == BID - 2 * d.stop_distance
        assert not emu1.positions
        await _cycles(h)
        _later(h, (emu1, emu2), hours=24)
        await w.candles("1h", FALLING_HOURS, 3.0)
        await w.candles("15m", [4400.0] * 70, 3.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:EXIT"]
        await _closed(emu2)
        assert not emu2.positions
        *_, last = await _decisions(h)
        assert last.reasons[-1].startswith("closing the sell of 1 ounce after 24 hours")


async def test_robot_a_rejects_below_30_and_leaves_what_made_no_money_and_says_so(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """The owner's bands on its record in markets like now: a SELL in a normal market (26.7 %)
    is rejected; a BUY in a volatile one (30.6 %, -0.002 R) is not taken by itself. Nothing is
    sent, and the owner is told why, each once."""
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    emus = {BRIDGE: emu1, BRIDGE_2: emu2}
    async with bridges_running(db, h.env.clock, emus, tmp_path, lanes=True):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        m = await _grant(h)
        await w.candles("1h", FALLING_HOURS, 3.0)
        await w.candles("15m", TURN_DOWN, 3.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:WAIT"]
        (sell,) = await _decisions(h)
        assert sell.decision == "SELL" and D("12.16") <= sell.stop_distance < D("17.47")
        assert sell.reasons[-1].startswith("WAIT — REJECTED, below your 30 %: SELL 1 oz")
        _later(h, (emu1, emu2), minutes=15)
        await w.candles("1h", RISING_HOURS, 3.0)
        await w.candles("15m", TURN_UP, 4.8)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:WAIT"]
        *_, buy = await _decisions(h)
        assert buy.decision == "BUY" and buy.stop_distance >= D("15.26")
        assert buy.reasons[-1].startswith("WAIT — not taken: BUY 1 oz: in volatile markets")
        assert "made -0.002 R a trade" in buy.reasons[-1]
        assert not emu2.positions and not emu1.positions
        told = await _told(h)
        assert len(told) == 2
        assert told[0].startswith("HFM DEMO 2 (Robot A): a SELL signal REJECTED, below your 30 %")
        assert told[1].startswith("HFM DEMO 2 (Robot A): a BUY signal NOT TAKEN")
        assert "only with a positive expected value" in told[1]


async def test_robot_a_waits_when_one_ounce_would_risk_more_than_its_ceiling(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """Gold so volatile that 1 ounce at the rule's stop would lose more than the 30 a trade the
    owner allows: nothing is sent, never a bigger risk, and the owner is told."""
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    emus = {BRIDGE: emu1, BRIDGE_2: emu2}
    async with bridges_running(db, h.env.clock, emus, tmp_path, lanes=True):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        m = await _grant(h)
        await w.candles("1h", FALLING_HOURS, 3.0)
        await w.candles("15m", TURN_DOWN, 9.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:WAIT"]
        (d,) = await _decisions(h)
        assert d.decision == "SELL" and d.stop_distance + trend_test.SLIPPAGE > 30
        assert "HFM's smallest gold trade (1 ounce) would lose about" in d.reasons[-1]
        assert d.reasons[-1].endswith("gold is too volatile for it now")
        assert not emu2.positions
        (told,) = await _told(h)
        assert told.startswith("HFM DEMO 2 (Robot A): a SELL was not taken.")


async def test_robot_a_is_only_for_the_second_demo_automatic_and_at_most_30_percent(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, second, emu1, emu2, _ = await _two_demos(settings, db)
    async with bridges_running(db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        for alias, mode in (("hfm-demo", "AUTO"), ("hfm-demo-2", "ASK")):
            terms = mandates.terms_from_money(
                account_alias=alias,
                money=D(100),
                care="NORMAL",
                mode=mode,
                instruments=(XAU,),
                strategy_key=mandates.GOLD_TREND_BUY,
                timeframe=mandates.HOURLY,
                days=mandates.UNTIL_STOPPED_DAYS,
                risk_pct=D(30),
                open_positions=1,
            )
            async with h.env.db.transaction() as conn:
                with pytest.raises(ConflictError, match="second HFM demo account only"):
                    await mandates.grant(
                        conn,
                        principal=principal_for(h.env.owner, h.env.clock),
                        terms=terms,
                        now=h.env.clock.now(),
                        settings=h.env.settings,
                    )
    common: dict[str, Any] = {
        "account_alias": "hfm-demo-2",
        "money": D(100),
        "care": "NORMAL",
        "mode": "AUTO",
        "instruments": (XAU,),
        "timeframe": mandates.HOURLY,
    }
    with pytest.raises(ConflictError, match=r"between 0\.1 % and 30 %"):
        mandates.terms_from_money(strategy_key=mandates.GOLD_TREND_BUY, risk_pct=D(31), **common)
    with pytest.raises(ConflictError, match=r"between 0\.1 % and 5 %"):
        mandates.terms_from_money(strategy_key=mandates.GOLD_M1, risk_pct=D(6), **common)
    with pytest.raises(ConflictError, match=r"the total stop must be from 30\.00"):
        mandates.terms_from_money(
            strategy_key=mandates.GOLD_TREND_BUY, risk_pct=D(30), total_loss=D(20), **common
        )


async def test_the_owner_starts_robot_a_on_its_demo_and_its_simulation_stops(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """START with the owner's code: their choice of 2026-11-20 (30 a trade at most, 30 a day,
    60 in all, a pause after three losses), the demo account's own limits in the risk policy
    (60 % below its best) and no other account's; Robot A's simulation stops. OFF without a
    code."""
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    async with bridges_running(
        db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path, lanes=True
    ):
        await open_account(h)
        await w.candles("1h", RISING_HOURS, 3.0)
        await w.candles("15m", [4400.0] * 70, 3.0)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/gold/sim")).text
            assert "Robot A on its own HFM demo" in page
            assert "Robot A&#39;s own HFM demo sets itself up" in page
            assert "START Robot A on its HFM demo" not in page
            # Robot A in the simulation first: START on the demo stops it.
            r = await c.post("/gold/testsim/start", data={"csrf_token": csrf_from(page)})
            assert r.status_code == 303
            assert await open_second(h, second) is not None  # type: ignore[arg-type]
            await next_report(emu2)
            page = (await c.get("/gold/sim")).text
            assert "START Robot A on its HFM demo" in page and "Your choice of 20 Nov" in page
            r = await c.post("/gold/test/start", data={"csrf_token": csrf_from(page)})
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=test"
            page = (await c.get("/gold/sim?started=test")).text
            assert "✓ Robot A started on its own HFM demo" in page and "Robot A OFF" in page
            assert "it would risk about" in page  # 1 ounce at the rule's stop now
            async with db.connect() as conn:
                active = list(
                    await conn.execute(
                        select(trading_mandates).where(trading_mandates.c.status == "ACTIVE")
                    )
                )
                (tm,) = [a for a in active if a.strategy_key == mandates.GOLD_TREND_BUY]
                ctl = await controls.get(conn, tm.id)
                _, policy = await active_policy(conn)
                assert await sim.running_accounts(conn, trend_sim.RULE) == []
                moved = await conn.scalar(
                    select(audit_events.c.id).where(
                        audit_events.c.action == "gold_sim.test_robot_moved_to_demo"
                    )
                )
            assert (
                tm.capital,
                tm.max_loss_per_trade,
                tm.daily_loss_limit,
                tm.total_loss_limit,
                tm.mode,
            ) == (D(100), D(30), D(30), D(60), "AUTO")
            assert ctl is not None and ctl.pause_after_losses == 3
            mine = policy.for_account("hfm-demo-2", "SANDBOX")
            assert (mine.max_drawdown_pct, mine.max_open_risk_pct) == (60, 75)
            other = policy.for_account("hfm-demo", "SANDBOX")
            assert other.max_drawdown_pct == policy.max_drawdown_pct < 60
            assert moved is not None
            off = await c.post("/gold/test/off", data={"csrf_token": csrf_from(page)})
            assert off.status_code == 303
            async with db.connect() as conn:
                assert await trend_test.test_mandates(conn, h.env.clock.now()) == []
