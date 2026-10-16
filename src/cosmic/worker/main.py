"""Worker process: heartbeats, scheduler, job execution with priority shedding."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.clock import Clock, SystemClock
from cosmic.core.config import Settings
from cosmic.core.jsonutil import canonical_hash_lenient
from cosmic.core.logs import correlation_id_var, get_logger, new_correlation_id
from cosmic.db.engine import Database
from cosmic.db.schema import worker_heartbeats
from cosmic.ops.backups import read_backups
from cosmic.ops.integrations import worker_report
from cosmic.ops.job_health import JobBook, last_successes, schedules_of
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


WATCHDOG_SECONDS = 120


class Watchdog:
    """Exits the process when the worker loop stops making progress (a hung await or a blocked
    event loop). Docker only *marks* an unhealthy container; exiting lets the restart policy
    start a fresh worker, whose startup recovery resolves any order in an uncertain
    transmission state without resubmitting it (AT-20)."""

    def __init__(
        self,
        limit_seconds: float = WATCHDOG_SECONDS,
        *,
        exit_fn: Callable[[int], object] = os._exit,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit_seconds
        self._exit = exit_fn
        self._clock = clock
        self._last = clock()
        self._stop = threading.Event()

    def progress(self) -> None:
        self._last = self._clock()

    def check(self) -> bool:
        stalled_for = self._clock() - self._last
        if stalled_for <= self.limit:
            return False
        log.critical("worker_watchdog_exit", stalled_seconds=round(stalled_for))
        self._exit(70)
        return True

    def start(self) -> None:
        threading.Thread(target=self._run, name="worker-watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(10):
            self.check()


class Worker:
    def __init__(
        self, ctx: WorkerContext, registry: Registry, watchdog: Watchdog | None = None
    ) -> None:
        self.ctx = ctx
        self.registry = registry
        self.stopping = asyncio.Event()
        self.watchdog = watchdog
        self.started_at: datetime = ctx.clock.now()
        self.pressure = Pressure.NORMAL
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._tasks: set[asyncio.Task[None]] = set()
        # Every run is recorded; the heartbeat publishes it and ops.job_watch alerts on it.
        self.jobs = JobBook(self.started_at)
        self.schedules = schedules_of(registry.periodic, ctx)
        ctx.services["job_book"] = self.jobs
        ctx.services["job_schedules"] = self.schedules

    async def heartbeat(self) -> None:
        now = self.ctx.clock.now()
        info: dict[str, Any] = {
            "pressure": self.pressure.value,
            "pid": os.getpid(),
            "running_jobs": len(self._tasks),
            "integrations": worker_report(self.ctx.settings),
            "jobs": self.jobs.as_info(self.schedules, now),
        }
        if self.ctx.settings.backup_check_dir is not None:
            report = await asyncio.to_thread(read_backups, self.ctx.settings.backup_check_dir, now)
            info["backups"] = report.as_info()
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
        if self.ctx.settings.heartbeat_file is not None:
            try:
                self.ctx.settings.heartbeat_file.touch()
            except OSError:
                log.warning("heartbeat_file_unwritable")

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
                # The payload is part of the key: several schedules may share a kind
                # (e.g. quote ingestion per source) and each must run.
                variant = canonical_hash_lenient(p.payload)[:16] if p.payload else "-"
                await queue.enqueue(
                    conn,
                    kind=p.kind,
                    now=now,
                    payload=p.payload,
                    priority=p.priority,
                    idempotency_key=f"periodic:{p.kind}:{variant}:{bucket}",
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
            started = self.ctx.clock.now()
            try:
                await handler(self.ctx, job.payload)
            except Exception as exc:
                self.jobs.failure(job.kind, self.ctx.clock.now(), f"{type(exc).__name__}: {exc}")
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
                self.jobs.success(job.kind, started, self.ctx.clock.now())
        finally:
            correlation_id_var.reset(token)
            self._sem.release()

    async def run(self) -> None:
        log.info("worker_started", worker_id=self.ctx.worker_id)
        for startup in self.ctx.services.get("startup_hooks", []):
            await startup(self.ctx)
        async with self.ctx.db.connect() as conn:  # successes from before this start
            self.jobs.seed(await last_successes(conn, list(self.schedules)))
        last_beat = last_tick = 0.0
        loop = asyncio.get_running_loop()
        while not self.stopping.is_set():
            if self.watchdog is not None:
                self.watchdog.progress()
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
    watchdog = Watchdog()
    worker = Worker(ctx, registry, watchdog)
    watchdog.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stopping.set)
    try:
        await worker.run()
    finally:
        watchdog.stop()
        for shutdown in ctx.services.get("shutdown_hooks", []):
            await shutdown(ctx)
        await db.dispose()
    return 0
