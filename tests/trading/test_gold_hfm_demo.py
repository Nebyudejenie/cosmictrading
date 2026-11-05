"""My Gold Auto-Trader in HFM Demo mode (S64): the gold reading sent to HFM's demo through the
existing order path, on the MT5 emulator. Virtual money; nothing here can reach a real account.

Gold here: HFM at 4,100.00 / 4,100.30; hourly candles with a range of 20 (ATR 20), so the stop is
30.00 below the ask."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import func, select

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_execution import trade_intents, trading_mandates
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.gold import hfm_demo
from cosmic.trading import mandates
from cosmic.trading.policy import active_policy, change_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_sim import World
from tests.trading.test_hfm import XAU, Hfm, a_quote, bridge_running, build, open_account, paxg

pytestmark = pytest.mark.keeps_binance_paused  # the gold reading never reads Binance's prices


async def _no_hfm_fee(h: Hfm) -> None:
    """HFM charged no commission on the owner's demo deals: the owner's step sets its fee to 0
    (without it, the policy's general 60 bps would be counted on every ounce)."""
    p = principal_for(h.env.owner, h.env.clock)
    async with h.env.db.transaction() as conn:
        _, policy = await active_policy(conn)
        params = policy.model_dump()
        params["venue_fees"] = ({"venue": "HFM", "bps": D(0)},)
        await change_policy(
            conn,
            principal=p,
            new_params=params,
            reason="HFM charges no commission",
            now=h.env.clock.now(),
            settings=h.env.settings,
        )


async def _grant(h: Hfm, *, money: str = "20000", risk: str = "0.4", mode: str = "AUTO") -> object:
    await _no_hfm_fee(h)
    terms = mandates.terms_from_money(
        account_alias="hfm-demo",
        money=D(money),
        care="NORMAL",
        mode=mode,  # type: ignore[arg-type]
        instruments=(XAU,),
        strategy_key=mandates.GOLD_READING,
        timeframe=mandates.HOURLY,
        days=90,
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


async def _cycles(h: Hfm, n: int = 4) -> None:
    for _ in range(n):
        await h.env.svc.execution.run_cycle()


async def test_a_buy_goes_to_hfm_with_its_stop_once_per_candle(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h)
        await w.market("BUY")
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:PROPOSED"]
        assert await hfm_demo.decide(db, h.env.clock.now()) == []  # once per candle
        (d,) = await _decisions(h)
        # 80.00 a trade; an ounce loses 34.11 at the stop (30.00, and the 0.1 % limit margin)
        assert (d.decision, d.action, d.ounces) == ("BUY", "PROPOSED", D(2))
        assert "approved under your mandate and sent to HFM's demo" in d.reasons[-1]
        async with db.connect() as conn:
            intent = (
                await conn.execute(select(trade_intents).where(trade_intents.c.id == d.intent_id))
            ).one()
        assert intent.status == "APPROVED" and intent.details["mandate_id"] == str(m)
        await _cycles(h)
        (pos,) = emu.positions.values()
        assert pos.volume == D("0.02") and pos.sl == D("4070.30")  # 2 ounces, the stop at HFM
        assert await h.reconcile() == ()


async def test_too_small_an_amount_waits_and_says_from_which_amount(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h, money="100", risk="2")  # 2.00 a trade; 1 ounce loses about 42
        await w.market("BUY")
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:WAIT"]
        (d,) = await _decisions(h)
        why = d.reasons[-1]
        assert why.startswith("WAIT — Your selected trading amount is too small")
        assert "HFM's smallest gold trade (1 ounce = 0.01 lot)" in why
        assert "HFM's demo trades this from an amount of" in why
        assert not emu.positions
        async with db.connect() as conn:
            told = await conn.scalar(
                select(func.count())
                .select_from(notifications)
                .where(notifications.c.kind == "gold_sim")
            )
            amount = await conn.scalar(
                select(trading_mandates.c.capital).where(trading_mandates.c.id == m)
            )
        assert told == 1 and amount == D(100)  # told once; the amount is never raised


async def test_a_sell_is_not_sent_and_says_why(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h)
        await w.market("SELL")
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:WAIT"]
        (d,) = await _decisions(h)
        assert d.decision == "SELL" and d.reasons[-1] == hfm_demo.NOT_YET_SHORT
        assert not emu.positions


async def test_the_rule_closes_with_the_stop_still_on(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h)
        await w.market("BUY")
        await hfm_demo.decide(db, h.env.clock.now())
        await _cycles(h)
        assert len(emu.positions) == 1
        h.env.clock.advance(minutes=1)
        emu.now_ms += 60_000
        await asyncio.sleep(0.1)  # the next report
        await a_quote(db, h.env.clock, XAU, "hfm_mt5_bridge", "4100.00", "4100.30")
        await paxg(db, h.env.clock, "4100.10", "4100.40")
        await w.candles("1h", [4100 - i for i in range(40)], half=10)  # the trend turned down
        await w.candles("1m", [4100.0] * 30, half=0.3)
        sent = len(emu.sent)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:EXIT"]
        await _cycles(h)
        assert not emu.positions
        assert [r["action"] for r in emu.sent[sent:]] == [emu.TRADE_ACTION_DEAL]
        assert await h.reconcile() == ()


async def test_ask_me_first_asks_in_the_portal(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _grant(h, mode="ASK")
        await w.market("BUY")
        await hfm_demo.decide(db, h.env.clock.now())
        (d,) = await _decisions(h)
        assert "asked: approve it in the portal (Approvals)" in d.reasons[-1]
        async with db.connect() as conn:
            status = await conn.scalar(
                select(trade_intents.c.status).where(trade_intents.c.id == d.intent_id)
            )
            asked = await conn.scalar(
                select(func.count())
                .select_from(notifications)
                .where(notifications.c.kind == "approval_requested")
            )
        assert status == "PROPOSED" and asked >= 1
        await _cycles(h)
        assert not emu.positions  # nothing is sent before the owner approves


async def test_the_panel_starts_changes_and_stops_hfm_demo(
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
            form = {
                "csrf_token": csrf_from(page),
                "mode": "HFM_DEMO",
                "amount": "20000",
                "automation": "ON",
                "permission": "AUTO",
                "risk_pct": "0.4",
            }
            review = await c.post("/gold/sim/review", data=form)
            assert review.status_code == 200 and "HFM DEMO · virtual money" in review.text
            assert "A trade now on the HFM demo would be 2 ounces (0.02 lot)" in review.text
            r = await c.post("/gold/sim/start", data=form)
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=hfm"
            page = (await c.get("/gold/sim?started=hfm")).text
            assert "✓ Started on your HFM demo" in page and "HFM Demo automation OFF" in page
            again = await c.post("/gold/sim/start", data={**form, "permission": "ASK"})
            assert again.status_code == 303
            async with db.connect() as conn:
                texts = [
                    r.payload.get("text", "")
                    for r in await conn.execute(
                        select(notifications.c.payload).where(
                            notifications.c.kind == "mandate_changed"
                        )
                    )
                ]
                active = list(
                    await conn.execute(
                        select(trading_mandates).where(trading_mandates.c.status == "ACTIVE")
                    )
                )
            for path in ("/", "/trader", "/hfm/settings"):  # every page with the mandate on
                shown = await c.get(path)
                assert shown.status_code == 200, (path, shown.text[:300])
            home = (await c.get("/")).text
            assert "GOLD AUTO-TRADER RUNNING</strong> — Yes: on your HFM demo" in home
            assert sum("Settings changed" in t for t in texts) == 1
            assert not any("Auto-trader OFF" in t for t in texts)  # never off and on
            assert len(active) == 1 and active[0].mode == "ASK"
            off = await c.post("/gold/hfm/off", data={"csrf_token": csrf_from(page)})
            assert off.status_code == 303
            async with db.connect() as conn:
                assert await hfm_demo.gold_mandates(conn, h.env.clock.now()) == []
            bad = await c.post(
                "/gold/sim/review", data={**form, "risk_pct": "6", "max_open_risk_pct": "10"}
            )
            assert bad.status_code == 422 and "at most 5 %" in bad.text


async def test_the_gold_reading_is_only_for_hfm_gold_on_the_demo(
    settings: Settings, db: Database
) -> None:
    from cosmic.core.errors import ConflictError
    from tests.trading_env import build_env

    env = await build_env(settings, db)  # a paper account
    terms = mandates.terms_from_money(
        account_alias="paper-main",
        money=D(100),
        care="NORMAL",
        mode="AUTO",
        instruments=("SPOT:BTC/USDT@BINANCE",),
        strategy_key=mandates.GOLD_READING,
        timeframe=mandates.HOURLY,
    )
    with pytest.raises(ConflictError, match="HFM's gold on the HFM demo account only"):
        async with env.db.transaction() as conn:
            await mandates.grant(
                conn,
                principal=env.principal(),
                terms=terms,
                now=env.clock.now(),
                settings=env.settings,
            )
    assert mandates.GOLD_READING not in mandates.RULES_BY_TIMEFRAME[mandates.HOURLY]


async def test_start_replaces_an_earlier_auto_trader_and_uses_hfms_measured_fee(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S65: the earlier (paused) auto-trader on the HFM demo never blocks START, and HFM's own
    deal records, not the policy's general 60 bps, give the fee: recorded with the same
    authenticated press, its evidence in the reason."""
    from cosmic.db.schema_trading import risk_policies
    from cosmic.gold import hfm_costs

    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        old = mandates.terms_from_money(
            account_alias="hfm-demo",
            money=D(100),
            care="NORMAL",
            mode="ASK",
            instruments=(XAU,),
            strategy_key="turtle_20_10",
            timeframe=mandates.HOURLY,
        )
        async with db.transaction() as conn:
            await mandates.grant(
                conn,
                principal=principal_for(h.env.owner, h.env.clock),
                terms=old,
                now=h.env.clock.now(),
                settings=h.env.settings,
            )
        for _ in range(3):  # the owner's trades by hand: HFM's deals, each with its commission
            p = emu.by_hand("BUY", "0.01")
            emu.by_hand("SELL", "0.01", position=p)
        for _ in range(250):
            await asyncio.sleep(0.02)
            async with db.connect() as conn:
                costs = await hfm_costs.measure(conn)
            if costs is not None and costs.measured:
                break
        assert costs is not None and costs.measured
        assert costs.bps < 1  # 3 USD a lot a side: well under 1 bps, not the policy's 60
        await w.market("BUY")
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/gold/sim")).text
            r = await c.post(
                "/gold/sim/start",
                data={
                    "csrf_token": csrf_from(page),
                    "mode": "HFM_DEMO",
                    "amount": "20000",
                    "permission": "AUTO",
                    "risk_pct": "0.4",
                },
            )
            assert r.status_code == 303 and r.headers["location"] == "/gold/sim?started=hfm"
        async with db.connect() as conn:
            _, policy = await active_policy(conn)
            reason = await conn.scalar(
                select(risk_policies.c.reason).where(risk_policies.c.active.is_(True))
            )
            rows = list(
                await conn.execute(select(trading_mandates).order_by(trading_mandates.c.granted_at))
            )
            texts = [
                r.payload.get("text", "")
                for r in await conn.execute(
                    select(notifications.c.payload).where(notifications.c.kind == "mandate_changed")
                )
            ]
        assert policy.fee_bps_for("HFM") == costs.bps
        assert "HFM's own records" in reason and "owner's START" in reason
        assert [(m.strategy_key, m.status) for m in rows] == [
            ("turtle_20_10", "REVOKED"),
            (mandates.GOLD_READING, "ACTIVE"),
        ]
        assert any("replacing the earlier one (Turtle" in t for t in texts)
        assert not any("Auto-trader OFF" in t for t in texts)  # never off and on
