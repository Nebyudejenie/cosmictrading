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
from cosmic.db.schema_execution import orders, trade_intents, trading_mandates
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.db.schema_trading import positions
from cosmic.gold import hfm_demo, hfm_selftest
from cosmic.trading import mandates
from cosmic.trading.policy import active_policy, change_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_sim import World
from tests.trading.test_hfm import XAU, Hfm, a_quote, bridge_running, build, open_account, paxg

pytestmark = pytest.mark.keeps_binance_paused  # the gold reading never reads Binance's prices


async def _no_hfm_fee(h: Hfm, *, shorts: bool = True) -> None:
    """HFM charged no commission on the owner's demo deals: the owner's step sets its fee to 0
    (without it, the policy's general 60 bps would be counted on every ounce); and, as the
    owner's "buy and sell" does at START (S66), HFM's gold may be sold first (a short)."""
    p = principal_for(h.env.owner, h.env.clock)
    async with h.env.db.transaction() as conn:
        _, policy = await active_policy(conn)
        params = policy.model_dump()
        params["venue_fees"] = ({"venue": "HFM", "bps": D(0)},)
        params["short_venues"] = ("HFM",) if shorts else ()
        await change_policy(
            conn,
            principal=p,
            new_params=params,
            reason="HFM charges no commission",
            now=h.env.clock.now(),
            settings=h.env.settings,
        )


async def _grant(
    h: Hfm, *, money: str = "20000", risk: str = "0.4", mode: str = "AUTO", shorts: bool = True
) -> object:
    await _no_hfm_fee(h, shorts=shorts)
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


async def test_a_sell_waits_when_the_owner_chose_buying_only(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h, shorts=False)
        await w.market("SELL")
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:WAIT"]
        (d,) = await _decisions(h)
        assert d.decision == "SELL" and d.reasons[-1] == hfm_demo.NO_SHORTS
        assert not emu.positions


async def _short(h: Hfm, w: World, m: object) -> object:
    """The reading says SELL: a genuine short of 2 ounces at HFM's bid, its stop above."""
    await w.market("SELL")
    assert await hfm_demo.decide(h.env.db, h.env.clock.now()) == [f"{m}:PROPOSED"]
    (d,) = [x for x in await _decisions(h) if x.decision == "SELL"]
    # 80.00 a trade; an ounce loses 34.10 at the stop (30.00, and the 0.1 % limit margin)
    assert (d.action, d.ounces) == ("PROPOSED", D(2)) and "SELL (short) 2 ounce" in d.reasons[-1]
    await _cycles(h)
    (pos,) = emu_positions(h)
    assert pos.type == 1 and pos.volume == D("0.02")  # a short at HFM: 0.02 lot = 2 ounces
    assert pos.sl == D("4130.00")  # the bid 4100.00 + 30.00: its stop above, held by HFM
    async with h.env.db.connect() as conn:
        held = await conn.scalar(
            select(positions.c.quantity).where(positions.c.account_id == h.env.account_id)
        )
    assert held == D(-2)  # our books: short 2 ounces
    assert await h.reconcile() == ()  # and HFM agrees
    return d.intent_id


def emu_positions(h: Hfm) -> list[object]:
    return list(h.emu.positions.values())


async def test_a_short_opens_with_its_stop_above_and_the_rule_closes_it(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S66: a genuine SELL on the HFM demo, not the close of a buy: sold first at HFM's bid,
    its stop-loss above held by HFM; the hourly trend turns up and the rule buys it back, the
    stop on until HFM has closed it; the books and HFM agree at every step."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h)
        await _short(h, w, m)
        h.env.clock.advance(minutes=1)
        emu.now_ms += 60_000
        await asyncio.sleep(0.1)
        await a_quote(db, h.env.clock, XAU, "hfm_mt5_bridge", "4100.00", "4100.30")
        await paxg(db, h.env.clock, "4100.10", "4100.40")
        await w.candles("1h", [4000 + i for i in range(40)], half=10)  # the trend turned up
        await w.candles("1m", [4100.0] * 30, half=0.3)
        sent = len(emu.sent)
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m}:EXIT"]
        await _cycles(h)
        assert not emu.positions
        assert [(r["action"], r["type"]) for r in emu.sent[sent:]] == [
            (emu.TRADE_ACTION_DEAL, 0)  # one buy, by the position: no stop cleared first
        ]
        async with db.connect() as conn:
            held = await conn.scalar(
                select(positions.c.quantity).where(positions.c.account_id == h.env.account_id)
            )
        assert held == 0 and await h.reconcile() == ()


