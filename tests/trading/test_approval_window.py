"""How long the owner has to approve a real-money proposal (S51): for each 00:00 UTC open of the
real-money market, how often the price N hours later was still where the approved order would
buy as proposed: from 0.5 % below the proposal up to its limit, the ask + 0.1 %."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.reporting import approval_window, road
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading_env import Env, build_env

D = Decimal
START = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    e = await build_env(settings, db)
    e.clock.set(datetime(2026, 11, 2, 12, tzinfo=UTC))
    return e


def _days(
    moves: list[float], *, opens_at: list[float] | None = None
) -> tuple[tuple[datetime, ...], tuple[float, ...]]:
    """One day per move: it opens at ``opens_at[d]`` (100 by default) and from hour 6 on the
    price is that open × (1 + move); before hour 6 it stays at the open."""
    times: list[datetime] = []
    opens: list[float] = []
    for d, move in enumerate(moves):
        base = opens_at[d] if opens_at else 100.0
        for h in range(24):
            times.append(START + timedelta(days=d, hours=h))
            opens.append(base * (1 + move) if h >= 6 else base)
    return tuple(times), tuple(opens)


def test_bought_as_proposed_only_between_the_band_and_the_limit() -> None:
    # 10 days each: up 0.6 % (beyond the band), up 0.3 % (in the band, above the limit: the
    # order could not buy), up 0.05 % and down 0.3 % (bought)
    moves = [0.006] * 10 + [0.003] * 10 + [0.0005] * 10 + [-0.003] * 10
    w = approval_window.measure(*_days(moves))
    assert w is not None and (w.days, w.since) == (40, START.date())
    assert (w.below, w.above) == (D("0.5"), D("0.1"))
    assert {h: w.share(h) for h in approval_window.HOURS} == {
        1: 100,
        3: 100,
        6: 50,
        12: 50,
        18: 50,
    }
    assert approval_window.measure(*_days([0.0] * 29)) is None  # too few days to say
    times, opens = _days([0.0] * 40)
    gappy = (times[:30] + times[31:], opens[:30] + opens[31:])  # day 2 lacks hour 6
    assert approval_window.measure(*gappy).days == 39  # type: ignore[union-attr]


def test_shares_are_rounded_down_from_the_counts() -> None:
    w = approval_window.measure(*_days([0.0] * 29 + [0.01] * 71))  # 29 of 100 days bought
    assert w is not None and w.share(6) == 29  # never 28 from a float, never 30


def test_after_a_busy_day_apart() -> None:
    # every other day opens 2 % away from the day before: busy days, and those move away
    opens_at = [100.0 if d % 2 == 0 else 102.0 for d in range(60)]
    moves = [0.0 if d % 2 == 0 else 0.006 for d in range(60)]
    w = approval_window.measure(*_days(moves, opens_at=opens_at))
    assert w is not None and w.busy_days == 59  # the first day has no day before it
    assert w.share(6) == 50 and w.busy_share(6) == 49  # 29 of 59 busy days were bought
    few = approval_window.measure(*_days([0.0] * 40))
    assert few is not None and few.busy_days == 0 and few.busy_share(6) is None


async def _store_hourly(env: Env, times: tuple[datetime, ...], opens: tuple[float, ...]) -> None:
    async with env.db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key == approval_window.MARKET)
        )
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == "binance_public"))
        run, now = uuid7(), env.clock.now()
        await conn.execute(
            fetch_runs.insert().values(
                id=run,
                source_id=src,
                kind="test_bars",
                started_at=now,
                finished_at=now,
                status="SUCCESS",
            )
        )
        params: list[dict[str, Any]] = [
            {
                "i": inst,
                "s": src,
                "t": t,
                "o": D(f"{o:.2f}"),
                "h": D(f"{o + 0.05:.2f}"),
                "l": D(f"{o - 0.05:.2f}"),
                "c": D(f"{o:.2f}"),
                "r": run,
                "n": now,
            }
            for t, o in zip(times, opens, strict=True)
        ]
        await conn.execute(
            text(
                "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, "
                "close, volume, is_complete, fetch_run_id, ingested_at, updated_at) "
                "VALUES (:i, :s, '1h', :t, :o, :h, :l, :c, 1, true, :r, :n, :n)"
            ),
            params,
        )


async def test_the_road_shows_it_on_the_page_and_on_telegram(
    env: Env, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with env.db.connect() as conn:
        assert (await road.build(conn, env.settings, env.clock.now())).window is None  # no bars
    moves = [0.006] * 10 + [0.003] * 10 + [0.0005] * 10 + [-0.003] * 10
    await _store_hourly(env, *_days(moves, opens_at=[4000.0] * 40))
    async with env.db.connect() as conn:
        way = await road.build(conn, env.settings, env.clock.now())
    assert way.window is not None and way.window.share(6) == 50
    (line,) = road.window_lines(way.window)
    assert line.startswith(
        "Approving a real-money signal in time (HISTORICAL, Binance gold, 40 days since "
        "01 Sep 2026; not a promise)"
    )
    assert "(price between -0.5 % and +0.1 %) after 1 h 100 %, 3 h 100 %, 6 h 50 %" in line
    assert any(x.startswith("Approving a real-money signal") for x in road.telegram_lines(way))
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/golive")).text
    assert "How long you will have to approve" in html and "HISTORICAL" in html
    assert '<th scope="row">6 hours</th><td class="num">50 %</td>' in html
    assert "Not a promise" in html

    async def broken(_conn: Any) -> None:
        raise RuntimeError("a fault in the measure")

    monkeypatch.setattr(approval_window, "build", broken)
    async with env.db.connect() as conn:  # the road and its connection carry on without it
        way = await road.build(conn, env.settings, env.clock.now())
        assert way.window is None and way.steps
        assert await conn.scalar(select(instruments.c.id).limit(1)) is not None
