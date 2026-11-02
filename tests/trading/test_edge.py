"""Is the rule's edge real? (S47): nearby settings, switching rules and each rule's forward record
since the daily rules were registered, all from one bench of prices."""

from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.market.jobs import build_ingest_service
from cosmic.reporting import autotrader, edge
from cosmic.reporting import rehearsal as page
from cosmic.research.library import expect
from cosmic.research.library.catalog import BY_KEY
from cosmic.trading import mandates
from cosmic.trading import rehearsal as r
from cosmic.web.app import create_app
from cosmic.worker.context import WorkerContext
from tests.helpers import client_for, full_login
from tests.market_mock import MarketMock
from tests.trading_env import Env, build_env

D = Decimal
PAXG = "SPOT:PAXG/USDT@BINANCE"
NOW = datetime(2027, 3, 1, 12, tzinfo=UTC)  # four months after the rules were registered
PINNED_TRADES, PINNED_TOTAL = 9, 16.21  # Turtle 55/20 on the seeded series below


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    e = await build_env(settings, db)
    e.clock.set(NOW)
    ctx = WorkerContext(settings=e.settings, db=e.db, clock=e.clock, worker_id="test")
    await build_ingest_service(ctx, transport=MarketMock().transport()).sync_instruments(
        "binance_public"
    )  # the venue's lot sizes, which a replay needs
    return e


def _at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def _trade(opened: str, pnl: float, how: str = "rule exit") -> expect.Trade:
    t = _at(opened)
    return expect.Trade(PAXG, t, t + timedelta(days=9), 0.01, 1.0, 0.9, 1.0, 2.0, pnl, how)


def test_nearby_settings_are_the_rules_numbers_shorter_and_longer() -> None:
    assert edge.nearby_settings(BY_KEY["turtle_55_20"]) == [
        {"entry": e, "exit": x} for e in (44, 55, 66) for x in (16, 20, 24)
    ]
    assert [p["n"] for p in edge.nearby_settings(BY_KEY["ema_200_trend"])] == [
        150,
        175,
        200,
        225,
        250,
    ]
    assert edge.nearby_settings(BY_KEY["trend_ensemble_6"]) == []  # nothing single to vary


class _Bench:
    """A bench whose replays are a table: each rule's result as it stood on a date (kept
    running), and what it makes started afresh in a year."""

    def __init__(
        self,
        start: datetime,
        kept: dict[str, list[tuple[str, float]]],
        fresh: dict[tuple[str, int], float],
    ) -> None:
        self.start, self.kept, self.fresh = start, kept, fresh

    def run(
        self,
        spec: Any,
        *,
        total_stop: Any = autotrader.KEEP,
        since: datetime | None = None,
        until: Any = autotrader.KEEP,
    ) -> autotrader.Expectation:
        assert total_stop is None  # switching runs without the stop for good
        if since is not None:
            total = self.fresh[(spec.key, since.year)]
        else:
            total = 0.0
            for day, value in self.kept[spec.key]:  # the last value known before ``until``
                if until is autotrader.KEEP or until is None or _at(day) < until:
                    total = value
        return _with_total(total)


def _with_total(total: float) -> autotrader.Expectation:
    t = _at("2020-01-01")
    summary = expect.summarize(expect.Result(t, t, 1, [_trade("2020-01-01", total)]))
    e = autotrader.Expectation(summary, D(100), D(2), [], [], [], timeframe=mandates.DAILY)
    return e


A, B = "turtle_55_20", "ema_200_trend"


def test_switching_picks_as_it_stood_on_1_january_never_with_hindsight() -> None:
    """B's trade opened in December 2022 closes in November 2023 at +40: on 1 January 2023 it
    was still open at −1, so A (+5 by then) is picked; and B, restarted, waits for a new signal."""
    kept = {
        A: [("2021-06-01", 5.0), ("2023-06-01", 13.0), ("2024-06-01", 14.0)],
        B: [("2021-06-01", 0.0), ("2022-12-31", -1.0), ("2023-11-30", 40.0)],
    }
    fresh = {(A, 2023): 8.0, (B, 2023): 0.0, (A, 2024): 1.0, (B, 2024): -2.0}
    b: Any = _Bench(_at("2021-03-01"), kept, fresh)
    unstopped = {A: _with_total(14.0), B: _with_total(40.0)}
    sw = edge.switching(b, [A, B], _at("2024-07-01"), unstopped)
    assert sw is not None
    # 2022 begins less than a year after the start: the first pick is for 2023
    assert [(y.year, y.picked, y.made, y.so_far) for y in sw.years] == [
        (2023, A, 8.0, False),
        (2024, B, -2.0, True),  # +40 against +13 on 1 January 2024
    ]
    assert sw.years[0].known == {A: 5.0, B: -1.0}
    assert sw.years[0].kept == {A: 8.0, B: 41.0} and sw.years[1].kept == {A: 1.0, B: 0.0}
    assert sw.total == 6.0 and sw.kept_all == {A: 9.0, B: 41.0} and sw.beaten == 0
    assert sw.restarted == {A: 9.0, B: -2.0}  # B restarted each year misses its own trend
    late: Any = _Bench(_at("2023-06-01"), kept, fresh)
    assert edge.switching(late, [A, B], _at("2024-03-01"), unstopped) is None


def test_how_usual_a_record_is_against_the_rules_history() -> None:
    history = [2.0, -1.0, -1.0, -1.0]  # a trend rule: one big win, many small losses
    assert edge.as_bad(history, -1.0, 1) == pytest.approx(0.75, abs=0.02)
    assert edge.as_bad(history, -3.0, 3) == pytest.approx(0.75**3, abs=0.02)
    assert edge.as_bad(history, -9.0, 3) == 0.0
    assert edge.as_bad(history, 0.0, 0) is None and edge.as_bad([], -1.0, 2) is None
    assert edge.as_bad(history, -2.0, 2) == edge.as_bad(history, -2.0, 2)  # seeded


