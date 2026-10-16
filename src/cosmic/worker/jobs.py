"""Assembles every job handler and periodic schedule from the domain packages."""

from __future__ import annotations

import asyncio
from typing import Any

from cosmic.audit.service import verify_chain
from cosmic.notify.alerts import Alert, Severity
from cosmic.notify.outbox import notify_owners
from cosmic.ops.backup_check import KEYS as BACKUP_KEYS
from cosmic.ops.backup_check import backup_alerts
from cosmic.ops.backups import read_backups
from cosmic.ops.heartbeat import prune_stale
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.ops.job_health import JobBook
from cosmic.ops.resources import Pressure, disk_status
from cosmic.reporting import daily as report
from cosmic.reporting import periodic
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_DEFAULT, PRIORITY_SAFETY, prune_finished
from cosmic.worker.registry import Periodic, Registry


def build_registry(ctx: WorkerContext) -> Registry:
    from cosmic.ai import jobs as ai_jobs
    from cosmic.market import jobs as market_jobs
    from cosmic.research.library import jobs as library_jobs
    from cosmic.trading import jobs as trading_jobs
    from cosmic.workforce import jobs as workforce_jobs

    registry = Registry()
    _register_ops(registry)
    _register_reports(registry)
    market_jobs.register(registry, ctx)
    trading_jobs.register(registry, ctx)
    ai_jobs.register(registry, ctx)
    workforce_jobs.register(registry, ctx)
    library_jobs.register(registry, ctx)
    return registry


