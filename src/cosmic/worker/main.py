"""Worker process: heartbeats, scheduler, job execution with priority shedding."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.clock import Clock, SystemClock
from cosmic.core.config import Settings
from cosmic.core.logs import correlation_id_var, get_logger, new_correlation_id
from cosmic.db.engine import Database
from cosmic.db.schema import worker_heartbeats
from cosmic.ops.resources import Pressure, disk_status
from cosmic.worker import queue
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from cosmic.worker.registry import Registry

log = get_logger(__name__)

HEARTBEAT_SECONDS = 15
SCHEDULER_TICK_SECONDS = 5
IDLE_SLEEP_SECONDS = 1.0
MAX_CONCURRENCY = 4


class Worker:
    def __init__(self, ctx: WorkerContext, registry: Registry) -> None:
        self.ctx = ctx
        self.registry = registry
        self.stopping = asyncio.Event()
        self.started_at: datetime = ctx.clock.now()
        self.pressure = Pressure.NORMAL
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._tasks: set[asyncio.Task[None]] = set()

    async def heartbeat(self) -> None:
        now = self.ctx.clock.now()
        info: dict[str, Any] = {
            "pressure": self.pressure.value,
            "pid": os.getpid(),
            "running_jobs": len(self._tasks),
        }
        stmt = pg_insert(worker_heartbeats).values(
            worker_id=self.ctx.worker_id,
            process="worker",
            started_at=self.started_at,
            last_beat_at=now,
            info=info,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[worker_heartbeats.c.worker_id],
            set_={"last_beat_at": now, "info": info, "started_at": self.started_at},
        )
        async with self.ctx.db.transaction() as conn:
            await conn.execute(stmt)

    def check_resources(self) -> None:
        try:
            status = disk_status(
                self.ctx.settings.data_path,
                self.ctx.settings.disk_warning_free_pct,
                self.ctx.settings.disk_critical_free_pct,
            )
            self.pressure = status.pressure
        except OSError:
            self.pressure = Pressure.WARNING

    async def schedule_periodic(self) -> None:
        now = self.ctx.clock.now()
        epoch = int(now.timestamp())
        async with self.ctx.db.transaction() as conn:
            for p in self.registry.periodic:
                if not p.enabled(self.ctx):
                    continue
                bucket = epoch // p.interval_seconds
                await queue.enqueue(
                    conn,
                    kind=p.kind,
                    now=now,
                    payload=p.payload,
                    priority=p.priority,
                    idempotency_key=f"periodic:{p.kind}:{bucket}",
                    max_attempts=2,
                )
            await queue.recover_expired_leases(conn, now=now)

    async def run_one(self) -> bool:
        """Claim and start one job. Returns False when nothing is claimable."""
        now = self.ctx.clock.now()
        max_priority = queue.PRIORITY_BACKGROUND if self.pressure is not Pressure.NORMAL else None
        async with self.ctx.db.transaction() as conn:
            job = await queue.claim(
                conn,
                worker_id=self.ctx.worker_id,
                now=now,
                max_priority=max_priority,
                kinds=list(self.registry.handlers),
            )
        if job is None:
            return False
        await self._sem.acquire()
        task = asyncio.create_task(self._execute(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _execute(self, job: queue.Job) -> None:
        token = correlation_id_var.set(new_correlation_id())
        try:
            handler = self.registry.handlers[job.kind]
            try:
                await handler(self.ctx, job.payload)
            except Exception as exc:
                log.warning(
                    "job_failed",
                    kind=job.kind,
                    job_id=str(job.id),
                    attempt=job.attempts,
                    error=type(exc).__name__,
                    detail=str(exc)[:300],
                )
                async with self.ctx.db.transaction() as conn:
                    await queue.fail(
                        conn,
                        job=job,
                        now=self.ctx.clock.now(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
            else:
                async with self.ctx.db.transaction() as conn:
                    await queue.complete(conn, job_id=job.id, now=self.ctx.clock.now())
        finally:
            correlation_id_var.reset(token)
            self._sem.release()

    async def run(self) -> None:
        log.info("worker_started", worker_id=self.ctx.worker_id)
        for startup in self.ctx.services.get("startup_hooks", []):
            await startup(self.ctx)
        last_beat = last_tick = 0.0
        loop = asyncio.get_running_loop()
        while not self.stopping.is_set():
            mono = loop.time()
            try:
                if mono - last_beat >= HEARTBEAT_SECONDS:
                    self.check_resources()
                    await self.heartbeat()
                    last_beat = mono
                if mono - last_tick >= SCHEDULER_TICK_SECONDS:
                    await self.schedule_periodic()
                    last_tick = mono
                started = await self.run_one()
            except Exception as exc:  # database outage etc.: back off, keep the process alive
                log.error("worker_loop_error", error=type(exc).__name__, detail=str(exc)[:300])
                started = False
                await asyncio.sleep(5)
            if not started:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), timeout=IDLE_SLEEP_SECONDS)
        log.info("worker_stopping", running=len(self._tasks))
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=30)
        log.info("worker_stopped")


async def run_worker(settings: Settings, clock: Clock | None = None) -> int:
    db = Database.from_settings(settings)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    ctx = WorkerContext(settings=settings, db=db, clock=clock or SystemClock(), worker_id=worker_id)
    registry = build_registry(ctx)
    worker = Worker(ctx, registry)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stopping.set)
    try:
        await worker.run()
    finally:
        for shutdown in ctx.services.get("shutdown_hooks", []):
            await shutdown(ctx)
        await db.dispose()
    return 0
