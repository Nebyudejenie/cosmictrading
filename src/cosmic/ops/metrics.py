"""Prometheus text metrics (OBS-02). Served only on /internal/metrics with a bearer token."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema import incidents, jobs, worker_heartbeats


def _line(name: str, value: float | int, labels: dict[str, str] | None = None) -> str:
    if labels:
        inner = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in sorted(labels.items()))
        return f"cosmic_{name}{{{inner}}} {value}"
    return f"cosmic_{name} {value}"


async def collect(conn: AsyncConnection, now: datetime) -> str:
    from cosmic.db.schema_ai import ai_usage
    from cosmic.db.schema_execution import orders
    from cosmic.db.schema_market import sources
    from cosmic.db.schema_trading import accounts
    from cosmic.trading.modes import environment_of

    out: list[str] = []
    for r in await conn.execute(
        select(
            jobs.c.kind,
            jobs.c.status,
            func.count().label("n"),
            func.min(jobs.c.run_at).label("oldest"),
        )
        .where(jobs.c.status.in_(["QUEUED", "RUNNING", "DEAD"]))
        .group_by(jobs.c.kind, jobs.c.status)
    ):
        out.append(_line("jobs", r.n, {"kind": r.kind, "status": r.status}))
        if r.status == "QUEUED" and r.oldest is not None:
            out.append(
                _line(
                    "job_oldest_age_seconds",
                    int((now - r.oldest).total_seconds()),
                    {"kind": r.kind},
                )
            )
    for r in await conn.execute(select(worker_heartbeats)):
        out.append(
            _line(
                "worker_heartbeat_age_seconds",
                int((now - r.last_beat_at).total_seconds()),
                {"worker": r.worker_id},
            )
        )
    for r in await conn.execute(select(sources.c.key, sources.c.last_success_at)):
        age = -1 if r.last_success_at is None else int((now - r.last_success_at).total_seconds())
        out.append(_line("source_last_success_age_seconds", age, {"source": r.key}))
    for r in await conn.execute(
        select(
            ai_usage.c.provider_key,
            ai_usage.c.status,
            func.count().label("n"),
            func.coalesce(func.sum(ai_usage.c.reported_cost), 0).label("cost"),
        ).group_by(ai_usage.c.provider_key, ai_usage.c.status)
    ):
        out.append(_line("ai_calls_total", r.n, {"provider": r.provider_key, "status": r.status}))
        out.append(
            _line(
                "ai_reported_cost_total",
                float(r.cost),
                {"provider": r.provider_key, "status": r.status},
            )
        )
    for r in await conn.execute(
        select(orders.c.status, func.count().label("n")).group_by(orders.c.status)
    ):
        out.append(_line("orders", r.n, {"status": r.status}))
    for r in await conn.execute(
        select(
            accounts.c.alias,
            accounts.c.kind,
            accounts.c.broker_adapter,
            accounts.c.reconciliation_status,
            accounts.c.last_reconciled_at,
        )
    ):
        age = (
            -1
            if r.last_reconciled_at is None
            else int((now - r.last_reconciled_at).total_seconds())
        )
        out.append(
            _line(
                "reconciliation_age_seconds",
                age,
                {
                    "account": r.alias,
                    # a dashboard or alert rule must tell simulated money from real (DIR-02)
                    "environment": environment_of(r.kind, r.broker_adapter).code,
                    "status": r.reconciliation_status,
                },
            )
        )
    for r in await conn.execute(
        select(incidents.c.severity, func.count().label("n"))
        .where(incidents.c.status != "RESOLVED")
        .group_by(incidents.c.severity)
    ):
        out.append(_line("open_incidents", r.n, {"severity": r.severity}))
    size = await conn.scalar(text("SELECT pg_database_size(current_database())"))
    out.append(_line("database_size_bytes", int(size or 0)))
    return "\n".join(out) + "\n"


def describe() -> dict[str, Any]:
    return {
        "format": "prometheus text 0.0.4",
        "auth": "Bearer COSMIC_METRICS_TOKEN",
        "public": False,
    }