async def test_a_shorts_take_profit_buys_it_back_with_the_stop_on(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h)
        intent = await _short(h, w, m)
        emu.move("XAUUSD", "4039.00", "4039.30")  # down past the target 4040.00 (two stops)
        h.env.clock.advance(seconds=2)
        emu.now_ms += 2_000
        await a_quote(db, h.env.clock, XAU, "hfm_mt5_bridge", "4039.00", "4039.30")
        sent = len(emu.sent)
        await _cycles(h)
        assert not emu.positions
        assert [(r["action"], r["type"]) for r in emu.sent[sent:]] == [(emu.TRADE_ACTION_DEAL, 0)]
        async with db.connect() as conn:
            rows = {
                o.purpose: (o.status, o.side)
                for o in await conn.execute(select(orders).where(orders.c.intent_id == intent))
            }
        assert rows["TAKE_PROFIT"] == ("FILLED", "BUY") and rows["STOP_LOSS"][0] == "CANCELLED"
        assert await h.reconcile() == ()


async def test_a_short_stopped_out_by_hfm_is_booked_and_reconciled(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m = await _grant(h)
        intent = await _short(h, w, m)
        emu.move("XAUUSD", "4130.00", "4130.30")  # the ask reaches the stop above: HFM closes
        await _cycles(h)
        assert not emu.positions
        async with db.connect() as conn:
            stop = (
                await conn.execute(
                    select(orders).where(
                        orders.c.intent_id == intent, orders.c.purpose == "STOP_LOSS"
                    )
                )
            ).one()
            held = await conn.scalar(
                select(positions.c.quantity).where(positions.c.account_id == h.env.account_id)
            )
        assert (stop.status, stop.side, held) == ("FILLED", "BUY", 0)
        assert await h.reconcile() == ()  # the loss booked as HFM booked it


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
            # The owner's one page (S69): the ten things, first.
            for item in (
                "My gold",
                "My amount",
                "20,000.00",
                "Balance · equity",
                "Today",
                "Total",
                "Gold market now",
                "AI second opinion",
                "Automatic trading",
                "Open positions",
                "Protection",
                "Latest alerts",
            ):
                assert item in home, item
            assert home.index("My gold") < home.index("Everything else")
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


async def _move(h: Hfm, bid: str, ask: str) -> None:
    """HFM's price moves; its next report and our stored quote follow."""
    h.emu.move("XAUUSD", bid, ask)
    h.env.clock.advance(seconds=2)
    h.emu.now_ms += 2_000
    await asyncio.sleep(0.1)
    await a_quote(h.env.db, h.env.clock, XAU, "hfm_mt5_bridge", bid, ask)
    await paxg(h.env.db, h.env.clock, bid, ask)


async def _until(h: Hfm, want: str, *, cycles: bool = True) -> list[str]:
    """Advance the self-test (and the execution) until a step named ``want`` happens."""
    seen: list[str] = []
    for _ in range(12):
        if cycles:
            await _cycles(h, 2)
        steps = await hfm_selftest.advance(h.env.db, h.env.clock.now())
        seen += [s.split(":", 1)[1] for s in steps]
        if want in seen:
            return seen
        await asyncio.sleep(0.05)
    raise AssertionError(f"{want} never happened: {seen}")


async def test_the_self_test_proves_buy_and_sell_end_to_end(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S67, the owner's proof: under the mandate granted at START (no code per trade), a 1-ounce
    BUY and a 1-ounce genuine SELL, each filled by HFM, its stop-loss read back from HFM's own
    position, closed by its take-profit; then our books reconciled with HFM; the owner told."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m_id = await _grant(h)
        await w.market("BUY")
        async with db.transaction() as conn:
            m = await mandates.get_mandate(conn, m_id)  # type: ignore[arg-type]
            await hfm_selftest.start(conn, m, h.env.owner.user_id, h.env.clock.now())
        assert await hfm_demo.decide(db, h.env.clock.now()) == [f"{m_id}:WAIT"]  # it waits
        await _until(h, "BUY_PROPOSED", cycles=False)
        await _until(h, "BUY_OPENED")
        (pos,) = emu.positions.values()
        assert pos.type == 0 and pos.sl == D("4070.30")  # HFM holds the buy's stop
        await _move(h, "4101.00", "4101.30")  # past the buy's near target
        await _until(h, "BUY_CLOSED")
        assert not emu.positions
        await _until(h, "SELL_PROPOSED", cycles=False)
        await _until(h, "SELL_OPENED")
        (pos,) = emu.positions.values()
        assert pos.type == 1 and pos.sl == D("4131.00")  # a genuine short, its stop above
        await _move(h, "4100.00", "4100.30")  # down past the short's near target
        await _until(h, "SELL_CLOSED")
        assert not emu.positions
        assert await h.reconcile() == ()
        assert (await _until(h, "PASSED", cycles=False))[-1] == "PASSED"
        async with db.connect() as conn:
            run = await hfm_selftest.latest(conn)
            told = [
                r.payload["text"]
                for r in await conn.execute(
                    select(notifications.c.payload).where(notifications.c.kind == "gold_sim")
                )
            ]
    assert run.outcome == "PASSED"
    for side in ("BUY", "SELL"):
        e = run.evidence[side]
        assert e["opened"]["deal"] and e["opened"]["position"] and e["opened"]["stop_at_hfm"]
        assert e["closed"]["by"] == "its take-profit" and e["closed"]["deal"]
    assert run.evidence["SELL"]["opened"]["type_at_hfm"] == "SELL (short)"
    assert run.evidence["reconciliation"]["status"] == "PASSED"
    (summary,) = [t for t in told if t.startswith("HFM DEMO SELF-TEST PASSED")]
    assert "BUY 1" in summary and "SELL 1" in summary and "held by HFM" in summary


async def test_the_self_test_closes_by_time_when_the_target_is_not_reached(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    w = World(db, h.env.clock, h.env.owner.user_id)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        m_id = await _grant(h, shorts=False)  # buying only: the SELL is said, not tested
        await w.market("BUY")
        async with db.transaction() as conn:
            m = await mandates.get_mandate(conn, m_id)  # type: ignore[arg-type]
            await hfm_selftest.start(conn, m, h.env.owner.user_id, h.env.clock.now())
        await _until(h, "BUY_PROPOSED", cycles=False)
        await _until(h, "BUY_OPENED")
        h.env.clock.advance(minutes=16)  # the target was never reached
        emu.now_ms += 16 * 60_000
        await _move(h, "4100.00", "4100.30")
        await w.market("BUY")  # current candles for the exit's reading
        await _until(h, "BUY_TIME_EXIT", cycles=False)
        await _until(h, "BUY_CLOSED")
        await _until(h, "SELL_SKIPPED", cycles=False)
        assert await h.reconcile() == ()
        await _until(h, "PASSED", cycles=False)
        async with db.connect() as conn:
            run = await hfm_selftest.latest(conn)
    assert run.evidence["BUY"]["closed"]["by"] == "the time limit"
    assert run.evidence["SELL"] == {"skipped": "you chose buying only"}
