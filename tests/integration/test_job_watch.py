"""The worker records every run of its scheduled jobs; ops.job_watch alerts on repeated failures
and missed schedules, and the incidents close by themselves (directive Phase 8)."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, worker_heartbeats
from cosmic.web.app import create_app
from cosmic.worker import queue
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from cosmic.worker.main import Worker
from cosmic.worker.registry import Periodic, Registry
from tests.helpers import client_for, full_login, make_user


async def _open(db: Database) -> dict[str, str]:
    async with db.connect() as conn:
        rows = await conn.execute(
            select(incidents.c.dedup_key, incidents.c.severity).where(
                incidents.c.status != "RESOLVED"
            )
        )
        return {r.dedup_key: r.severity for r in rows if r.dedup_key.startswith("ops.job_")}


async def test_repeated_failures_and_missed_schedules_raise_incidents(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="t")
    full = build_registry(ctx)
    registry = Registry()
    registry.handlers["ops.job_watch"] = full.handlers["ops.job_watch"]
    outcome = {"fail": True}

    @registry.handler("test.flaky")
    async def flaky(_ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        if outcome["fail"]:
            raise RuntimeError("the source answered 503")

    registry.every(Periodic("test.flaky", 600, queue.PRIORITY_DEFAULT))
    worker = Worker(ctx, registry)

    runs = iter(range(1000))

    async def run(kind: str, n: int = 1) -> None:
        for _ in range(n):  # one attempt each: a failure is final, nothing is retried later
            async with db.transaction() as conn:
                await queue.enqueue(
                    conn,
                    kind=kind,
                    now=clock.now(),
                    idempotency_key=f"{kind}:{next(runs)}",
                    max_attempts=1,
                )
            assert await worker.run_one()
            await asyncio.gather(*worker._tasks)

    await run("test.flaky", 3)
    await run("ops.job_watch")
    assert await _open(db) == {"ops.job_failing:test.flaky": "WARNING"}
    outcome["fail"] = False
    await run("test.flaky")
    await run("ops.job_watch")
    assert await _open(db) == {}
    await worker.heartbeat()
    async with db.connect() as conn:
        info = await conn.scalar(select(worker_heartbeats.c.info))
    assert info["jobs"]["test.flaky"]["state"] == "OK"
    assert info["jobs"]["test.flaky"]["last_duration_s"] is not None
    clock.advance(seconds=600 * 3 + 180)  # three intervals and the grace, with no success
    await run("ops.job_watch")
    assert await _open(db) == {"ops.job_overdue:test.flaky": "WARNING"}
    await worker.heartbeat()
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = (await c.get("/ops")).text
    assert "Scheduled jobs" in page and "test.flaky" in page and "OVERDUE" in page
