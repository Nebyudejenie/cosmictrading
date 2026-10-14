"""Strategy library against the database: stored runs, owner switches, signals → proposals."""

from __future__ import annotations

import math
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, StepUpRequiredError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_market import bars, fetch_runs, instruments, sources
from cosmic.db.schema_research import (
    library_runs,
    opportunities,
    strategy_activations,
    strategy_evaluations,
    strategy_versions,
)
from cosmic.research.library import signals, store
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import COINBASE_LIKE
from cosmic.trading.approvals import expire_sweep
from tests.trading_env import Env, build_env

BTC_RESEARCH = "SPOT:BTC/USD@BITSTAMP"
GOLD_RESEARCH = "SPOT:PAXG/USDT@BINANCE"
BTC_TRADE = "SPOT:BTC/USD@COINBASE"
LAST_DAY = date(2026, 10, 9)  # last complete daily bar at the frozen clock (2026-10-10 21:06)


def walk(n: int, seed: int, start: float = 100.0) -> list[float]:
    rng = random.Random(seed)
    out, p = [], start
    for _ in range(n):
        p *= math.exp(rng.gauss(0.0005, 0.03))
        out.append(p)
    return out


def turning_long(n: int = 900) -> list[float]:
    """Closes whose last bar lifts the close above its 10-day average after 10 days below it."""
    closes = walk(n - 11, seed=4)
    p = closes[-1]
    for _ in range(10):
        p *= 0.985
        closes.append(p)
    closes.append(p * 1.12)
    return closes


async def seed_daily(db: Database, key: str, closes: list[float], last: date = LAST_DAY) -> None:
    now = datetime(2026, 10, 10, 21, 0, tzinfo=UTC)
    async with db.transaction() as conn:
        inst = (
            await conn.execute(select(instruments.c.id).where(instruments.c.canonical_key == key))
        ).scalar_one()
        source_key = "bitstamp_public" if key.endswith("BITSTAMP") else "binance_public"
        src = (
            await conn.execute(select(sources.c.id).where(sources.c.key == source_key))
        ).scalar_one()
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run,
                source_id=src,
                kind="test_daily",
                started_at=now,
                finished_at=now,
                status="SUCCESS",
            )
        )
        first = last - timedelta(days=len(closes) - 1)
        prev = closes[0]
        rows = []
        for i, c in enumerate(closes):
            d = first + timedelta(days=i)
            o = prev
            rows.append(
                {
                    "instrument_id": inst,
                    "source_id": src,
                    "timeframe": "1d",
                    "open_time": datetime(d.year, d.month, d.day, tzinfo=UTC),
                    "open": Decimal(f"{o:.6f}"),
                    "high": Decimal(f"{max(o, c) * 1.01:.6f}"),
                    "low": Decimal(f"{min(o, c) * 0.99:.6f}"),
                    "close": Decimal(f"{c:.6f}"),
                    "volume": Decimal("100"),
                    "is_complete": True,
                    "fetch_run_id": run,
                    "ingested_at": now,
                    "updated_at": now,
                }
            )
            prev = c
        await conn.execute(bars.insert(), rows)


async def fake_run(env: Env, tiers: dict[str, str]) -> uuid.UUID:
    """A coinbase-cost run with chosen tiers on the BTC series (controlled, for switch tests)."""
    now = env.clock.now()
    async with env.db.transaction() as conn:
        run_id = uuid7()
        await conn.execute(
            library_runs.insert().values(
                id=run_id,
                started_at=now,
                finished_at=now,
                code_version=store.CODE_VERSION,
                trials=len(tiers),
                costs={"name": COINBASE_LIKE.name},
                series=[],
                summary={},
                result_hash="test",
                created_at=now,
            )
        )
        for rank, (key, tier) in enumerate(tiers.items(), start=1):
            vid = await store.ensure_spec(conn, BY_KEY[key], now)
            await conn.execute(
                strategy_evaluations.insert().values(
                    id=uuid7(),
                    library_run_id=run_id,
                    strategy_version_id=vid,
                    series=BTC_RESEARCH,
                    instrument_id=None,
                    tier=tier,
                    rank=rank,
                    deflated_sharpe=Decimal("0.2"),
                    timing_sharpe=Decimal("0.8"),
                    result={
                        "strategy": key,
                        "series": BTC_RESEARCH,
                        "tier": tier,
                        "reasons": ["timing skill not significant"],
                        "costs": {"name": COINBASE_LIKE.name},
                    },
                    created_at=now,
                )
            )
    return run_id


