"""The owner's daily report and the Telegram health summary (directive Phases 8 and 10)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.notify.telegram.runner import notification_text
from cosmic.reporting.daily import KIND, daily_report, health_lines
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from tests.trading_env import Env, build_env


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def test_the_report_names_each_account_and_never_adds_them_up(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(2)  # filled, protected by its stop
    async with env.db.connect() as conn:
        text = await daily_report(conn, env.clock.now())
    assert "Mode PAPER_TRADING · live orders DISABLED" in text
    assert "• paper-main [SIMULATED · PAPER] USD: equity" in text and "reconciliation OK" in text
    assert "SPOT:BTC/USD@COINBASE 0.004 · stop ACTIVE" in text  # the open position and its stop
    assert "limits: daily loss" in text and "drawdown" in text
    assert "Closed in the last 24 h: none" in text
    assert "NO QUALIFIED STRATEGY" in text
    assert "Worker: NO HEARTBEAT" in text  # no worker in this test: said, not hidden
    assert len(text) < 4096
    env.clock.advance(seconds=5)
    await env.set_market("80990.00", "80990.50")  # through the stop
    await env.cycle(2)
    async with env.db.connect() as conn:
        text = await daily_report(conn, env.clock.now())
    assert (
        f"   {p.trade_code} on paper-main [SIMULATED · PAPER]: -" in text
    )  # realised loss after fees
    assert text.count(" USD") >= 2 and "USDT" not in text  # one currency per account


async def test_the_report_is_sent_once_a_day_after_its_hour(env: Env) -> None:
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="t")
    job = build_registry(ctx).handlers["notify.daily_report"]
    hour = env.settings.daily_report_hour_utc
    now = env.clock.now()
    env.clock.advance(
        seconds=int(
            (now.replace(hour=hour, minute=0, second=0) + timedelta(days=1) - now).total_seconds()
        )
        - 60
    )  # one minute before the hour
    await job(ctx, {})
    assert await env.count(notifications, notifications.c.kind == KIND) == 0
    env.clock.advance(seconds=120)
    await job(ctx, {})
    await job(ctx, {})  # the same day again: nothing new
    async with env.db.connect() as conn:
        rows = list(await conn.execute(select(notifications).where(notifications.c.kind == KIND)))
    assert {r.channel for r in rows} == {"PORTAL"}  # the test owner has no Telegram link
    assert len(rows) == 1 and rows[0].payload["text"].startswith("COSMICTRADING daily report")
    assert notification_text(KIND, rows[0].payload) == rows[0].payload["text"]
    env.clock.advance(seconds=86400)
    await job(ctx, {})
    assert await env.count(notifications, notifications.c.kind == KIND) == 2


async def test_health_lines_are_the_status_answer(env: Env) -> None:
    async with env.db.connect() as conn:
        lines = await health_lines(conn, env.clock.now())
    assert lines[0] == "Mode PAPER_TRADING · live orders DISABLED"
    assert lines[-1] == "Proposals waiting for you: 0"
    assert any(line.startswith("Backups: ") for line in lines)


async def test_the_report_carries_the_morning_briefing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-trader, the rule competition and gold in a few lines; a part that fails is
    left out with a note, and the report still goes out."""
    async with env.db.connect() as conn:
        text = await daily_report(conn, env.clock.now())
    assert "Auto-trader and research:" in text
    assert "- No paper or demo account can run the auto-trader yet." in text  # none here
    assert "- Rule competition: the first run comes within a few hours." in text
    assert "- GOLD no fresh price" in text
    assert len(text) < 4096
    from cosmic.reporting import gold

    async def broken(conn: object, now: object) -> object:
        raise RuntimeError("no gold today")

    monkeypatch.setattr(gold, "build", broken)
    async with env.db.connect() as conn:
        text = await daily_report(conn, env.clock.now())
    assert "(briefing not available this time)" in text and "NO QUALIFIED STRATEGY" in text
