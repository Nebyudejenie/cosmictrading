"""The TEST robot (S98) on the second HFM demo, on two MT5 emulators: the lab's best candidate as a
forward test. BUY only, once per 15-minute candle, its stop and target with the order, in the
account's own volume steps within the mandate's loss a trade; closed after 24 hours; never on
terminal 1. On a standard account (1 ounce at least) the owner's 2 USD a trade buys nothing, and
it says the test needs a Cent account: Cent support (its own lot rules, money in US cents) comes
with the owner's account (S99). The order flow is tested here at a size a standard account
takes."""

from __future__ import annotations

import asyncio
from decimal import ROUND_DOWN
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trading_mandates
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.gold import controls, trend_test
from cosmic.gold import jobs as gold_jobs
from cosmic.trading import mandates
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_2
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.trading.test_gold_daily import _later, _two_demos
from tests.trading.test_gold_hfm_demo import _cycles, _no_hfm_fee
from tests.trading.test_hfm import XAU, Hfm, next_report, open_account
from tests.trading.test_hfm_two_bridges import bridges_running, open_second

pytestmark = pytest.mark.keeps_binance_paused  # the gold robots never read Binance's prices

TURN_UP = [4400.0] * 60 + [4400 - 1.5 * k for k in range(1, 8)] + [4400.0]
RISING_HOURS = [4000.0 + k for k in range(400)]


async def _grant(h: Hfm, *, money: str = "100", risk: str = "2") -> object:
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
    """The TEST robot's step once the reports have stored a fresh price and status."""
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


async def test_the_test_robot_buys_in_the_uptrend_within_2_usd_and_closes_after_24_hours(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    emus = {BRIDGE: emu1, BRIDGE_2: emu2}
    async with bridges_running(db, h.env.clock, emus, tmp_path, lanes=True):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        # 30.00 a trade: whole ounces fit (2 here: the order's value and its share of the
        # account stay within the risk policy's limits)
        m = await _grant(h, money="1000", risk="3")
        await w.candles("1h", RISING_HOURS, 3.0)
        await w.candles("15m", TURN_UP, 3.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:PROPOSED"]
        assert await trend_test.decide(db, h.env.clock.now()) == []  # once a 15-minute candle
        (d,) = await _decisions(h)
        assert (d.decision, d.action) == ("BUY", "PROPOSED")
        assert d.reasons[0].startswith("TEST (m15-trend-buy-1): both trends UP")
        # 30.00 a trade over the stop distance and the 0.10 slippage allowance, in whole ounces
        # (the account's step: 0.01 lot of 100 ounces).
        per_ounce = d.stop_distance + D("0.10")
        assert d.ounces == (D(30) / per_ounce).quantize(D(1), ROUND_DOWN) == 2
        assert d.ounces * per_ounce <= 30
        for _ in range(50):  # sent in the same step; HFM's answer comes with the next reports
            if emu2.positions:
                break
            await asyncio.sleep(0.05)
        (pos,) = emu2.positions.values()
        assert pos.volume == d.ounces / 100  # lots of 100 ounces
        assert pos.sl == D("4100.30") - d.stop_distance  # below the ask, held by HFM
        assert pos.tp == D("4100.30") + 2 * d.stop_distance  # 1:2
        assert not emu1.positions  # the other robots' terminal is never touched
        await _cycles(h)
        assert await h.reconcile() == ()
        # 24 hours later it is still open: the robot closes it at HFM's price.
        _later(h, (emu1, emu2), hours=24)
        await w.candles("1h", RISING_HOURS, 3.0)
        await w.candles("15m", [4400.0] * 70, 3.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:EXIT"]
        for _ in range(50):
            if not emu2.positions:
                break
            await asyncio.sleep(0.05)
        assert not emu2.positions
        *_, last = await _decisions(h)
        assert "closing the buy of" in last.reasons[-1] and "after 24 hours" in last.reasons[-1]


async def test_a_one_ounce_step_never_trades_the_owners_2_usd(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """A standard account's smallest trade is 1 ounce: at a stop of several dollars it would
    lose more than 2 USD, so the test takes nothing and says the test needs a Cent account."""
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    async with bridges_running(
        db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path, lanes=True
    ):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        m = await _grant(h)
        await w.candles("1h", RISING_HOURS, 3.0)
        await w.candles("15m", TURN_UP, 3.0)
        await next_report(emu2)
        assert await _decide(h) == [f"{m}:WAIT"]
        (d,) = await _decisions(h)
        assert "smallest gold trade (1 ounce)" in d.reasons[-1]
        assert d.reasons[-1].endswith("the test needs a Cent account")
        assert not emu2.positions and not emu1.positions


async def test_the_test_robot_is_only_for_the_second_demo_and_automatic(
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
                risk_pct=D(2),
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


async def test_the_owner_starts_and_stops_the_test_robot_on_the_gold_page(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """START with the owner's code and their 100 USD rules; OFF without a code. On a standard
    second demo the page says at once that the test needs a Cent account."""
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
            assert "TEST robot" in page and "your Cent demo, once its MT5" in page
            assert "START the TEST robot" not in page
            assert await open_second(h, second) is not None  # type: ignore[arg-type]
            await next_report(emu2)
            page = (await c.get("/gold/sim")).text
            assert "START the TEST robot" in page and "Your 100 USD rules" in page
            r = await c.post("/gold/test/start", data={"csrf_token": csrf_from(page)})
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=test"
            page = (await c.get("/gold/sim?started=test")).text
            assert "✓ The TEST robot started on your second HFM demo" in page
            assert "TEST robot OFF" in page
            # A standard account: 1 ounce is its smallest trade, said at once.
            assert "the test needs a Cent account" in page
            async with db.connect() as conn:
                active = list(
                    await conn.execute(
                        select(trading_mandates).where(trading_mandates.c.status == "ACTIVE")
                    )
                )
                (tm,) = [a for a in active if a.strategy_key == mandates.GOLD_TREND_BUY]
                ctl = await controls.get(conn, tm.id)
            assert (tm.capital, tm.max_loss_per_trade, tm.daily_loss_limit, tm.mode) == (
                D(100),
                D(2),
                D(6),
                "AUTO",
            )
            assert ctl is not None and ctl.pause_after_losses == 3
            off = await c.post("/gold/test/off", data={"csrf_token": csrf_from(page)})
            assert off.status_code == 303
            async with db.connect() as conn:
                assert await trend_test.test_mandates(conn, h.env.clock.now()) == []
