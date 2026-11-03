"""The HFM gold home page (S55): the owner's HFM account at a glance, read from HFM's own report
through the bridge (here the Windows bridge's loop in process, on the MT5 emulator), the five
answers first, nothing simulated shown as HFM's."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import worker_heartbeats
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.db.schema_trading import bridge_state
from cosmic.reporting import hfm_home
from cosmic.trading import mandates
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_hfm import XAU, Hfm, bridge_running, build, buy_gold, open_account

D = Decimal
PAXG = "SPOT:PAXG/USDT@BINANCE"


async def _beat(h: Hfm) -> None:
    now = h.env.clock.now()
    async with h.env.db.transaction() as conn:
        await conn.execute(
            pg_insert(worker_heartbeats)
            .values(worker_id="test", process="worker", started_at=now, last_beat_at=now)
            .on_conflict_do_update(
                index_elements=[worker_heartbeats.c.worker_id], set_={"last_beat_at": now}
            )
        )


async def _paxg_hours(h: Hfm, n: int = 80) -> None:
    """Completed 1-hour PAXG bars up to the last hour (the HFM gold auto-trader's signal)."""
    now = h.env.clock.now()
    last = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    async with h.env.db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key == PAXG)
        )
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == "binance_public"))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run, source_id=src, kind="t", started_at=now, finished_at=now, status="SUCCESS"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, "
                "close, volume, is_complete, fetch_run_id, ingested_at, updated_at) VALUES (:i, :s, "
                "'1h', :t, 4100, 4110, 4090, 4100, 1, true, :r, :n, :n) ON CONFLICT DO NOTHING"
            ),
            [
                {"i": inst, "s": src, "t": last - timedelta(hours=k), "r": run, "n": now}
                for k in range(n)
            ],
        )


async def _grant(h: Hfm, money: str) -> None:
    terms = mandates.terms_from_money(
        account_alias="hfm-demo",
        money=D(money),
        care="BOLD",
        mode="AUTO",
        instruments=(XAU,),
        strategy_key="turtle_20_10",
    )
    async with h.env.db.transaction() as conn:
        await mandates.grant(
            conn,
            principal=h.env.principal(),
            terms=terms,
            now=h.env.clock.now(),
            settings=h.env.settings,
        )


async def _home(h: Hfm) -> hfm_home.HfmHome:
    async with h.env.db.connect() as conn:
        return await hfm_home.build(conn, h.env.clock.now())


def _answer(home: hfm_home.HfmHome, question: str) -> hfm_home.Answer:
    return next(a for a in home.answers if a.question.startswith(question))


async def test_the_hfm_account_at_a_glance(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _beat(h)
        await h.env.svc.reconcile_all(trigger="test")
        home = await _home(h)
    assert home.mode == "DEMO" and home.currency == "USD"
    assert home.balance == D(str(emu.balance)) and home.equity == D(str(emu.balance))
    assert home.bid is not None and home.market
    assert _answer(home, "Is my system working").ok is True
    assert _answer(home, "Is my broker connected").ok is True
    assert _answer(home, "Is automatic trading running").ok is False  # off
    assert any("Start the HFM auto-trader" in t for t in home.attention)
    assert home.decision == "WAIT" and home.decision_text == "the auto-trader is off"
    assert home.status == "NEEDS YOU" and home.last_check is not None


async def test_an_open_trade_shows_hfms_stop_and_result(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await buy_gold(h)
        await _beat(h)
        home = await _home(h)
    assert len(home.positions) == 1
    p = home.positions[0]
    assert (p.lots, p.ounces, p.stop) == (D("0.01"), D(1), D("4000.00"))  # 0.01 lot is 1 oz
    assert _answer(home, "Is my money protected").ok is not False


async def test_one_ounce_too_big_for_its_money_needs_the_owner(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """As on the owner's demo on 2026-11-03: $100 for an auto-trader whose smallest gold trade
    is 1 oz (about 4,100) skips every signal; the page says so and names the Cent account."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _paxg_hours(h)
        await _grant(h, "100")
        await _beat(h)
        home = await _home(h)
    running = _answer(home, "Is automatic trading running")
    assert running.ok is False and "cannot trade" in running.text
    assert any("Cent account" in t for t in home.attention)
    assert home.next_decision is not None and home.next_decision > h.env.clock.now()


async def _report(db: Database, h: Hfm, **account: Any) -> None:
    snap = {
        "terminal": {"connected": True},
        "account": {"trade_mode": "DEMO", "currency": "USD", "balance": "100.0", "equity": "100.0"}
        | account,
    }
    async with db.transaction() as conn:
        await conn.execute(
            pg_insert(bridge_state)
            .values(bridge="hfm-demo", received_at=h.env.clock.now(), snapshot=snap)
            .on_conflict_do_update(
                index_elements=[bridge_state.c.bridge],
                set_={"received_at": h.env.clock.now(), "snapshot": snap},
            )
        )


async def test_a_wrong_account_or_a_silent_bridge_is_a_problem(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
    await _beat(h)
    for account, said in (
        ({"trade_mode": "REAL"}, "a REAL account: every order is refused"),
        ({"currency": "USC"}, "its money is in USC: every order is refused"),
    ):
        await _report(db, h, **account)
        home = await _home(h)
        broker = _answer(home, "Is my broker connected")
        assert broker.ok is False and said in broker.text and home.status == "PROBLEM"
    h.env.clock.advance(minutes=1)  # the bridge silent for a minute
    await _beat(h)
    home = await _home(h)
    assert "not reporting" in _answer(home, "Is my broker connected").text


async def test_the_home_page_shows_the_hfm_account_first(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _beat(h)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/")).text
    assert "Your HFM gold account" in page and "HFM DEMO · virtual money" in page
    assert "STOP ALL NEW TRADES" in page and "Is my broker connected?" in page
    assert "Nothing simulated is shown here" in page
