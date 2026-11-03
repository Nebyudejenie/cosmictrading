"""The HFM gold home page (S55): the owner's HFM account at a glance, read from HFM's own report
through the bridge (here the Windows bridge's loop in process, on the MT5 emulator), the five
answers first, nothing simulated shown as HFM's."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import worker_heartbeats
from cosmic.db.schema_execution import reconciliation_runs
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.db.schema_trading import bridge_state
from cosmic.reporting import hfm_home
from cosmic.research.library import auto
from cosmic.trading import mandates
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.mt5_emulator import MT5Emulator, Position
from tests.trading.test_hfm import XAU, Hfm, bridge_running, build, buy_gold, open_account, paxg
from tests.trading_env import Env, build_env

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


async def _grant(h: Hfm, money: str, mode: str = "AUTO") -> None:
    terms = mandates.terms_from_money(
        account_alias="hfm-demo",
        money=D(money),
        care="BOLD",
        mode=mode,  # type: ignore[arg-type]
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
    assert (p.symbol, p.lots, p.units, p.stop) == ("XAUUSD", D("0.01"), D(1), D("4000.00"))
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
    assert home.next_decision is not None and home.next_decision_end is not None
    assert home.next_decision_end > h.env.clock.now()  # the window still to come, or now


async def _report(db: Database, h: Hfm, **account: Any) -> None:
    snap = {
        "terminal": {"connected": True, "trade_allowed": True, "tradeapi_disabled": False},
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
    assert "has not reported since" in _answer(home, "Is my broker connected").text


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
    assert "Nothing from the paper simulator is shown here" in page


# ---- found by review (2026-11-03): each answer only on fresh facts ------------------------------


async def test_every_open_position_needs_its_stop_bitcoin_too(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _beat(h)
        await h.env.svc.reconcile_all(trigger="test")
        emu.positions[999] = Position(999, "#BTCUSD", D("0.10"), D("84000"), 0, "x", emu.now_ms)
        emu._deal(
            order=1,
            type=1,
            entry=1,
            magic=0,
            position_id=998,
            volume=0.1,
            price=82000.0,
            profit=-500.0,
            symbol="#BTCUSD",
        )
        await asyncio.sleep(0.5)
        home = await _home(h)
    protected = _answer(home, "Is my money protected")
    assert protected.ok is False and "#BTCUSD" in protected.text and home.status == "PROBLEM"
    assert [p.symbol for p in home.positions] == ["#BTCUSD"] and home.total == D(-500)


async def test_without_a_current_report_protection_is_unknown(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
    async with db.transaction() as conn:
        await conn.execute(delete(bridge_state))
    await _beat(h)
    home = await _home(h)
    protected = _answer(home, "Is my money protected")
    assert protected.ok is None and protected.text.startswith("unknown")
    assert _answer(home, "Is my broker connected").ok is False and home.status == "PROBLEM"


async def test_the_newest_check_with_hfm_decides(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _beat(h)
        now = h.env.clock.now()
        async with db.transaction() as conn:
            for minutes, status in ((10, "PASSED"), (5, "FAILED"), (0, "FAILED")):
                at = now - timedelta(minutes=minutes)
                await conn.execute(
                    reconciliation_runs.insert().values(
                        id=uuid7(),
                        account_id=h.env.account_id,
                        trigger="t",
                        started_at=at,
                        finished_at=at,
                        status=status,
                    )
                )
        home = await _home(h)
    system = _answer(home, "Is my system working")
    assert system.ok is False and "failed" in system.text and home.status == "PROBLEM"


async def test_algo_trading_off_in_mt5_is_a_problem(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        emu.terminal_info = lambda: SimpleNamespace(  # type: ignore[method-assign]
            connected=True, trade_allowed=False, tradeapi_disabled=False, build=6264
        )
        await asyncio.sleep(0.5)
        await _beat(h)
        home = await _home(h)
    broker = _answer(home, "Is my broker connected")
    assert broker.ok is False and "Algo Trading is off" in broker.text
    assert home.status == "PROBLEM"


async def test_the_price_is_labelled_delayed_and_the_check_dated(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _beat(h)
        await h.env.svc.reconcile_all(trigger="test")
        h.env.clock.advance(days=3)
        emu.now_ms = int(h.env.clock.now().timestamp() * 1000)
        await asyncio.sleep(0.5)
        await _beat(h)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            page = (await c.get("/")).text
    start = page.index('id="home-hfm"')
    words = re.sub(
        r"\s+", " ", re.sub(r"<[^>]+>", " ", page[start : page.index("</section>", start)])
    )
    assert "DELAYED" in words.split("Gold price", 1)[1].split("Open trades", 1)[0]
    assert "3 days ago" in words.split("Last successful check", 1)[1].split("Next decision", 1)[0]


async def _breakout_hours(h: Hfm, n: int = 80) -> None:
    """PAXG hourly bars, quiet, then a close above the prior 20-hour high on the last one."""
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
                "'1h', :t, 4100, :h, 4090, :c, 1, true, :r, :n, :n) ON CONFLICT DO NOTHING"
            ),
            [
                {
                    "i": inst,
                    "s": src,
                    "t": last - timedelta(hours=k),
                    "r": run,
                    "n": now,
                    "c": 4120 if k == 0 else 4100,
                    "h": 4125 if k == 0 else 4110,
                }
                for k in range(n)
            ],
        )


async def _signal(h: Hfm, emu: MT5Emulator, mode: str) -> list[str]:
    h.env.clock.advance(days=1, hours=17)  # Monday 2026-10-12 14:06 UTC: gold trades
    emu.now_ms = int(h.env.clock.now().timestamp() * 1000)
    await paxg(h.env.db, h.env.clock, "4100.10", "4100.40")
    await _breakout_hours(h)
    await _grant(h, "20000", mode)
    await asyncio.sleep(0.5)
    await _beat(h)
    await h.env.svc.reconcile_all(trigger="test")
    return await auto.run(h.env.db, h.env.clock)


async def test_a_trade_waiting_for_the_owner_is_said_so(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """An ASK auto-trader proposed and waits: never "sent to HFM", and it needs the owner."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        assert await _signal(h, emu, "ASK") == [f"{XAU}:ASKED"]
        home = await _home(h)
    assert home.decision == "BUY" and home.decision_text.startswith("waiting for your approval")
    assert any("wait for your approval" in t for t in home.attention)
    assert _answer(home, "Does anything need my attention").ok is False
    assert home.status == "NEEDS YOU"


