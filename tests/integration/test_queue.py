from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import jobs
from cosmic.market.jobs import MARKET_SOURCES
from cosmic.ops.resources import Pressure
from cosmic.worker import queue
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from cosmic.worker.main import Worker


async def test_enqueue_idempotent(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        a = await queue.enqueue(conn, kind="k", now=clock.now(), idempotency_key="same")
        b = await queue.enqueue(conn, kind="k", now=clock.now(), idempotency_key="same")
    assert a is not None and b is None


async def test_priority_order_and_run_at(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        await queue.enqueue(conn, kind="k", now=clock.now(), priority=100, payload={"n": "low"})
        await queue.enqueue(conn, kind="k", now=clock.now(), priority=10, payload={"n": "high"})
        await queue.enqueue(
            conn,
            kind="k",
            now=clock.now(),
            priority=1,
            payload={"n": "future"},
            run_at=clock.now() + timedelta(minutes=5),
        )
    async with db.transaction() as conn:
        j = await queue.claim(conn, worker_id="w", now=clock.now())
    assert j is not None and j.payload["n"] == "high"


async def test_concurrent_claims_never_duplicate(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        for i in range(20):
            await queue.enqueue(conn, kind="k", now=clock.now(), payload={"i": i})

    async def claimer(name: str) -> list[str]:
        got = []
        while True:
            async with db.transaction() as conn:
                j = await queue.claim(conn, worker_id=name, now=clock.now())
            if j is None:
                return got
            got.append(str(j.id))

    results = await asyncio.gather(*(claimer(f"w{n}") for n in range(4)))
    claimed = [x for r in results for x in r]
    assert len(claimed) == 20 and len(set(claimed)) == 20


async def test_fail_backoff_then_dead(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        await queue.enqueue(conn, kind="k", now=clock.now(), max_attempts=2)
    async with db.transaction() as conn:
        j = await queue.claim(conn, worker_id="w", now=clock.now())
        assert j is not None
        assert (
            await queue.fail(conn, job=j, now=clock.now(), error="boom gsk_" + "a" * 30) == "QUEUED"
        )
    clock.advance(minutes=20)
    async with db.transaction() as conn:
        j = await queue.claim(conn, worker_id="w", now=clock.now())
        assert j is not None and j.attempts == 2
        assert await queue.fail(conn, job=j, now=clock.now(), error="boom") == "DEAD"
    async with db.connect() as conn:
        row = (await conn.execute(select(jobs.c.status, jobs.c.last_error))).one()
    assert row.status == "DEAD"


async def test_error_text_is_scrubbed(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        await queue.enqueue(conn, kind="k", now=clock.now())
    async with db.transaction() as conn:
        j = await queue.claim(conn, worker_id="w", now=clock.now())
        assert j
        await queue.fail(conn, job=j, now=clock.now(), error="key=gsk_" + "z" * 30)
    async with db.connect() as conn:
        err = (await conn.execute(select(jobs.c.last_error))).scalar_one()
    assert "gsk_" not in err


async def test_expired_lease_is_recovered(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        await queue.enqueue(conn, kind="k", now=clock.now())
    async with db.transaction() as conn:
        assert await queue.claim(conn, worker_id="dead-worker", now=clock.now(), lease_seconds=60)
    clock.advance(minutes=2)
    async with db.transaction() as conn:
        assert await queue.recover_expired_leases(conn, now=clock.now()) == 1
        j = await queue.claim(conn, worker_id="w2", now=clock.now())
    assert j is not None and j.attempts == 2


async def test_background_shedding(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        await queue.enqueue(conn, kind="k", now=clock.now(), priority=queue.PRIORITY_BACKGROUND)
    async with db.transaction() as conn:
        assert (
            await queue.claim(
                conn, worker_id="w", now=clock.now(), max_priority=queue.PRIORITY_BACKGROUND
            )
            is None
        )


async def test_periodic_schedules_sharing_a_kind_all_run(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    """Regression: the idempotency key ignored the payload, so only the first source's
    quote ingestion was ever scheduled and the cross-venue check was always STALE."""
    enabled = settings.model_copy(update={"market_data_enabled": True})
    ctx = WorkerContext(settings=enabled, db=db, clock=clock, worker_id="t")
    worker = Worker(ctx, build_registry(ctx))
    await worker.schedule_periodic()
    await worker.schedule_periodic()  # idempotent within the same bucket
    async with db.connect() as conn:
        rows = list(
            await conn.execute(select(jobs.c.payload).where(jobs.c.kind == "market.ingest_quotes"))
        )
    assert sorted(r.payload["source"] for r in rows) == sorted(MARKET_SOURCES)


async def test_heartbeat_touches_the_healthcheck_file(
    settings: Settings, db: Database, clock: FrozenClock, tmp_path: Path
) -> None:
    beat = tmp_path / "worker.heartbeat"
    ctx = WorkerContext(
        settings=settings.model_copy(update={"heartbeat_file": beat}),
        db=db,
        clock=clock,
        worker_id="t",
    )
    await Worker(ctx, build_registry(ctx)).heartbeat()
    assert beat.exists()


async def test_background_work_is_shed_under_disk_pressure(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="t")
    registry = build_registry(ctx)
    registry.handlers.setdefault("test.bg", registry.handlers["ops.disk_check"])
    async with db.transaction() as conn:
        await queue.enqueue(
            conn, kind="test.bg", now=clock.now(), priority=queue.PRIORITY_BACKGROUND
        )
        await queue.enqueue(
            conn, kind="ops.audit_verify", now=clock.now(), priority=queue.PRIORITY_SAFETY
        )
    worker = Worker(ctx, registry)
    worker.pressure = Pressure.WARNING
    assert await worker.run_one() is True  # the safety job is claimed and started
    assert await worker.run_one() is False  # background work waits while disk space is low
    await asyncio.gather(*worker._tasks)
    async with db.connect() as conn:
        status = dict(list(await conn.execute(select(jobs.c.kind, jobs.c.status))))
    assert status == {"ops.audit_verify": "DONE", "test.bg": "QUEUED"}
    worker.pressure = Pressure.NORMAL
    assert await worker.run_one() is True  # pressure gone: background work resumes
    await asyncio.gather(*worker._tasks)


async def test_runnable_tasks_follow_priority_then_deadline(
    db: Database, clock: FrozenClock
) -> None:
    """AI-20: when quota frees up, the most valuable waiting research runs first."""
    from datetime import timedelta as td

    from cosmic.core.ids import uuid7
    from cosmic.db.schema_research import missions
    from cosmic.workforce import tasks as engine

    async with db.transaction() as conn:
        mid = uuid7()
        await conn.execute(
            missions.insert().values(
                id=mid,
                kind="entry_signal",
                status="RUNNING",
                idempotency_key="m:priority",
                ai_call_budget=2,
                expires_at=clock.now() + td(hours=6),
                created_at=clock.now(),
                updated_at=clock.now(),
            )
        )
        ids = {}
        for name, prio, hours in (("low", 60, 1), ("high_late", 20, 5), ("high_soon", 20, 2)):
            ids[name] = await engine.create_task(
                conn,
                mission_id=mid,
                role_key="market_researcher",
                kind="narrative",
                title=name,
                now=clock.now(),
                expires_at=clock.now() + td(hours=hours),
                idempotency_key=f"k:{name}",
                acceptance={},
                priority=prio,
            )
    async with db.connect() as conn:
        ready = await engine.runnable(conn, clock.now())
    order = [t.title for t in ready]
    assert order[:3] == ["high_soon", "high_late", "low"], order


async def test_stale_heartbeats_are_pruned(db: Database, clock: FrozenClock) -> None:
    from cosmic.db.schema import worker_heartbeats
    from cosmic.ops.heartbeat import beat, prune_stale

    async with db.transaction() as conn:
        await beat(
            conn,
            worker_id="old",
            process="worker",
            started_at=clock.now(),
            now=clock.now(),
            info={},
        )
    clock.advance(minutes=59)
    async with db.transaction() as conn:
        await beat(
            conn,
            worker_id="current",
            process="worker",
            started_at=clock.now(),
            now=clock.now(),
            info={},
        )
        assert await prune_stale(conn, clock.now()) == 0  # 59 minutes: still kept
    clock.advance(minutes=2)
    async with db.transaction() as conn:
        assert await prune_stale(conn, clock.now()) == 1
        left = [r.worker_id for r in await conn.execute(select(worker_heartbeats))]
    assert left == ["current"]
