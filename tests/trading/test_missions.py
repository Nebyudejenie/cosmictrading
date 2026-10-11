"""WF-08: one complete research mission, end to end, on the real database (fake free AI)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from cosmic.ai.gateway import AIGateway
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_market import bars, claims, fetch_runs, instruments, market_events, sources
from cosmic.db.schema_research import missions, opportunities, task_reviews, tasks
from cosmic.market import data as md
from cosmic.research import opportunities as opp
from cosmic.research.events import detect_and_store
from cosmic.research.strategy import TrendSmaAtr
from cosmic.workforce.missions import MissionDeps, MissionRunner, mission_report
from tests.ai_fake import FakeAI
from tests.integration.test_ai_gateway import setup_routes
from tests.research_data import entry_signal_bars
from tests.trading_env import BTC, Env, build_env

NARRATIVE = {
    "thesis": "A fresh SMA crossover after a basing pattern suggests a short-term trend continuation.",
    "plain_summary": "The short-term trend turned up; a small, stop-protected long is reasonable to consider.",
    "counterarguments": ["Crossovers often fail in ranges", "Volume is not confirming"],
    "red_team": "This may be a bear-market rally; the regime was down for most of the sample.",
    "scenarios": [
        {"name": "base", "description": "Gradual drift higher over a few days."},
        {"name": "fail", "description": "Reversal below the stop within hours."},
    ],
    "invalidation": ["Hourly close below the stop level"],
    "claims": [
        {"statement": "RSI(14) is extremely overbought at 99", "metric": "rsi14", "value": 99.0},
        {"statement": "Momentum has improved over the last day"},
    ],
}


async def load_synthetic_bars(env: Env) -> None:
    """Insert deterministic synthetic hourly bars (test data only) for the Coinbase BTC instrument."""
    data = entry_signal_bars()
    async with env.db.transaction() as conn:
        inst_id = (
            await conn.execute(select(instruments.c.id).where(instruments.c.canonical_key == BTC))
        ).scalar_one()
        src_id = (
            await conn.execute(
                select(sources.c.id).where(sources.c.key == "coinbase_exchange_public")
            )
        ).scalar_one()
        run_id = uuid7()
        now = env.clock.now()
        await conn.execute(
            fetch_runs.insert().values(
                id=run_id,
                source_id=src_id,
                kind="synthetic_test",
                started_at=now,
                finished_at=now,
                status="SUCCESS",
            )
        )
        await conn.execute(
            bars.insert(),
            [
                {
                    "instrument_id": inst_id,
                    "source_id": src_id,
                    "timeframe": "1h",
                    "open_time": b.open_time,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                    "is_complete": True,
                    "fetch_run_id": run_id,
                    "ingested_at": now,
                    "updated_at": now,
                }
                for b in data
            ],
        )


async def run_until_done(runner: MissionRunner, env: Env, rounds: int = 10) -> None:
    for _ in range(rounds):
        if await runner.step() == 0:
            return
        env.clock.advance(seconds=1)


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    e = await build_env(settings, db)
    await load_synthetic_bars(e)
    return e


async def detect(env: Env) -> list[str]:
    async with env.db.transaction() as conn:
        inst = await md.instrument_by_key(conn, BTC)
        return await detect_and_store(
            conn, instrument=inst, strategy=TrendSmaAtr(), now=env.clock.now()
        )


async def test_complete_mission_from_signal_to_execution(env: Env, settings: Settings) -> None:
    s = settings.model_copy(update={"groq_api_key": SecretStr("gsk_" + "q" * 40)})
    await setup_routes(env.db, s, env.clock, username="aiadmin")
    fake = FakeAI()
    fake.default["groq"] = NARRATIVE
    gw = AIGateway(env.db, env.clock, s, transport=fake.transport(), retry_base_seconds=0)
    runner = MissionRunner(MissionDeps(env.db, env.clock, s, gw))
    found = await detect(env)
    assert "SIGNAL" in found
    assert await detect(env) == []  # idempotent per bar
    assert await runner.open_from_events() >= 1
    await run_until_done(runner, env)
    async with env.db.connect() as conn:
        m = (await conn.execute(select(missions).where(missions.c.kind == "entry_signal"))).one()
        ts = {
            t.kind: t for t in await conn.execute(select(tasks).where(tasks.c.mission_id == m.id))
        }
        reviews = list(
            await conn.execute(
                select(task_reviews).where(
                    task_reviews.c.task_id.in_(select(tasks.c.id).where(tasks.c.mission_id == m.id))
                )
            )
        )
        o = (
            await conn.execute(select(opportunities).where(opportunities.c.mission_id == m.id))
        ).one()
        intent = (
            await conn.execute(select(trade_intents).where(trade_intents.c.id == o.intent_id))
        ).one()
        cl = {
            c.statement: c.status
            for c in await conn.execute(select(claims).where(claims.c.mission_id == m.id))
        }
        report = await mission_report(conn, m.id)
        ev = (
            await conn.execute(select(market_events).where(market_events.c.kind == "SIGNAL"))
        ).one()
    assert m.status == "ARCHIVED" and m.ai_calls_used == 1
    assert {k: t.status for k, t in ts.items()} == dict.fromkeys(
        ("data_check", "quant", "narrative", "proposal", "report"), "DONE"
    )
    assert (
        all(r.verdict == "PASS" for r in reviews) and len(reviews) == 4
    )  # every reviewed task verified
    assert o.status == "APPROVAL_REQUIRED" and intent.status == "PROPOSED"
    assert intent.created_by_kind == "WORKER" and intent.created_by.endswith("risk_reviewer")
    assert intent.management_plan["stop_loss"] and intent.management_plan["take_profit_price"]
    assert (
        cl["RSI(14) is extremely overbought at 99"] == "CONTRADICTED"
    )  # AI number re-checked by code
    assert cl["Momentum has improved over the last day"] == "INFERRED"
    assert any(v == "VERIFIED" for v in cl.values())
    assert ev.status == "PROCESSED"
    assert report is not None
    for section in (
        "what_changed",
        "what_matters",
        "verified",
        "inferred",
        "contradicts",
        "unknown",
        "timestamps",
        "freshness",
        "why_it_matters",
    ):
        assert section in report
    assert report["what_matters"]["proposal"] == "PROPOSED"
    # The proposal is research, not authority: nothing executes until the operator approves.
    await env.cycle()
    assert intent.id and (await env.count(trade_intents, trade_intents.c.status == "PROPOSED")) == 1
    await env.approve(intent.id)
    await env.cycle(2)
    async with env.db.transaction() as conn:
        await opp.sync_from_intents(conn, env.clock.now())
    async with env.db.connect() as conn:
        o2 = (await conn.execute(select(opportunities).where(opportunities.c.id == o.id))).one()
    assert o2.status == "EXECUTION"
    await gw.close()


async def test_no_ai_means_no_ai_researched_proposal(env: Env) -> None:
    runner = MissionRunner(MissionDeps(env.db, env.clock, env.settings, None))
    await detect(env)
    await runner.open_from_events()
    await run_until_done(runner, env)
    async with env.db.connect() as conn:
        m = (await conn.execute(select(missions).where(missions.c.kind == "entry_signal"))).one()
    assert m.status == "WAITING_FOR_AI" and "free AI" in (m.status_reason or "")
    env.clock.advance(hours=3)
    await runner.expire()
    async with env.db.connect() as conn:
        m = (await conn.execute(select(missions).where(missions.c.kind == "entry_signal"))).one()
        o = (
            await conn.execute(select(opportunities).where(opportunities.c.mission_id == m.id))
        ).one()
    assert m.status == "EXPIRED" and o.status == "NO_TRADE" and o.intent_id is None
    assert await env.count(trade_intents) == 0


async def test_event_review_missions_use_no_ai(env: Env) -> None:
    async with env.db.transaction() as conn:
        inst = await md.instrument_by_key(conn, BTC)
        await conn.execute(
            market_events.insert().values(
                id=uuid7(),
                instrument_id=inst.id,
                kind="VOLUME_SPIKE",
                detected_at=env.clock.now(),
                bar_time=env.clock.now() - timedelta(hours=1),
                detail={"volume": 99},
                dedup_key="vs:test",
                status="NEW",
            )
        )
    runner = MissionRunner(MissionDeps(env.db, env.clock, env.settings, None))
    await runner.open_from_events()
    await run_until_done(runner, env)
    async with env.db.connect() as conn:
        m = (await conn.execute(select(missions).where(missions.c.kind == "event_review"))).one()
        kinds = {
            t.kind for t in await conn.execute(select(tasks).where(tasks.c.mission_id == m.id))
        }
    assert m.status == "ARCHIVED" and m.ai_calls_used == 0
    assert kinds == {"data_check", "quant", "report"}
