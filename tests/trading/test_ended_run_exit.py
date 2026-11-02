"""After a run ends (S50): a trade the rule still holds keeps its stop, and when the rule says
sell, the owner is told once, with what to close and where; an ended run has no authority of its
own, so nothing is sold or bought by itself."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_execution import trade_intents
from cosmic.research.library import auto
from cosmic.trading import mandates, rehearsal
from tests.trading.test_auto_expect_board import Row, history
from tests.trading.test_auto_trader_daily import grant, last_day, quiet_then_breakout
from tests.trading.test_rehearsal import set_binance_fee
from tests.trading_env import BTC, Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    return await build_env(settings, db)


async def _bought_then_ended(env: Env, rule: str = "turtle_20_10") -> mandates.Mandate:
    """A daily breakout bought under a run that then ends at its expiry, unrenewed."""
    mid = await grant(env, rule=rule)
    await history(env, quiet_then_breakout(), last_open=last_day(env), timeframe="1d")
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    await env.cycle(2)
    async with env.db.connect() as conn:
        return await mandates.get_mandate(conn, mid)


async def _after_the_end(env: Env, m: mandates.Mandate, rows: list[Row]) -> None:
    """The clock moves past the run's end; the bars of the days since come in, and a price
    well above the trade's stop."""
    before = last_day(env)
    env.clock.set(m.expires_at + timedelta(hours=1))
    assert len(rows) == (last_day(env) - before).days
    await history(env, rows, last_open=last_day(env), timeframe="1d")
    await env.set_market("84000.00", "84010.00")


def _days(env: Env, m: mandates.Mandate) -> int:
    end = mandates.day_start(m.expires_at + timedelta(hours=1)) - timedelta(days=1)
    return (end - last_day(env)).days


def _trend(days: int) -> list[Row]:
    return [
        (83_200.0 + 20 * k, 83_250.0 + 20 * k, 83_150.0 + 20 * k, 83_200.0 + 20 * k)
        for k in range(days)
    ]


def _trend_then_exit(days: int) -> list[Row]:
    """Up all along, then a close under the prior 10-day low on the last day: the rule's exit."""
    return [*_trend(days - 1), (84_900.0, 84_900.0, 83_000.0, 83_100.0)]


def _exit_then_breakout(days: int) -> list[Row]:
    """The rule's exit, then the next day a close above the prior 20-day high: a new entry."""
    exit_day: Row = (84_900.0, 84_900.0, 83_000.0, 83_100.0)
    breakout: Row = (85_000.0, 86_000.0, 84_950.0, 85_900.0)
    return [*_trend(days - 2), exit_day, breakout]


async def _told(env: Env, m: mandates.Mandate) -> list[str]:
    async with env.db.connect() as conn:
        return [
            str(t)
            for (t,) in await conn.execute(
                select(notifications.c.payload["text"].astext).where(
                    notifications.c.dedup_key.like(f"mandate:{m.id}:ended_exit:%"),
                    notifications.c.channel == "PORTAL",
                )
            )
        ]


async def _intents(env: Env, side: str) -> list[Any]:
    async with env.db.connect() as conn:
        return list(await conn.execute(select(trade_intents).where(trade_intents.c.side == side)))


async def test_the_owner_is_told_once_when_the_rule_of_an_ended_run_says_sell(env: Env) -> None:
    m = await _bought_then_ended(env)
    await _after_the_end(env, m, _trend_then_exit(_days(env, m)))
    assert await auto.run(env.db, env.clock) == [f"{BTC}:EXIT_TOLD"]  # it expired the run first
    (told,) = await _told(env, m)
    assert "while its rule Turtle system 1 (20/10 breakout) still held" in told
    assert "the rule says sell. Nothing is sold by itself" in told
    assert "close it on Portfolio → paper-main with your code" in told
    assert await _intents(env, "SELL") == []  # nothing sold
    env.clock.advance(days=1)  # the next day the rule still says sell: not told again
    await env.set_market("84000.00", "84010.00")
    assert await auto.run(env.db, env.clock) == []
    assert len(await _told(env, m)) == 1


async def test_an_ended_run_never_buys_even_on_a_new_breakout(env: Env) -> None:
    m = await _bought_then_ended(env)
    await _after_the_end(env, m, _exit_then_breakout(_days(env, m)))
    assert await auto.run(env.db, env.clock) == []  # the rule says buy again: an ended run won't
    assert len(await _intents(env, "BUY")) == 1  # only the trade made while it ran
    assert await _told(env, m) == []


async def test_nothing_is_told_while_the_rule_still_holds(env: Env) -> None:
    m = await _bought_then_ended(env)
    await _after_the_end(env, m, _trend(_days(env, m)))
    assert await auto.run(env.db, env.clock) == []
    assert await _told(env, m) == []


async def test_a_later_run_of_the_same_rule_takes_over_another_rule_does_not(env: Env) -> None:
    m = await _bought_then_ended(env)
    env.clock.set(m.expires_at + timedelta(hours=1))
    async with env.db.transaction() as conn:
        await mandates.expire_due(conn, env.clock.now())
    await grant(env, rule="turtle_55_20")  # another rule: this run's trade stays its own
    async with env.db.connect() as conn:
        assert [r.id for r in await mandates.ended_runs(conn)] == [m.id]
    async with env.db.transaction() as conn:
        for r in await mandates.active_mandates(conn, env.clock.now()):
            await mandates.revoke(
                conn,
                principal=env.principal(),
                mandate_id=r.id,
                reason="trying another",
                now=env.clock.now(),
            )
    await grant(env)  # the same rule again: it takes the trade over itself
    async with env.db.connect() as conn:
        assert await mandates.ended_runs(conn) == []


async def test_runs_that_stopped_were_revoked_or_renew_themselves_are_left_alone(
    env: Env,
) -> None:
    mid = await grant(env)
    async with env.db.transaction() as conn:
        await mandates.end(
            conn,
            mandate_id=mid,
            status="STOPPED",
            by=Actor.system("mandate"),
            reason="its floor",
            now=env.clock.now(),
        )
        assert await mandates.ended_runs(conn) == []
    env.clock.advance(seconds=1)
    mid = await grant(env)
    async with env.db.transaction() as conn:
        await mandates.revoke(
            conn, principal=env.principal(), mandate_id=mid, reason="stopping", now=env.clock.now()
        )
        assert await mandates.ended_runs(conn) == []
    # a $100 rehearsal that renews itself: its renewal takes the trade over (S49)
    await set_binance_fee(env)
    async with env.db.transaction() as conn:
        await rehearsal.start(
            conn,
            principal=env.principal(),
            broker=env.svc.paper,
            rule="turtle_55_20",
            now=env.clock.now(),
            settings=env.settings,
            auto_renew=True,
        )
        run = (await mandates.active_mandates(conn, env.clock.now()))[-1]
        await mandates.end(
            conn,
            mandate_id=run.id,
            status="EXPIRED",
            by=Actor.system("mandate"),
            reason="expired",
            now=env.clock.now(),
        )
        assert all(r.id != run.id for r in await mandates.ended_runs(conn))
        await mandates.stop_renewing(
            conn,
            principal=env.principal(),
            mandate_id=run.id,
            reason="I will decide",
            now=env.clock.now(),
        )
        assert run.id in [r.id for r in await mandates.ended_runs(conn)]  # now it is watched
