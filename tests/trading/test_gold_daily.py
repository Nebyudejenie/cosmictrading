"""The daily gold robot (S75) on the second HFM demo account (S74), on two MT5 emulators: the
Turtle breakout on HFM's daily candles, decided once a day, sent with its stop at 2 × ATR(20) to
the second terminal only, closed by the 20-day exit; the owner's START and OFF.

Daily candles here: 55 days at 4,000 with a range of 40 (high 4,020, low 3,980), then the day
that decides. HFM's price is the emulator's 4,100.00 / 4,100.30."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trading_mandates
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.gold import daily
from cosmic.market.hfm_candles import Candle
from cosmic.trading import mandates
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_2
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_hfm_demo import _cycles, _no_hfm_fee
from tests.trading.test_gold_sim import World
from tests.trading.test_hfm import XAU, Hfm, build, open_account, paxg
from tests.trading.test_hfm_two_bridges import bridges_running, open_second, with_second

pytestmark = pytest.mark.keeps_binance_paused  # the gold robots never read Binance's prices

FLAT = [4000.0] * 55


def _day(t: datetime, close: str, half: str = "20") -> Candle:
    c = D(close)
    return Candle(t, c, c + D(half), c - D(half), c, D(1), True)


def _days(closes: Sequence[str], end: datetime) -> list[Candle]:
    return [_day(end - timedelta(days=len(closes) - k), c) for k, c in enumerate(closes)]


NOW = datetime(2026, 11, 10, 23, 30, tzinfo=UTC)


def test_the_rule_reads_the_breakout_its_exit_and_its_stop() -> None:
    few = daily.read(_days(["4000"] * 30, NOW), NOW)
    assert few.decision == daily.WAIT and "30 of 56 daily candles" in few.reasons[0]
    up = daily.read(_days([*["4000"] * 55, "4090"], NOW), NOW)
    # ATR(20): 40 a day, then the breakout day's true range 110 (4,110 − 4,000): 43.50.
    assert (up.decision, up.entry_high, up.entry_low) == (daily.BUY, D("4020"), D("3980"))
    assert (up.atr, up.stop_distance, up.exit_long, up.exit_short) == (
        D("43.5"),
        D("87.00"),
        False,
        True,
    )
    down = daily.read(_days([*["4000"] * 55, "3900"], NOW), NOW)
    assert (down.decision, down.exit_long, down.exit_short) == (daily.SELL, True, False)
    inside = daily.read(_days([*["4000"] * 55, "4010"], NOW), NOW)
    assert (inside.decision, inside.exit_long, inside.exit_short) == (daily.WAIT, False, False)
    old = daily.read(_days([*["4000"] * 55, "4090"], NOW - timedelta(days=6)), NOW)
    assert old.decision == daily.WAIT and "no daily candle has closed lately" in old.reasons[0]


async def _grant_daily(h: Hfm, *, money: str = "2000", risk: str = "12") -> object:
    await _no_hfm_fee(h)
    terms = mandates.terms_from_money(
        account_alias="hfm-demo-2",
        money=D(money),
        care="NORMAL",
        mode="AUTO",
        instruments=(XAU,),
        strategy_key=mandates.GOLD_DAILY,
        timeframe=mandates.DAILY,
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


async def _decisions(h: Hfm) -> list[object]:
    async with h.env.db.connect() as conn:
        return list(
            await conn.execute(select(gold_hfm_decisions).order_by(gold_hfm_decisions.c.at))
        )


async def _two_demos(
    settings: Settings, db: Database
) -> tuple[Hfm, object, MT5Emulator, MT5Emulator, World]:
    emu1, emu2 = MT5Emulator(), MT5Emulator(number=87654321)
    h = await build(settings, db, emu1)
    emu2.now_ms = emu1.now_ms
    second = await with_second(settings, h)
    return h, second, emu1, emu2, World(db, h.env.clock, h.env.owner.user_id)


def _later(h: Hfm, emus: Sequence[MT5Emulator], **step: float) -> None:
    h.env.clock.advance(**step)
    for e in emus:
        e.now_ms = int(h.env.clock.now().timestamp() * 1000)


async def _decide(h: Hfm) -> list[str]:
    """The daily decision once the bridges' reports have stored a fresh price and status (it
    decides nothing before; the reports arrive every 20 ms here)."""
    for _ in range(100):
        done = await daily.decide(h.env.db, h.env.clock.now())
        if done:
            return done
        await asyncio.sleep(0.05)
    return []


async def test_a_daily_breakout_trades_the_second_demo_once_a_day_and_its_exit_closes_it(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    async with bridges_running(db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        m = await _grant_daily(h)
        await w.candles("1d", [*FLAT, 4090.0], 20)
        assert await _decide(h) == [f"{m}:PROPOSED"]
        assert await daily.decide(db, h.env.clock.now()) == []  # once a day
        (d,) = await _decisions(h)
        # 240.00 a trade (12 % of 2,000); an ounce loses 91.10 at the stop (87.00, and the
        # 0.1 % limit margin): 2 ounces.
        assert (d.decision, d.action, d.ounces, d.stop_distance) == (
            "BUY",
            "PROPOSED",
            D(2),
            D("87.00"),
        )
        assert "closed above the 55-day high" in d.reasons[0]
        await _cycles(h)
        (pos,) = emu2.positions.values()
        assert pos.volume == D("0.02") and pos.sl == D("4013.30")  # 2 oz, the stop at HFM
        assert not emu1.positions  # the hourly robot's demo is never touched
        assert await h.reconcile() == ()
        # The next day closes below the 20-day low: the rule closes it at the next price.
        _later(h, (emu1, emu2), days=1)
        await w.candles("1d", [*FLAT, 3900.0], 20)
        assert await _decide(h) == [f"{m}:EXIT"]
        await _cycles(h)
        assert not emu2.positions
        *_, last = await _decisions(h)
        assert (
            "closing the buy of 2 ounce: the day closed below the 20-day low" in (last.reasons[-1])
        )


async def test_a_closed_market_decides_nothing_until_hfms_price_is_fresh(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    async with bridges_running(db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        m = await _grant_daily(h)
        await w.candles("1d", [*FLAT, 4090.0], 20)
        h.env.clock.advance(minutes=10)  # HFM's daily break: no new price for 10 minutes
        assert await daily.decide(db, h.env.clock.now()) == []
        assert await _decisions(h) == []  # not decided: the signal is not lost
        _later(h, (emu1, emu2), seconds=1)  # gold trades again
        await paxg(db, h.env.clock, "4100.10", "4100.40")  # Binance's PAXG, polled all along
        decided = await _decide(h)
        assert decided == [f"{m}:PROPOSED"], [d.reasons for d in await _decisions(h)]


async def test_the_daily_robot_is_only_for_the_second_demo_automatic_and_up_to_15_percent(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, second, emu1, emu2, _ = await _two_demos(settings, db)
    async with bridges_running(db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path):
        await open_account(h)
        assert await open_second(h, second) is not None  # type: ignore[arg-type]
        common = {
            "money": D(2000),
            "care": "NORMAL",
            "instruments": (XAU,),
            "days": 30,
            "open_positions": 1,
        }
        with pytest.raises(ConflictError, match=r"between 0.1 % and 15 %"):
            mandates.terms_from_money(
                account_alias="hfm-demo-2",
                mode="AUTO",
                strategy_key=mandates.GOLD_DAILY,
                timeframe=mandates.DAILY,
                risk_pct=D(16),
                **common,  # type: ignore[arg-type]
            )
        with pytest.raises(ConflictError, match=r"between 0.1 % and 5 %"):
            mandates.terms_from_money(
                account_alias="hfm-demo",
                mode="AUTO",
                strategy_key=mandates.GOLD_READING,
                timeframe=mandates.HOURLY,
                risk_pct=D(12),
                **common,  # type: ignore[arg-type]
            )
        for alias, mode in (("hfm-demo", "AUTO"), ("hfm-demo-2", "ASK")):
            terms = mandates.terms_from_money(
                account_alias=alias,
                mode=mode,  # type: ignore[arg-type]
                strategy_key=mandates.GOLD_DAILY,
                timeframe=mandates.DAILY,
                risk_pct=D(12),
                **common,  # type: ignore[arg-type]
            )
            async with db.transaction() as conn:
                with pytest.raises(ConflictError, match="the second HFM demo account only"):
                    await mandates.grant(
                        conn,
                        principal=principal_for(h.env.owner, h.env.clock),
                        terms=terms,
                        now=h.env.clock.now(),
                        settings=settings,
                    )


async def test_the_owner_starts_and_stops_the_daily_robot_on_the_gold_page(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h, second, emu1, emu2, w = await _two_demos(settings, db)
    async with bridges_running(db, h.env.clock, {BRIDGE: emu1, BRIDGE_2: emu2}, tmp_path):
        await open_account(h)
        await w.candles("1d", [*FLAT, 4010.0], 20)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/gold/sim")).text
            assert "Daily robot" in page and "the second HFM demo account is not open yet" in page
            assert "START the daily robot" not in page
            assert await open_second(h, second) is not None  # type: ignore[arg-type]
            await _no_hfm_fee(h)
            page = (await c.get("/gold/sim")).text
            # 1 ounce loses 80.00 at the stop today (2 × ATR 40): 4 % of 2,000 carries it.
            assert "START the daily robot" in page and 'value="4"' in page
            assert "1 ounce would lose about 80.00 at the stop" in page
            form = {"csrf_token": csrf_from(page), "amount": "2000", "risk_pct": "12"}
            r = await c.post("/gold/daily/start", data=form)
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=daily"
            page = (await c.get("/gold/sim?started=daily")).text
            assert "✓ The daily robot started on your second HFM demo" in page
            assert "240.00" in page and "Daily robot OFF" in page
            async with db.connect() as conn:
                active = list(
                    await conn.execute(
                        select(trading_mandates).where(trading_mandates.c.status == "ACTIVE")
                    )
                )
            (dm,) = [a for a in active if a.strategy_key == mandates.GOLD_DAILY]
            assert (dm.risk_pct, dm.max_loss_per_trade, dm.mode, dm.timeframe) == (
                D(12),
                D(240),
                "AUTO",
                "1d",
            )
            assert (dm.max_trades_per_day, dm.daily_loss_limit, dm.total_loss_limit) == (
                1,
                D(240),
                D(2000),
            )
            off = await c.post("/gold/daily/off", data={"csrf_token": csrf_from(page)})
            assert off.status_code == 303
            async with db.connect() as conn:
                assert await daily.daily_mandates(conn, h.env.clock.now()) == []
            too_much = await c.post("/gold/daily/start", data={**form, "risk_pct": "16"})
            assert too_much.status_code == 422 and "15 %" in too_much.text
