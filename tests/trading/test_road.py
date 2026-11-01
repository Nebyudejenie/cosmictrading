"""The road to real money (S43): what is done, what is left, and an honest estimate at the
recent pace: no date without a pace, or while the fill comparison fails."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Role
from cosmic.notify.telegram.handlers import TelegramHandlers, Update
from cosmic.notify.telegram.linking import create_link_code
from cosmic.reporting import road
from cosmic.trading import live
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login, make_user, principal_for
from tests.trading_env import Env, build_env

NOW = datetime(2026, 10, 30, tzinfo=UTC)
TG_ID = 555_000_333


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


def _road(**kw: object) -> road.Road:
    base: dict[str, object] = {
        "now": NOW,
        "gates_passed": [],
        "gates_left": [],
        "locks_open": 0,
        "locks_total": 6,
        "locks_left": [],
        "trades": road.Progress(0, 20, 0, None),
        "first_trade": None,
        "days": 0,
        "fills": road.Progress(0, 20, 0, None),
    }
    base.update(kw)
    way = road.Road(**base)  # type: ignore[arg-type]
    way.steps = road._steps(way)
    return way


def _lock(number: int, name: str, who: str) -> live.Lock:
    return live.Lock(number, name, "LOCKED", "", who)


def test_an_estimate_is_the_recent_pace_and_never_a_guess() -> None:
    assert road.estimate(20, 20, 0, NOW) == NOW  # reached
    assert road.estimate(5, 20, 0, NOW) is None  # no pace: no date
    # 9 in the last 90 days is 0.1 a day: 15 more take 150 days
    assert road.estimate(5, 20, 9, NOW) == NOW + timedelta(days=150)
    trades = road.Progress(5, 20, 9, NOW + timedelta(days=150))
    fills = road.Progress(18, 20, 30, NOW + timedelta(days=6))
    way = _road(trades=trades, fills=fills, first_trade=NOW - timedelta(days=10), days=10)
    assert way.paper_ready == NOW + timedelta(days=150)  # the slower of the two
    quick = _road(
        trades=road.Progress(20, 20, 20, NOW),
        fills=fills,
        first_trade=NOW - timedelta(days=3),
        days=3,
    )
    assert quick.paper_ready == NOW + timedelta(days=27)  # the 30 days still have to pass
    assert _road(fills=fills).paper_ready is None


def test_a_young_series_is_paced_over_its_own_life_a_week_at_least() -> None:
    assert road.pace_window(None, NOW) == road.PACE_WINDOW
    assert road.pace_window(NOW - timedelta(days=400), NOW) == timedelta(days=90)
    assert road.pace_window(NOW - timedelta(days=10), NOW) == timedelta(days=10)
    assert road.pace_window(NOW - timedelta(hours=5), NOW) == timedelta(days=7)
    # 10 trades in a 10-day-old series: 10 more take 10 days, not 90
    window = road.pace_window(NOW - timedelta(days=10), NOW)
    assert road.estimate(10, 20, 10, NOW, window) == NOW + timedelta(days=10)


def test_a_date_is_rounded_up_and_says_utc() -> None:
    assert road.day(datetime(2026, 11, 2, tzinfo=UTC)) == "02 Nov 2026 UTC"
    assert road.day(datetime(2026, 11, 2, 0, 0, 1, tzinfo=UTC)) == "03 Nov 2026 UTC"


def test_a_fill_comparison_problem_gives_no_date_and_says_why() -> None:
    met = road.Progress(20, 20, 20, NOW)
    way = _road(
        trades=met,
        fills=met,
        first_trade=NOW - timedelta(days=40),
        days=40,
        fill_problems=["2 fill(s) on a book too thin for their size"],
    )
    assert way.paper_ready is None and not way.evidence_holds
    assert way.steps == [
        "Gate G's fill comparison does not hold yet: 2 fill(s) on a book too thin for their size."
    ]
    assert "Gate G's evidence: no estimate yet" in road.telegram_lines(way)


def test_the_steps_follow_gate_g_from_waiting_to_recording() -> None:
    met = road.Progress(20, 20, 20, NOW)
    waiting = _road(trades=met, fills=met, first_trade=NOW - timedelta(days=12), days=12)
    assert waiting.steps == ["Wait for 30 days since the first closed trade: 17 Nov 2026 UTC."]
    assert "Gate G's evidence could hold about 17 Nov 2026 UTC (an estimate)" in (
        road.telegram_lines(waiting)
    )
    holds = _road(trades=met, fills=met, first_trade=NOW - timedelta(days=31), days=31)
    assert holds.evidence_holds
    assert holds.steps == ["Record the evidence for gate G (Go-live: what passes each)."]
    assert "Gate G's evidence holds now" in road.telegram_lines(holds)
    passed = _road(trades=met, fills=met, gates_passed=["G"], first_trade=NOW, days=0)
    assert passed.steps == []
    lines = road.telegram_lines(passed)
    assert "Gate G's evidence holds now" in lines
    assert "Gates passed: G; still to pass: none" in lines


def test_the_steps_follow_the_locks_still_locked() -> None:
    mode = _lock(2, "The server allows a live mode", "you, in the server's env file")
    key = _lock(5, "A key that can only trade spot", "you, in the venue's key settings")
    gates = _lock(4, "Go-live gates A to H", "recorded evidence")
    with_key = _road(locks_left=[mode, gates, key])
    assert with_key.steps[1:] == [  # lock 4 is the gates: the steps before it
        "Lock 2: The server allows a live mode. Who opens it: you, in the server's env file.",
        "Ask your internet provider whether the server's outgoing address is fixed (B-11): "
        "the real-money key must be locked to it.",
        "Lock 5: A key that can only trade spot. Who opens it: you, in the venue's key settings.",
    ]
    assert not any("B-11" in s for s in _road(locks_left=[mode]).steps)


def test_a_rehearsal_parted_from_its_replay_holds_back_gate_g() -> None:
    met = road.Progress(20, 20, 20, NOW)
    parted = _road(
        trades=met,
        fills=met,
        first_trade=NOW - timedelta(days=31),
        days=31,
        off_replay=[("rehearsal-binance-100", "2026-11-20")],
    )
    assert parted.paper_ready is None and not parted.evidence_holds
    assert parted.steps == [
        "Find why rehearsal-binance-100 did not follow its replay (latest on 2026-11-20; Start "
        "with $100 → Your rehearsals), then acknowledge its alert: gate G's evidence assumes the "
        "system does what its simulation says."
    ]
    assert "Gate G's evidence: no estimate yet" in road.telegram_lines(parted)


async def test_the_go_live_page_and_telegram_show_the_road(env: Env) -> None:
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/golive")).text
    assert "The road to real money" in page
    assert "Closed simulated trades: 0 of 20" in page
    assert "no estimate yet" in page and "not a promise" not in page
    assert "Ask your internet provider" in page  # B-11
    async with env.db.transaction() as conn:
        code = await create_link_code(
            conn, principal=principal_for(env.owner, env.clock), now=env.clock.now()
        )
    h = TelegramHandlers(env.db, env.clock, env.settings, env.svc)
    await h.handle(Update(TG_ID, TG_ID, "private", text=f"/link {code}"))
    answer = (await h.handle(Update(TG_ID, TG_ID, "private", text="/golive")))[0].text
    assert answer.startswith("The road to real money (live trading is DISABLED)")
    assert "Locks open: " in answer and "Gate G's evidence: no estimate yet" in answer
    viewer = await make_user(env.db, env.clock, username="viewer", roles=[Role.VIEWER])
    async with env.db.transaction() as conn:
        code = await create_link_code(
            conn, principal=principal_for(viewer, env.clock), now=env.clock.now()
        )
    await h.handle(Update(TG_ID + 1, TG_ID + 1, "private", text=f"/link {code}"))
    refused = (await h.handle(Update(TG_ID + 1, TG_ID + 1, "private", text="/golive")))[0].text
    assert refused.startswith("Not allowed: this needs the right to read accounts")
