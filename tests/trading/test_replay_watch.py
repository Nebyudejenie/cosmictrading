"""The replay watch (S45): a rehearsal run that parts from its own replay is told once, each later
parting day once more, never twice for a day that comes and goes; the alert ends when the run
ends or is renewed."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, notifications
from cosmic.reporting import autotrader, replay_watch, road
from cosmic.reporting import rehearsal as page
from cosmic.trading import rehearsal
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from tests.trading.test_rehearsal import _expectation, _replayed, set_binance_fee, start
from tests.trading_env import Env, build_env

D = Decimal
ALIAS = rehearsal.ALIAS
RUN = uuid.UUID("00000000-0000-7000-8000-000000000001")
KEY = f"{replay_watch.PREFIX}{ALIAS}:{RUN}"
FIRST = "The $100 rehearsal on Binance gold (PAXG) did not follow its replay"
AGAIN = "The $100 rehearsal on Binance gold (PAXG) parted from its replay again"


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


def _at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def _run(*days: date, mandate: uuid.UUID = RUN, ended: bool = False) -> replay_watch.Run:
    return replay_watch.Run(
        ALIAS, "PAPER", "Binance gold (PAXG)", mandate, ended, days, "It did not follow ..."
    )


async def _told(env: Env) -> list[str]:
    """The titles of the alerts and notices sent about partings (portal copies, oldest first)."""
    async with env.db.connect() as conn:
        return [
            str(t)
            for (t,) in await conn.execute(
                select(notifications.c.payload["alert"]["title"].astext)
                .where(
                    notifications.c.payload["alert"]["title"].astext.contains("replay"),
                    notifications.c.channel == "PORTAL",
                )
                .order_by(notifications.c.created_at)
            )
        ]


async def _states(env: Env) -> dict[str, str]:
    async with env.db.connect() as conn:
        rows = await conn.execute(
            select(incidents.c.dedup_key, incidents.c.status).where(
                incidents.c.dedup_key.startswith(replay_watch.PREFIX)
            )
        )
        return {r.dedup_key: r.status for r in rows}


async def test_a_run_is_told_once_each_later_day_once_and_never_twice_for_a_day(
    env: Env,
) -> None:
    async def tell(*found: replay_watch.Run) -> None:
        env.clock.advance(hours=6)
        async with env.db.transaction() as conn:
            await replay_watch.tell(conn, env.clock.now(), list(found))

    nov20, dec4 = date(2026, 11, 20), date(2026, 12, 4)
    await tell(_run(nov20))
    assert await _states(env) == {KEY: "OPEN"}
    assert await _told(env) == [FIRST]
    await tell(_run(nov20))  # six hours later, the same parting: not told again
    await tell(_run())  # the past worked out again now matches: the alert stays
    await tell(_run(nov20))  # and parts again: still not told twice
    assert await _told(env) == [FIRST]
    await tell(_run(nov20, dec4))  # a later day: told once, on the same alert
    await tell(_run(nov20, dec4))
    assert await _told(env) == [FIRST, AGAIN]
    assert await _states(env) == {KEY: "OPEN"}
    async with env.db.connect() as conn:
        detail = await conn.scalar(select(incidents.c.detail).where(incidents.c.dedup_key == KEY))
    assert detail["days"] == ["2026-11-20", "2026-12-04"]
    await tell()  # the replay could not run: nothing changes
    assert await _states(env) == {KEY: "OPEN"}
    renewed = uuid.UUID("00000000-0000-7000-8000-000000000002")
    await tell(_run(mandate=renewed))  # renewed: the old run's alert ends, nothing new to tell
    assert await _states(env) == {KEY: "RESOLVED"}
    await tell(_run(dec4, mandate=renewed))  # the new run parts: a new alert
    new_key = f"{replay_watch.PREFIX}{ALIAS}:{renewed}"
    assert (await _states(env))[new_key] == "OPEN"
    await tell(_run(dec4, mandate=renewed, ended=True))  # the run ended (stopped, expired)
    assert (await _states(env))[new_key] == "RESOLVED"
    assert await _told(env) == [FIRST, AGAIN, FIRST]


async def test_the_worker_watches_each_rehearsal_on_its_own_and_the_road_says_so(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_binance_fee(env, cent=True)
    await start(env)
    await start(env, venue="hfm-cent")
    live = [
        page.Leg(_at("2026-11-03T00:05:00"), D("2.40"), _at("2026-11-10T00:05:00"), "rule exit")
    ]
    replay = [
        _replayed("2026-11-03T00:00:00", 2.5, "rule exit", "2026-11-10T00:00:00"),
        _replayed("2026-11-20T00:00:00", -1.96, "stop-loss", "2026-11-22T00:00:00"),
    ]
    parted = page.pair_trades(live, replay, _at("2026-12-01T00:00:00"))
    runs: dict[str, uuid.UUID] = {}

    async def track(_conn: Any, now: datetime, st: page.RehearsalState) -> page.Tracking:
        if st.venue.key == "hfm-cent":
            raise ConflictError("no valid risk policy")  # one rehearsal that cannot be compared
        assert st.account is not None and st.board is not None and st.board.mandate is not None
        runs[st.account.alias] = st.board.mandate.id
        return page.Tracking(
            _at("2026-11-01T00:00:00"), now, _expectation(2), parted, st.board.mandate.id
        )

    monkeypatch.setattr(page, "_track", track)
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="test")
    job = build_registry(ctx).handlers["rehearsal.replay_watch"]
    await job(ctx, {})
    key = f"{replay_watch.PREFIX}{ALIAS}:{runs[ALIAS]}"
    assert await _states(env) == {key: "OPEN"}  # the other rehearsal's failure hid nothing
    async with env.db.connect() as conn:
        alert = await conn.scalar(select(incidents.c.detail).where(incidents.c.dedup_key == key))
        way = await road.build(conn, env.settings, env.clock.now())
    assert alert["days"] == ["2026-11-20"]
    assert alert["alert"]["account"] == f"{ALIAS} [SIMULATED · PAPER]"
    assert "the replay traded on 1 day(s) it did not (20 Nov)" in alert["alert"]["what_happened"]
    assert way.off_replay == [(ALIAS, "2026-11-20")]
    assert any(s.startswith(f"Find why {ALIAS} did not follow its replay") for s in way.steps)
    async with env.db.transaction() as conn:  # the owner looked: acknowledged
        await conn.execute(
            update(incidents).where(incidents.c.dedup_key == key).values(status="ACKNOWLEDGED")
        )
    async with env.db.connect() as conn:
        assert (await road.build(conn, env.settings, env.clock.now())).off_replay == []
    env.clock.advance(hours=6)
    await job(ctx, {})  # nothing new: it stays acknowledged and is not told again
    assert await _states(env) == {key: "ACKNOWLEDGED"}
    assert await _told(env) == [FIRST]


def test_a_rule_exit_is_the_same_exit_on_both_sides() -> None:
    """The live side names an exit order "exit"; the replay, "rule exit": the same exit."""
    assert autotrader._how({"exits": {"x": {"purpose": "EXIT", "filled": "0.01"}}}) == "exit"
    board: Any = SimpleNamespace(
        closed=[
            SimpleNamespace(
                opened_at=_at("2026-11-03T00:05:00"),
                pnl=D("2.40"),
                closed_at=_at("2026-11-10T00:05:00"),
                how="exit",
                auto=True,
            )
        ],
        open=[],
    )
    legs = page.live_legs(board, _at("2026-11-01T00:00:00"), _at("2026-12-01T00:00:00"))
    assert [leg.how for leg in legs] == ["rule exit"]
    replay = [_replayed("2026-11-03T00:00:00", 2.5, "rule exit", "2026-11-10T00:00:00")]
    (pair,) = page.pair_trades(legs, replay, _at("2026-12-01T00:00:00"))
    assert pair.matched and pair.same_exit and not pair.differs


def test_a_venue_closed_at_weekends_compares_closes_by_session() -> None:
    """Gold gapping through a stop at Sunday evening's reopening: the live stop fills on Sunday,
    the replay (which skips weekend bars) at Monday's open. The same exit."""
    sunday_evening = _at("2026-11-08T22:05:00")
    live = [page.Leg(_at("2026-11-02T00:05:00"), D("-2"), sunday_evening, "stop-loss")]
    replay = [_replayed("2026-11-02T00:00:00", -2.1, "stop-loss", "2026-11-09T00:00:00")]
    to = _at("2026-11-20T00:00:00")
    (cfd,) = page.pair_trades(live, replay, to, closed_weekends=True)
    assert cfd.same_exit and not cfd.differs
    (spot,) = page.pair_trades(live, replay, to)  # a market open every day: Sunday is Sunday
    assert spot.differs
    assert page.session_day(_at("2026-11-06T22:00:00"), True) == date(2026, 11, 6)  # Friday
    assert page.session_day(_at("2026-11-07T10:00:00"), True) == date(2026, 11, 9)  # Saturday


async def test_nothing_started_is_nothing_compared(env: Env) -> None:
    async with env.db.connect() as conn:
        assert await replay_watch.runs(conn, env.clock.now()) == []
