"""What to expect and the market radar (ADR-030 addendum 2), from the database: the rule
replayed on the stored hourly history with the owner's plan, costs from the venue's own
quotes, too little history said plainly, and the radar's levels before and during a trade."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.reporting import autotrader
from cosmic.research.library import auto
from cosmic.trading import mandates
from cosmic.trading.policy import active_policy
from tests.trading.test_auto_trader import breakout, hourly_bars
from tests.trading.test_auto_trader_money import grant_money, last_hour
from tests.trading_env import BTC, Env, build_env

D = Decimal
Row = tuple[float, float, float, float]  # open, high, low, close


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    return await build_env(settings, db)


def walk(hours: int, seed: int = 3, price: float = 82_000.0) -> list[Row]:
    """A seeded hourly random walk around bitcoin's price (no edge by construction)."""
    rng = random.Random(seed)
    rows: list[Row] = []
    for _ in range(hours):
        close = price * math.exp(rng.gauss(0, 0.004))
        high = max(price, close) * math.exp(abs(rng.gauss(0, 0.002)))
        low = min(price, close) * math.exp(-abs(rng.gauss(0, 0.002)))
        rows.append((price, high, low, close))
        price = close
    return rows


async def history(env: Env, rows: list[Row], *, last_open: datetime) -> None:
    """Completed hourly bars of BTC ending with the bar opened at ``last_open``."""
    async with env.db.transaction() as conn:
        inst = await conn.scalar(select(instruments.c.id).where(instruments.c.canonical_key == BTC))
        src = await conn.scalar(
            select(sources.c.id).where(sources.c.key == "coinbase_exchange_public")
        )
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
        first = last_open - timedelta(hours=len(rows) - 1)
        params: list[dict[str, Any]] = [
            {
                "i": inst,
                "s": src,
                "t": first + timedelta(hours=k),
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
                "VALUES (:i, :s, '1h', :t, :o, :h, :l, :c, 1, true, :r, :n, :n)"
            ),
            params,
        )


async def plan_and_expect(env: Env, money: str = "5000") -> autotrader.Expectation:
    async with env.db.connect() as conn:
        p = await autotrader.plan(conn, env.clock.now(), D(money), "NORMAL", "AUTO")
        e = await autotrader.plan_expectation(conn, env.clock.now(), p)
    assert e is not None
    return e


async def test_what_to_expect_replays_the_rule_with_the_plan(env: Env) -> None:
    await history(env, walk(40 * 24), last_open=last_hour(env))
    e = await plan_and_expect(env)
    s = e.summary
    assert s is not None and e.note == ""
    assert s.trades > 5 and s.wins + s.losses <= s.trades
    first = last_hour(env) - timedelta(hours=40 * 24 - 1)
    assert s.start == first + autotrader.WARM_UP  # less than a year stored: all of it is used
    assert (e.money, e.per_trade, e.markets) == (D(5000), D("50.00"), ["BTC/USD"])
    assert e.series == ["Coinbase BTC/USD"]
    (cost,) = e.costs
    async with env.db.connect() as conn:
        _, policy = await active_policy(conn)
    assert cost.fee_pct == policy.fee_bps_for("COINBASE") / 100  # the policy's assumption
    assert cost.spread_measured and cost.spread_pct > 0  # from the venue's own quotes
    assert e.verdict and e.total_pct is not None
    assert sum(m.trades for m in s.markets) == s.trades


async def test_telegram_trader_carries_the_past_test_of_the_settings(env: Env) -> None:
    await history(env, walk(40 * 24, seed=7), last_open=last_hour(env))
    await grant_money(env, "5000")
    async with env.db.connect() as conn:
        board = await autotrader.build(conn, env.clock.now())
        e = await autotrader.mandate_expectation(conn, env.clock.now(), board)
    assert e is not None and e.summary is not None
    assert e.period == f"the last {e.summary.days} days"  # less than a year stored: said so
    lines = autotrader.telegram_lines(board, env.clock.now(), e)
    (line,) = [x for x in lines if x.startswith("Past test of these settings")]
    assert f"over {e.period}" in line and "HISTORICAL SIMULATION, not a promise" in line
    assert f"{e.summary.trades} trades" in line


async def test_too_little_history_is_said_plainly(env: Env) -> None:
    await history(env, walk(10 * 24), last_open=last_hour(env))
    e = await plan_and_expect(env)
    first = last_hour(env) - timedelta(hours=10 * 24 - 1)
    days = (env.clock.now() - first).days
    assert e.summary is None and e.note.startswith(f"Only {days} days of hourly prices")


async def test_the_radar_shows_where_it_buys_then_where_it_sells_and_the_stop(env: Env) -> None:
    await grant_money(env, "5000")
    last = last_hour(env)
    closes = breakout()
    await hourly_bars(env, closes[:-1], last_open=last - timedelta(hours=1))
    async with env.db.connect() as conn:
        board = await autotrader.build(conn, env.clock.now())
    (w,) = board.watch
    assert w.chart is not None and not w.chart.in_trade
    assert len(w.chart.closes) == autotrader.RADAR_HOURS
    assert w.chart.next_buy == float(w.level) == max(c + 20 for c in closes[-21:-1])
    assert w.chart.series == "Coinbase BTC/USD"
    await hourly_bars(env, closes, last_open=last)
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    await env.cycle(2)
    async with env.db.connect() as conn:
        board = await autotrader.build(conn, env.clock.now())
    (w,) = board.watch
    (trade,) = board.open
    r = w.chart
    assert r is not None and r.in_trade and r.entry_index == len(r.closes) - 1
    assert r.next_sell == min(c - 20 for c in closes[-10:])
    assert r.stop_price == trade.stop and r.entry_price == trade.entry
    assert r.stop is not None and r.stop < r.closes[-1]  # drawn at the trade's distance below