def _register_reports(registry: Registry) -> None:
    @registry.handler("notify.daily_report")
    async def daily_report(ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        now = ctx.clock.now()
        if now.hour < ctx.settings.daily_report_hour_utc:
            return
        async with ctx.db.connect() as conn:
            if await report.sent_today(conn, now):
                return
            text = await report.daily_report(conn, now)
        async with ctx.db.transaction() as conn:
            await notify_owners(
                conn,
                kind=report.KIND,
                payload=report.report_payload(text),
                now=now,
                dedup_key=f"{report.KIND}:{now.date().isoformat()}",
            )

    @registry.handler("notify.period_reports")
    async def period_reports(ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        now = ctx.clock.now()
        for p in periodic.due_periods(now, ctx.settings.daily_report_hour_utc):
            async with ctx.db.connect() as conn:
                if await periodic.already_sent(conn, p):
                    continue
                text = await periodic.period_report(conn, p, now)
            async with ctx.db.transaction() as conn:
                await notify_owners(
                    conn,
                    kind=p.kind,
                    payload=report.report_payload(text),
                    now=now,
                    dedup_key=p.key,
                )

    registry.every(Periodic("notify.daily_report", 900, PRIORITY_DEFAULT))
    registry.every(Periodic("notify.period_reports", 900, PRIORITY_DEFAULT))


def _register_ops(registry: Registry) -> None:
    @registry.handler("ops.audit_verify")
    async def audit_verify(ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        async with ctx.db.connect() as conn:
            result = await verify_chain(conn)
        now = ctx.clock.now()
        async with ctx.db.transaction() as conn:
            if result.ok:
                await resolve_by_key(
                    conn,
                    dedup_key="audit.chain_broken",
                    now=now,
                    resolution=f"chain verified ({result.checked} events)",
                )
            else:
                await raise_incident(
                    conn,
                    dedup_key="audit.chain_broken",
                    category=Category.SECURITY,
                    alert=Alert(
                        title="Audit hash chain verification failed",
                        severity="CRITICAL",
                        what_happened=f"The hourly audit check failed at event "
                        f"{result.first_bad_seq}: {result.reason}.",
                        verified="The stored audit history no longer matches its hash chain.",
                        uncertain="Whether rows were altered, deleted or written out of order, "
                        "and by whom.",
                        risk_now="The record of decisions and orders cannot be trusted until this "
                        "is explained. Trading itself is not stopped automatically.",
                        action_taken="Incident opened; nothing was modified or repaired.",
                        owner_action="Treat as a security incident: take a backup now, do not edit "
                        "rows, consider the emergency stop, follow docs/15 'Audit chain broken'.",
                    ),
                    now=now,
                    detail={"first_bad_seq": result.first_bad_seq, "reason": result.reason},
                )

    @registry.handler("ops.disk_check")
    async def disk_check(ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        s = ctx.settings
        status = disk_status(s.data_path, s.disk_warning_free_pct, s.disk_critical_free_pct)
        now = ctx.clock.now()
        async with ctx.db.transaction() as conn:
            if status.pressure is Pressure.NORMAL:
                await resolve_by_key(
                    conn,
                    dedup_key="ops.disk_pressure",
                    now=now,
                    resolution=f"free {status.free_pct}%",
                )
            else:
                await raise_incident(
                    conn,
                    dedup_key="ops.disk_pressure",
                    category=Category.OPS,
                    alert=Alert(
                        title=f"Disk space low ({status.free_pct}% free)",
                        severity="CRITICAL" if status.pressure is Pressure.CRITICAL else "WARNING",
                        what_happened=f"Free space on {status.path} fell to {status.free_pct}%.",
                        verified="Measured by the worker just now.",
                        uncertain="How fast space is being used.",
                        risk_now="Below 10% the database and backups can fail; a full disk can "
                        "stop order recording.",
                        action_taken="Background research and backtests are paused; safety, "
                        "trading and data jobs continue.",
                        owner_action="Prune old backups per retention, check log sizes, or "
                        "extend the disk (docs/15 'Disk critical').",
                    ),
                    now=now,
                    detail={"path": status.path, "free_pct": str(status.free_pct)},
                )

    @registry.handler("ops.backup_check")
    async def backup_check(ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        directory = ctx.settings.backup_check_dir
        if directory is None:
            return
        now = ctx.clock.now()
        report = await asyncio.to_thread(read_backups, directory, now)
        found = backup_alerts(report, now)
        async with ctx.db.transaction() as conn:
            for key in BACKUP_KEYS:
                alert = found.get(key)
                if alert is None:
                    await resolve_by_key(conn, dedup_key=key, now=now, resolution="checked: ok")
                else:
                    await raise_incident(
                        conn,
                        dedup_key=key,
                        category=Category.OPS,
                        alert=alert,
                        now=now,
                        detail=report.as_info(),
                    )

    @registry.handler("ops.job_watch")
    async def job_watch(ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        book: JobBook | None = ctx.services.get("job_book")
        schedules: dict[str, int] | None = ctx.services.get("job_schedules")
        if book is None or schedules is None:
            return  # not inside a running worker
        now = ctx.clock.now()
        late, failing = book.overdue(schedules, now), book.failing()
        async with ctx.db.transaction() as conn:
            for kind, every in sorted(schedules.items()):
                severity: Severity = "CRITICAL" if kind.startswith("trading.") else "WARNING"
                key = f"ops.job_overdue:{kind}"
                if kind in late:
                    last = book.kinds[kind].last_success_at if kind in book.kinds else None
                    await raise_incident(
                        conn,
                        dedup_key=key,
                        category=Category.OPS,
                        alert=Alert(
                            title=f"Scheduled job {kind} has not completed for "
                            f"{int(late[kind].total_seconds() // 60)} min",
                            severity=severity,
                            what_happened=f"{kind} is scheduled every {every} s; its last "
                            f"success: {last or 'none since the worker started'}.",
                            verified="Recorded by the worker that runs it.",
                            uncertain="Whether it is stuck, failing before it starts, or shed "
                            "while disk space is low.",
                            risk_now="What this job keeps current (prices, orders, "
                            "reconciliation, reports or backups) may be out of date.",
                            action_taken="Incident opened; the job stays scheduled.",
                            owner_action="Check Operations → Scheduled jobs and the worker log; "
                            "restart the worker if the job stays stuck.",
                        ),
                        now=now,
                        detail={"kind": kind, "every_s": every, "last_success_at": last},
                    )
                else:
                    await resolve_by_key(conn, dedup_key=key, now=now, resolution="completed")
                key = f"ops.job_failing:{kind}"
                if kind in failing:
                    s = failing[kind]
                    await raise_incident(
                        conn,
                        dedup_key=key,
                        category=Category.OPS,
                        alert=Alert(
                            title=f"Job {kind} failed {s.failures_in_a_row} times in a row",
                            severity=severity,
                            what_happened=f"Last error: {s.last_error}.",
                            verified="Recorded by the worker that runs it.",
                            uncertain="Whether the cause is our code, the network or a source.",
                            risk_now="What this job keeps current may be out of date.",
                            action_taken="Retried with backoff; the job stays scheduled.",
                            owner_action="Check the worker log for this job; it resolves after "
                            "the next success.",
                        ),
                        now=now,
                        detail={"kind": kind, "failures_in_a_row": s.failures_in_a_row},
                    )
                else:
                    await resolve_by_key(conn, dedup_key=key, now=now, resolution="succeeded")

    @registry.handler("ops.prune_heartbeats")
    async def prune_heartbeats(ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        async with ctx.db.transaction() as conn:
            await prune_stale(conn, ctx.clock.now())

    registry.every(Periodic("ops.audit_verify", 3600, PRIORITY_SAFETY))

    @registry.handler("ops.prune_jobs")
    async def prune_jobs(ctx: WorkerContext, _payload: dict[str, Any]) -> None:
        async with ctx.db.transaction() as conn:
            await prune_finished(conn, now=ctx.clock.now())

    registry.every(Periodic("ops.prune_heartbeats", 900, PRIORITY_DEFAULT))
    registry.every(Periodic("ops.prune_jobs", 3600, PRIORITY_DEFAULT))
    registry.every(Periodic("ops.job_watch", 300, PRIORITY_SAFETY))
    registry.every(Periodic("ops.disk_check", 300, PRIORITY_SAFETY))
    registry.every(Periodic("ops.backup_check", 1800, PRIORITY_SAFETY))
