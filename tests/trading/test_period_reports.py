"""Weekly and monthly reports (directive Phase 10): whole periods, each account apart, the period
before for comparison, and the definitions spelled out."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_trading import equity_snapshots
from cosmic.reporting.periodic import (
    MONTHLY,
    WEEKLY,
    due_periods,
    last_month,
    last_week,
    period_report,
)
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from tests.trading_env import CAPTURE, Env, build_env


def test_periods_are_whole_weeks_and_months_in_utc() -> None:
    monday = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    w = last_week(monday)
    assert (w.start, w.end) == (
        datetime(2026, 10, 5, tzinfo=UTC),
        datetime(2026, 10, 12, tzinfo=UTC),
    )
    assert w.label == "2026-W41 (Mon 05 Oct – Sun 11 Oct)"
    m = last_month(datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
    assert (m.start, m.end) == (
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert m.label == "October 2026"
    assert [p.kind for p in due_periods(monday, 5)] == [WEEKLY]
    assert due_periods(monday.replace(hour=4), 5) == []  # before the report hour
    assert due_periods(datetime(2026, 10, 13, 6, tzinfo=UTC), 5) == []  # a Tuesday
    first = datetime(2026, 6, 1, 6, tzinfo=UTC)  # a Monday and the 1st
    assert [p.kind for p in due_periods(first, 5)] == [WEEKLY, MONTHLY]


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def sample(env: Env, at: datetime, equity: str) -> None:
    async with env.db.transaction() as conn:
        await conn.execute(
            equity_snapshots.insert().values(
                id=uuid7(),
                account_id=env.account_id,
                taken_at=at,
                equity=D(equity),
                cash=D(equity),
                positions_value=D(0),
                price_label="DELAYED",
                is_simulated=True,
            )
        )


async def test_the_weekly_report_compares_each_account_with_the_week_before(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(2)
    env.clock.advance(seconds=5)
    await env.set_market("80990.00", "80990.50")  # the stop sells: one losing trade
    await env.cycle(2)
    await sample(env, CAPTURE - timedelta(days=7), "9900")  # the week before
    await sample(env, CAPTURE - timedelta(days=6, hours=23), "9950")
    await sample(env, CAPTURE - timedelta(hours=1), "10000")
    await sample(env, CAPTURE, "10050")  # the week's peak
    await sample(env, CAPTURE + timedelta(hours=1), "9987")
    week = last_week(datetime(2026, 10, 12, 6, 0, tzinfo=UTC))  # CAPTURE is Sat 10 Oct
    async with env.db.connect() as conn:
        text = await period_report(conn, week, env.clock.now())
    lines = text.splitlines()
    assert lines[0] == "COSMICTRADING weekly report · 2026-W41 (Mon 05 Oct – Sun 11 Oct), UTC"
    assert (
        "• paper-main [PAPER] USD: equity 10,000.00 → 9,987.00 (-0.13 %; the period before: "
        "+0.51 %) · max drawdown 0.63 %"
    ) in lines
    trades = next(x for x in lines if "trade(s) closed" in x)
    assert "1 trade(s) closed (0 won, 1 lost) · realised -" in trades
    assert re.search(r"fees \d+\.\d\d USD", trades)  # cents for dollars
    assert lines[-1].startswith("Definitions: equity = cash + holdings")
    assert "NO QUALIFIED STRATEGY" in text and len(text) < 4096


async def test_reports_are_sent_once_on_their_day(env: Env) -> None:
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="t")
    job = build_registry(ctx).handlers["notify.period_reports"]
    # CAPTURE is Saturday 2026-10-10 21:06 UTC: nothing is due.
    await job(ctx, {})
    assert await env.count(notifications, notifications.c.kind.in_([WEEKLY, MONTHLY])) == 0
    env.clock.advance(seconds=int(timedelta(days=1, hours=8, minutes=24).total_seconds()))
    await job(ctx, {})  # Monday 2026-10-12 05:30 UTC: the weekly report
    await job(ctx, {})  # again the same Monday: nothing new
    async with env.db.connect() as conn:
        rows = list(await conn.execute(select(notifications).where(notifications.c.kind == WEEKLY)))
    assert len(rows) == 1 and rows[0].payload["text"].startswith("COSMICTRADING weekly report")
