"""The command center answers §11's questions from stored state, with honest labels."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema import incidents, worker_heartbeats
from cosmic.db.schema_execution import orders, recovery_checklists, trade_intents
from cosmic.db.schema_market import (
    claims,
    contradictions,
    fetch_runs,
    instruments,
    market_events,
    sources,
)
from cosmic.db.schema_research import escalations, missions, tasks
from cosmic.db.schema_trading import accounts, positions
from cosmic.ops.health import current_revision
from cosmic.sources.freshness import classify
from cosmic.trading.queries import authority_banner


async def build(conn: AsyncConnection, now: datetime, *, ai_status: str) -> dict[str, Any]:
    day_ago = now - timedelta(hours=24)
    events = list(
        await conn.execute(
            select(
                market_events.c.kind,
                market_events.c.detected_at,
                market_events.c.detail,
                instruments.c.canonical_key,
            )
            .join(instruments, instruments.c.id == market_events.c.instrument_id, isouter=True)
            .where(market_events.c.detected_at >= day_ago)
            .order_by(desc(market_events.c.detected_at))
            .limit(10)
        )
    )
    failed_fetches = int(
        await conn.scalar(
            select(func.count())
            .select_from(fetch_runs)
            .where(fetch_runs.c.status == "FAILED", fetch_runs.c.started_at >= day_ago)
        )
        or 0
    )
    src_rows = list(await conn.execute(select(sources)))
    source_labels = [
        {
            "key": s.key,
            "label": classify(
                s.last_success_at,
                now,
                s.freshness_limit_seconds,
                latency_class=s.latency_class,
                enabled=s.enabled,
            ).value,
        }
        for s in src_rows
    ]
    pending = list(
        await conn.execute(
            select(
                trade_intents.c.id,
                trade_intents.c.trade_code,
                trade_intents.c.side,
                trade_intents.c.quantity,
                trade_intents.c.intent_expires_at,
                trade_intents.c.is_simulated,
                instruments.c.canonical_key,
            )
            .join(instruments, instruments.c.id == trade_intents.c.instrument_id)
            .where(trade_intents.c.status == "PROPOSED")
            .order_by(trade_intents.c.intent_expires_at)
        )
    )
    claim_counts = {
        r.status: int(r.n)
        for r in await conn.execute(
            select(claims.c.status, func.count().label("n"))
            .where(claims.c.created_at >= day_ago)
            .group_by(claims.c.status)
        )
    }
    open_contradictions = int(
        await conn.scalar(
            select(func.count())
            .select_from(contradictions)
            .where(contradictions.c.status == "OPEN")
        )
        or 0
    )
    incident_rows = list(
        await conn.execute(
            select(incidents)
            .where(incidents.c.status != "RESOLVED")
            .order_by(desc(incidents.c.opened_at))
            .limit(20)
        )
    )
    task_counts = {
        r.status: int(r.n)
        for r in await conn.execute(
            select(tasks.c.status, func.count().label("n")).group_by(tasks.c.status)
        )
    }
    overdue = int(
        await conn.scalar(
            select(func.count())
            .select_from(tasks)
            .where(
                tasks.c.status.in_(["QUEUED", "WAITING_FOR_AI", "RUNNING"]),
                tasks.c.expires_at <= now + timedelta(minutes=15),
            )
        )
        or 0
    )
    open_escalations = int(
        await conn.scalar(
            select(func.count()).select_from(escalations).where(escalations.c.resolved_at.is_(None))
        )
        or 0
    )
    mission_counts = {
        r.status: int(r.n)
        for r in await conn.execute(
            select(missions.c.status, func.count().label("n"))
            .where(missions.c.created_at >= day_ago)
            .group_by(missions.c.status)
        )
    }
    acct_rows = list(await conn.execute(select(accounts)))
    unknown_orders = int(
        await conn.scalar(
            select(func.count()).select_from(orders).where(orders.c.status == "UNKNOWN_ORDER_STATE")
        )
        or 0
    )
    degraded = int(
        await conn.scalar(
            select(func.count())
            .select_from(positions)
            .where(
                positions.c.quantity > 0, positions.c.protection_state.in_(["DEGRADED", "UNKNOWN"])
            )
        )
        or 0
    )
    checklist = (
        await conn.execute(
            select(recovery_checklists).where(recovery_checklists.c.status == "OPEN")
        )
    ).first()
    beats = list(await conn.execute(select(worker_heartbeats)))
    worker_age = min(((now - b.last_beat_at).total_seconds() for b in beats), default=None)
    attention: list[str] = []
    if pending:
        attention.append(f"{len(pending)} proposal(s) await your decision")
    crit = [i for i in incident_rows if i.severity == "CRITICAL" and i.status == "OPEN"]
    if crit:
        attention.append(f"{len(crit)} critical incident(s) unacknowledged")
    if checklist is not None:
        attention.append("emergency stop active: recovery checklist open")
    bad = [a.alias for a in acct_rows if a.reconciliation_status != "OK"]
    if bad:
        attention.append(f"reconciliation not OK for {', '.join(bad)} (new exposure blocked)")
    if unknown_orders:
        attention.append(f"{unknown_orders} order(s) in UNKNOWN state")
    if degraded:
        attention.append(f"{degraded} position(s) without verified protection")
    if open_escalations:
        attention.append(f"{open_escalations} escalated task(s)")
    if worker_age is None or worker_age > 60:
        attention.append(
            "no recent worker heartbeat: background work and execution are not running"
        )
    return {
        "authority": await authority_banner(conn),
        "events": events,
        "failed_fetches_24h": failed_fetches,
        "sources": source_labels,
        "pending": pending,
        "claim_counts": claim_counts,
        "open_contradictions": open_contradictions,
        "incidents": incident_rows,
        "task_counts": task_counts,
        "overdue_tasks": overdue,
        "escalations": open_escalations,
        "missions_24h": mission_counts,
        "attention": attention,
        "system_health": {
            "database": "OK",
            "schema": await current_revision_safe(conn),
            "worker_heartbeat_age_s": worker_age,
            "ai": ai_status,
        },
        "account_health": [
            {
                "alias": a.alias,
                "kind": a.kind,
                "reconciliation": a.reconciliation_status,
                "last_reconciled_at": a.last_reconciled_at,
            }
            for a in acct_rows
        ],
        "unknown_orders": unknown_orders,
        "degraded_protection": degraded,
        "checklist_open": checklist is not None,
    }


async def current_revision_safe(conn: AsyncConnection) -> str:
    from sqlalchemy import text

    try:
        return str(await conn.scalar(text("SELECT version_num FROM alembic_version")))
    except Exception:
        return "UNKNOWN"


__all__ = ["build", "current_revision"]
