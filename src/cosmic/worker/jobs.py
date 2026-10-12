"""Assembles every job handler and periodic schedule from the domain packages."""

from __future__ import annotations

from typing import Any

from cosmic.audit.service import verify_chain
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.ops.resources import Pressure, disk_status
from cosmic.worker.context import WorkerContext
from cosmic.worker.queue import PRIORITY_SAFETY
from cosmic.worker.registry import Periodic, Registry


def build_registry(ctx: WorkerContext) -> Registry:
    from cosmic.ai import jobs as ai_jobs
    from cosmic.market import jobs as market_jobs
    from cosmic.trading import jobs as trading_jobs
    from cosmic.workforce import jobs as workforce_jobs

    registry = Registry()
    _register_ops(registry)
    market_jobs.register(registry, ctx)
    trading_jobs.register(registry, ctx)
    ai_jobs.register(registry, ctx)
    workforce_jobs.register(registry, ctx)
    return registry


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

    registry.every(Periodic("ops.audit_verify", 3600, PRIORITY_SAFETY))
    registry.every(Periodic("ops.disk_check", 300, PRIORITY_SAFETY))
