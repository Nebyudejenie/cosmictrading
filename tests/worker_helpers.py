"""Run queued jobs synchronously in tests (exactly the worker's handlers)."""

from __future__ import annotations

from cosmic.worker import queue
from cosmic.worker.context import WorkerContext
from cosmic.worker.registry import Registry


async def run_queued(ctx: WorkerContext, registry: Registry, *, max_jobs: int = 50) -> list[str]:
    ran: list[str] = []
    for _ in range(max_jobs):
        async with ctx.db.transaction() as conn:
            job = await queue.claim(
                conn, worker_id="test", now=ctx.clock.now(), kinds=list(registry.handlers)
            )
        if job is None:
            return ran
        await registry.handlers[job.kind](ctx, job.payload)
        async with ctx.db.transaction() as conn:
            await queue.complete(conn, job_id=job.id, now=ctx.clock.now())
        ran.append(job.kind)
    return ran