async def test_a_trade_sent_and_filled_is_said_so(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        assert await _signal(h, emu, "AUTO") == [f"{XAU}:APPROVED"]
        for _ in range(3):  # sent, filled, its stop recorded
            await h.env.svc.execution.run_cycle()
        await asyncio.sleep(0.5)
        home = await _home(h)
    assert home.decision == "BUY" and home.decision_text.startswith("sent to HFM and filled at")
    assert [p.symbol for p in home.positions] == ["XAUUSD"] and home.positions[0].stop
    assert _answer(home, "Is my money protected").ok is True


@pytest.fixture
async def plain(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def test_without_an_hfm_account_every_answer_is_said(plain: Env) -> None:
    async with plain.db.connect() as conn:
        home = await hfm_home.build(conn, plain.clock.now())
    assert len(home.answers) == 5 and all(a.text for a in home.answers)
    assert home.protection and home.market and home.status == "NEEDS YOU"


async def test_a_failing_panel_leaves_the_page_working(
    plain: Env, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(conn: Any, _now: Any, **_: Any) -> Any:
        await conn.execute(text("SELECT 1/0"))

    monkeypatch.setattr(hfm_home, "build", broken)
    app = create_app(settings, db=plain.db, clock=plain.clock)
    async with client_for(app) as c:
        await full_login(c, plain.owner, plain.clock)
        r = await c.get("/")
    assert r.status_code == 200 and "could not be read" in r.text and "Paper rehearsals" in r.text


# ---- found by the second review (2026-11-03) -----------------------------------------------------


async def test_mt5_logged_out_is_never_a_yes(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """MT5 closed or logged out: the bridge still reports, with an empty account and no
    positions, while HFM holds the trade."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await buy_gold(h)
        emu.account_info = lambda: None  # type: ignore[method-assign]
        emu.positions_get = lambda **_: None  # type: ignore[method-assign]
        emu.terminal_info = lambda: SimpleNamespace(  # type: ignore[method-assign]
            connected=False, trade_allowed=False, tradeapi_disabled=False, build=6264
        )
        await asyncio.sleep(0.5)
        await _beat(h)
        home = await _home(h)
    assert _answer(home, "Is my money protected").ok is None and not home.fresh
    assert home.status == "PROBLEM"


async def test_trades_the_bridge_does_not_follow_are_found(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """A trade opened by hand on a symbol the bridge does not follow shows in HFM's equity and
    margin: protection is not a yes."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    real = emu.account_info
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        emu.account_info = lambda: SimpleNamespace(  # type: ignore[method-assign]
            **{**vars(real()), "equity": real().balance - 1000, "margin": 50.0}
        )
        await asyncio.sleep(0.5)
        await _beat(h)
        home = await _home(h)
    protected = _answer(home, "Is my money protected")
    assert protected.ok is False and "does not follow" in protected.text
    assert home.status == "PROBLEM"


async def test_a_real_account_is_named_before_algo_trading(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    from tests.mt5_emulator import ACCOUNT_TRADE_MODE_REAL

    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        emu.trade_mode = ACCOUNT_TRADE_MODE_REAL
        emu.terminal_info = lambda: SimpleNamespace(  # type: ignore[method-assign]
            connected=True, trade_allowed=False, tradeapi_disabled=False, build=6264
        )
        await asyncio.sleep(0.5)
        await _beat(h)
        home = await _home(h)
    assert "REAL account" in _answer(home, "Is my broker connected").text and home.mode == "REAL"


async def test_with_the_worker_down_nothing_is_running(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _paxg_hours(h)
        await _grant(h, "20000")
        home = await _home(h)  # no heartbeat from the worker
    running = _answer(home, "Is automatic trading running")
    assert running.ok is False and "worker" in running.text


async def test_an_hourly_skip_says_why(settings: Settings, db: Database, tmp_path: Path) -> None:
    """As on the owner's demo: $100 for 1 oz of gold. The skip's reason is shown (the notice's
    stored key carries the owner and channel)."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        h.env.clock.advance(days=1, hours=17)
        emu.now_ms = int(h.env.clock.now().timestamp() * 1000)
        await paxg(h.env.db, h.env.clock, "4100.10", "4100.40")
        await _breakout_hours(h)
        await _grant(h, "100")
        await asyncio.sleep(0.5)
        await _beat(h)
        await h.env.svc.reconcile_all(trigger="test")
        assert await auto.run(h.env.db, h.env.clock) == [f"{XAU}:NO_TRADE"]
        home = await _home(h)
    assert home.decision == "BUY" and "signalled, but not traded: the smallest trade" in (
        home.decision_text
    )


async def test_a_filled_buy_shows_its_entry_price_after_its_stop_exists(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    from cosmic.db.schema_execution import orders

    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        assert await _signal(h, emu, "AUTO") == [f"{XAU}:APPROVED"]
        await h.env.svc.execution.run_cycle()
        h.env.clock.advance(minutes=1)  # the stop order is written later than the entry
        for _ in range(3):
            await h.env.svc.execution.run_cycle()
        await asyncio.sleep(0.5)
        home = await _home(h)
        async with db.connect() as conn:
            entry = await conn.scalar(
                select(orders.c.avg_fill_price).where(orders.c.purpose == "ENTRY")
            )
    assert entry is not None and f"filled at {entry:,.2f}" in home.decision_text


async def test_another_mt5_account_is_a_problem_on_the_panel(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        emu.login = 87654321
        await asyncio.sleep(0.5)
        await _beat(h)
        home = await _home(h)
    broker = _answer(home, "Is my broker connected")
    assert broker.ok is False and "different HFM account" in broker.text
    assert home.status == "PROBLEM"


async def test_a_check_still_running_is_not_a_failed_one(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _beat(h)
        await h.env.svc.reconcile_all(trigger="test")
        async with db.transaction() as conn:
            await conn.execute(
                reconciliation_runs.insert().values(
                    id=uuid7(),
                    account_id=h.env.account_id,
                    trigger="scheduled",
                    started_at=h.env.clock.now() + timedelta(seconds=1),
                    status="RUNNING",
                )
            )
        h.env.clock.advance(seconds=2)
        await _beat(h)
        home = await _home(h)
    assert _answer(home, "Is my system working").ok is True
