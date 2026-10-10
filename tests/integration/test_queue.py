from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.db.engine import Database
from cosmic.db.schema import jobs
from cosmic.worker import queue


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