async def switch(env: Env, key: str, on: bool, *, ack: bool = True, fresh: bool = True) -> None:
    async with env.db.transaction() as conn:
        await store.set_active(
            conn,
            principal=env.principal(mfa_fresh=fresh),
            strategy_key=key,
            instrument_key=BTC_TRADE,
            on=on,
            reason="paper forward test",
            acknowledged=ack,
            settings=env.settings,
            now=env.clock.now(),
        )


async def count(db: Database, table: object, *where: object) -> int:
    async with db.connect() as conn:
        return int(await conn.scalar(select(func.count()).select_from(table).where(*where)) or 0)  # type: ignore[arg-type]


async def test_library_run_stores_every_rule(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    empty = await store.run_library(db, env.clock.now(), (COINBASE_LIKE,))
    assert empty["status"] == "INSUFFICIENT_DATA"
    await seed_daily(db, BTC_RESEARCH, walk(900, seed=1))
    await seed_daily(db, GOLD_RESEARCH, walk(900, seed=2, start=2000.0))
    out = await store.run_library(db, env.clock.now(), (COINBASE_LIKE,))
    assert out["status"] == "COMPLETED"
    assert await count(db, library_runs) == 1
    assert await count(db, strategy_evaluations) == 95 * 2 + 5
    async with db.connect() as conn:
        run = await store.latest_run(conn, COINBASE_LIKE.name)
        rows = await store.run_evaluations(conn, run.id)
    assert run.trials == 195 and run.code_version == store.CODE_VERSION
    by_series: dict[str, list[int]] = {}
    for r in rows:
        assert r.tier in ("QUALIFIED", "WATCH", "REJECTED")
        by_series.setdefault(r.series, []).append(r.rank)
    assert {k: sorted(v) for k, v in by_series.items()} == {
        BTC_RESEARCH: list(range(1, 96)),
        GOLD_RESEARCH: list(range(1, 96)),
        store.PANEL_SERIES: list(range(1, 6)),
    }
    assert await count(db, audit_events, audit_events.c.action == "research.library_evaluated") == 1
    with pytest.raises(Exception, match=r"permission denied|immutable|not allowed"):
        async with db.transaction() as conn:
            await conn.execute(strategy_evaluations.update().values(tier="QUALIFIED"))


async def test_owner_switches_with_safeguards(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    keys = [s.key for s in list(BY_KEY.values()) if s.fn][:12]
    await fake_run(env, {keys[0]: "REJECTED", **dict.fromkeys(keys[1:], "WATCH")})
    with pytest.raises(ConflictError, match="REJECTED"):
        await switch(env, keys[0], True)
    with pytest.raises(ValidationFailedError, match="forward test"):
        await switch(env, keys[1], True, ack=False)
    with pytest.raises(StepUpRequiredError):
        await switch(env, keys[1], True, fresh=False)
    await switch(env, keys[1], True)
    with pytest.raises(ConflictError, match="already on"):
        await switch(env, keys[1], True)
    for k in keys[2:11]:
        await switch(env, k, True)
    with pytest.raises(ConflictError, match="at most 10"):
        await switch(env, keys[11], True)
    await switch(env, keys[1], False)
    await switch(env, keys[11], True)  # room again
    async with db.connect() as conn:
        on = {r.strategy_key for r in await store.active(conn)}
    assert len(on) == 10 and keys[1] not in on
    assert await count(db, strategy_activations) == 12
    assert (
        await count(db, audit_events, audit_events.c.action == "research.strategy_activated") == 11
    )


async def test_signal_becomes_a_proposal_and_exit_sells_only_the_rules_holding(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)
    rule = "detzel_sma_10"  # long while the close is above its 10-day average
    await seed_daily(db, BTC_RESEARCH, turning_long())
    await fake_run(env, {rule: "WATCH"})
    await switch(env, rule, True)
    await env.set_market("82990.00", "83000.00")
    done = await signals.run(db, env.clock)
    assert done == [f"{rule}:{BTC_TRADE}:ENTER:PROPOSED"], done
    assert await signals.run(db, env.clock) == []  # one proposal per rule and daily bar
    async with db.connect() as conn:
        intent = (await conn.execute(select(trade_intents))).one()
        opportunity = (
            await conn.execute(select(opportunities).where(opportunities.c.intent_id == intent.id))
        ).one()
        version = (
            await conn.execute(
                select(strategy_versions).where(
                    strategy_versions.c.id == opportunity.strategy_version_id
                )
            )
        ).one()
    assert intent.side == "BUY" and intent.status == "PROPOSED"
    assert "WATCH" in intent.plain_summary and "forward test" in intent.plain_summary
    assert "not part of the tested rule" in intent.thesis
    assert opportunity.status == "APPROVAL_REQUIRED" and rule in version.code_ref
    plan = intent.management_plan
    assert Decimal(str(plan["stop_loss"]["stop_price"])) < Decimal("83000")

    await env.approve(intent.id)
    await env.cycle()
    await env.cycle()
    async with db.connect() as conn:
        held = await signals.rule_holding(conn, BY_KEY[rule], env.account_id, intent.instrument_id)
    assert held > 0

    # Next day the close falls back below the 10-day average: the rule exits.
    await seed_daily(
        db, BTC_RESEARCH, [turning_long()[-1] * 0.8], last=LAST_DAY + timedelta(days=1)
    )
    env.clock.advance(hours=4)  # 2026-10-11 01:06 UTC: the 10-10 bar is complete
    await env.set_market("82000.00", "82010.00")
    done = await signals.run(db, env.clock)
    assert done == [f"{rule}:{BTC_TRADE}:EXIT:PROPOSED"], done
    async with db.connect() as conn:
        sell = (
            await conn.execute(select(trade_intents).where(trade_intents.c.side == "SELL"))
        ).one()
    assert sell.quantity == held and sell.effect == "CLOSE" and sell.time_in_force == "IOC"
    assert sell.details["closes"] == [str(intent.id)]  # replaces exactly this rule's entry
    # Still flat the next day while that exit is pending: no second proposal.
    await seed_daily(
        db, BTC_RESEARCH, [turning_long()[-1] * 0.79], last=LAST_DAY + timedelta(days=2)
    )
    env.clock.advance(days=1)
    await env.set_market("82000.00", "82010.00")
    assert await signals.run(db, env.clock) == []
    # The owner never approved it and it expired: the rule still holds, so it proposes again.
    async with db.transaction() as conn:
        await expire_sweep(conn, now=env.clock.now())
    await seed_daily(
        db, BTC_RESEARCH, [turning_long()[-1] * 0.78], last=LAST_DAY + timedelta(days=3)
    )
    env.clock.advance(days=1)
    await env.set_market("82000.00", "82010.00")
    assert await signals.run(db, env.clock) == [f"{rule}:{BTC_TRADE}:EXIT:PROPOSED"]
    async with db.connect() as conn:
        sell = (
            await conn.execute(
                select(trade_intents).where(
                    trade_intents.c.side == "SELL", trade_intents.c.status == "PROPOSED"
                )
            )
        ).one()
    await env.approve(sell.id)
    await env.cycle()  # the stop is cancelled, then the exit is sent
    await env.cycle()  # and filled
    async with db.connect() as conn:
        remaining = await signals.rule_holding(
            conn, BY_KEY[rule], env.account_id, intent.instrument_id
        )
        exit_status = await conn.scalar(
            select(trade_intents.c.status).where(trade_intents.c.id == sell.id)
        )
    assert remaining == 0 and exit_status == "EXECUTED"