def test_the_forward_record_counts_closed_trades_against_their_risk() -> None:
    e = autotrader.Expectation(None, D(100), D(2), [], [], [], timeframe=mandates.DAILY)
    e = replace(
        e,
        trades=[
            _trade("2026-11-02", -2.0, "stop-loss"),  # a full loss: −1 of its risk
            _trade("2026-12-01", 5.0),
            _trade("2027-02-20", 0.7, "still open"),
        ],
    )
    # the history's half-size losses (risk 1.00) count as −1 of their risk too
    half = [replace(_trade("2024-01-01", -1.0), risk=1.0)] * 3 + [_trade("2024-03-01", 8.0)]
    history = edge.in_risk(half)
    assert history == [-1.0, -1.0, -1.0, 4.0]
    f = edge.forward(A, e, history)
    assert (f.trades, f.wins, f.total, f.open_result) == (2, 1, pytest.approx(3.0), 0.7)
    assert f.as_bad is not None and not f.unusual
    bad = edge.forward(A, replace(e, trades=e.trades[:1] * 6), [4.0, 4.0, -0.1])
    assert bad.unusual and bad.as_bad == 0.0  # six full losses: never in a history like this


def _gold(days: int, seed: int = 11) -> list[tuple[float, float, float, float]]:
    """Daily gold with long trends both ways (a few hundred days each) and noise."""
    rng = random.Random(seed)
    price, rows = 1800.0, []
    for i in range(days):
        drift = 0.0012 if (i // 220) % 3 != 1 else -0.0009
        close = price * math.exp(drift + rng.gauss(0, 0.009))
        high = max(price, close) * math.exp(abs(rng.gauss(0, 0.004)))
        low = min(price, close) * math.exp(-abs(rng.gauss(0, 0.004)))
        rows.append((price, high, low, close))
        price = close
    return rows


async def _store_gold(env: Env, rows: list[tuple[float, float, float, float]]) -> None:
    last_open = mandates.day_start(env.clock.now()) - timedelta(days=1)
    async with env.db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key == PAXG)
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
        first = last_open - timedelta(days=len(rows) - 1)
        params: list[dict[str, Any]] = [
            {
                "i": inst,
                "s": src,
                "t": first + timedelta(days=k),
                "o": D(f"{o:.2f}"),
                "h": D(f"{h:.2f}"),
                "l": D(f"{lo:.2f}"),
                "c": D(f"{c:.2f}"),
                "r": run,
                "n": now,
            }
            for k, (o, h, lo, c) in enumerate(rows)
        ]
        await conn.execute(
            text(
                "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, "
                "close, volume, is_complete, fetch_run_id, ingested_at, updated_at) "
                "VALUES (:i, :s, '1d', :t, :o, :h, :l, :c, 1, true, :r, :n, :n)"
            ),
            params,
        )


async def test_one_bench_gives_the_same_replay_and_the_three_views(
    env: Env, settings: Settings
) -> None:
    await _store_gold(env, _gold(1300))
    per_trade, total = D(2), r.START - r.FLOOR
    async with env.db.connect() as conn:
        b = await page._bench(conn, env.clock.now(), PAXG, per_trade, total)
        alone = await page._replay(conn, env.clock.now(), PAXG, "turtle_55_20", per_trade, total)
    assert isinstance(b, autotrader.Bench)
    same = b.run(BY_KEY["turtle_55_20"])
    assert same.summary is not None and alone.summary is not None
    assert (same.summary.total, same.summary.trades) == (alone.summary.total, alone.summary.trades)
    # pinned on this seeded series: a later change to the replay shows here
    assert (same.summary.trades, round(same.summary.total, 2)) == (PINNED_TRADES, PINNED_TOTAL)
    rules = mandates.RULES_BY_TIMEFRAME[mandates.DAILY]
    view = edge.build(b, rules, env.clock.now(), {"turtle_55_20": same})
    assert edge.build(b, rules, env.clock.now()) is view  # kept for the day
    assert {k: v.settings for k, v in view.nearby.items()} == {
        "turtle_20_10": 9,
        "turtle_55_20": 9,
        "ema_200_trend": 5,
    }
    own = view.nearby["turtle_55_20"]
    assert own.low <= same.summary.total <= own.high
    sw = view.switching
    assert sw is not None and [y.year for y in sw.years] == [2026, 2027]
    jan = datetime(2026, 1, 1, tzinfo=UTC)
    for k in rules:  # as each rule stood on 1 January, an open trade valued at that day's close
        then = b.run(BY_KEY[k], total_stop=None, until=jan).summary
        assert sw.years[0].known[k] == (then.total if then else 0.0)
    assert view.since == mandates.RULES_REGISTERED[mandates.DAILY]
    assert [f.key for f in view.forward] == list(rules)
    for f in view.forward:  # the forward record starts at the registration, never before
        replayed = b.run(BY_KEY[f.key], since=view.since)
        assert all(t.opened >= view.since for t in replayed.trades)
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/rehearsal")).text
    assert "How far to trust it" in html and "settings made money" in html
    assert "Trend ensemble (six trend rules)</strong>: no single number to vary" in html
    assert "Switching on each 1 January to the rule that had made the most so far" in html
    assert "Restarted every 1 January" in html
    assert "Since the rules were chosen (24 Oct 2026)" in html


async def test_without_prices_there_is_nothing_to_trust_yet(env: Env, settings: Settings) -> None:
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/rehearsal")).text
    assert "No daily prices are stored yet" in html and "How far to trust it" not in html
