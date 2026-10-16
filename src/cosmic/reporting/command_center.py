"""The command center answers §11's questions from stored state, with honest labels."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, NotFoundError
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
from cosmic.db.schema_trading import accounts, bridge_state, positions
from cosmic.market import data as md
from cosmic.ops.health import current_revision
from cosmic.sources.freshness import classify
from cosmic.trading.policy import active_policy
from cosmic.trading.queries import authority_banner, portfolio


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
        "attention": [text for _, text in await attention_items(conn, now)],
        "accounts": await portfolio(conn, now),
        "prices": await prices(conn, now),
        "connections": await connections(conn, now),
        "strategies": await strategy_status(conn),
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


# ---- shared by the Overview, the daily report and Telegram /status -----------------------------

BRIDGE_STALE_SECONDS = 15  # the HFM adapter treats an older report as unavailable (ADR-028)


async def attention_items(conn: AsyncConnection, now: datetime) -> list[tuple[str, str]]:
    """What needs the owner now, as (kind, text). One set of rules for every place that tells
    them; a place that already shows a kind (the pending count, the worker) can leave it out."""
    pending = int(
        await conn.scalar(
            select(func.count())
            .select_from(trade_intents)
            .where(trade_intents.c.status == "PROPOSED")
        )
        or 0
    )
    critical = int(
        await conn.scalar(
            select(func.count())
            .select_from(incidents)
            .where(incidents.c.status == "OPEN", incidents.c.severity == "CRITICAL")
        )
        or 0
    )
    checklist = (
        await conn.execute(
            select(recovery_checklists.c.id).where(recovery_checklists.c.status == "OPEN")
        )
    ).first()
    bad = [
        r.alias
        for r in await conn.execute(
            select(accounts.c.alias).where(accounts.c.reconciliation_status != "OK")
        )
    ]
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
    open_escalations = int(
        await conn.scalar(
            select(func.count()).select_from(escalations).where(escalations.c.resolved_at.is_(None))
        )
        or 0
    )
    beat = await conn.scalar(
        select(func.max(worker_heartbeats.c.last_beat_at)).where(
            worker_heartbeats.c.process == "worker"
        )
    )
    worker_age = None if beat is None else (now - beat).total_seconds()
    out: list[tuple[str, str]] = []
    if pending:
        out.append(("pending", f"{pending} proposal(s) await your decision"))
    if critical:
        out.append(("incidents", f"{critical} critical incident(s) unacknowledged"))
    if checklist is not None:
        out.append(("emergency", "emergency stop active: recovery checklist open"))
    if bad:
        out.append(
            ("reconciliation", f"reconciliation not OK for {', '.join(bad)} (new exposure blocked)")
        )
    if unknown_orders:
        out.append(("orders", f"{unknown_orders} order(s) in UNKNOWN state"))
    if degraded:
        out.append(("protection", f"{degraded} position(s) without verified protection"))
    if open_escalations:
        out.append(("escalations", f"{open_escalations} escalated task(s)"))
    if worker_age is None or worker_age > 60:
        out.append(
            ("worker", "no recent worker heartbeat: background work and execution are not running")
        )
    return out


async def connections(conn: AsyncConnection, now: datetime) -> dict[str, Any]:
    """Heartbeats of the worker, the Telegram bot and the HFM bridge, and the worker's view of
    the backups: each with its age, so nothing stale reads as current."""
    beats: dict[str, float] = {}
    worker_info: dict[str, Any] = {}
    for b in await conn.execute(
        select(worker_heartbeats).order_by(desc(worker_heartbeats.c.last_beat_at))
    ):
        if b.process not in beats:
            beats[b.process] = (now - b.last_beat_at).total_seconds()
            if b.process == "worker":
                worker_info = b.info or {}
    bridges = [
        {
            "bridge": r.bridge,
            "version": (r.snapshot or {}).get("version"),
            "age_s": (now - r.received_at).total_seconds(),
            "account_mode": ((r.snapshot or {}).get("account") or {}).get("trade_mode"),
            "fresh": (now - r.received_at).total_seconds() <= BRIDGE_STALE_SECONDS,
        }
        for r in await conn.execute(select(bridge_state).order_by(bridge_state.c.bridge))
    ]
    return {
        "worker_age_s": beats.get("worker"),
        "bot_age_s": beats.get("bot"),
        "bridges": bridges,
        "backups": worker_info.get("backups"),
    }


async def prices(conn: AsyncConnection, now: datetime) -> list[dict[str, Any]]:
    """The latest quote of every instrument the risk policy lets you trade, labelled."""
    try:
        _, policy = await active_policy(conn)
    except ConflictError:
        return []
    out: list[dict[str, Any]] = []
    for key in policy.allowed_instruments:
        try:
            inst = await md.instrument_by_key(conn, key)
        except NotFoundError:
            continue
        if not inst.tradable:
            continue
        q = await md.latest_quote(conn, inst.id, now)
        out.append(
            {
                "key": key,
                "bid": q.bid,
                "ask": q.ask,
                "quote_ts": q.source_ts,
                "age_s": q.age_seconds,
                "label": q.label.value,
                "source": q.source_key,
            }
        )
    return out


async def strategy_status(conn: AsyncConnection) -> dict[str, int]:
    """Rules QUALIFIED in the latest library runs, and rules the owner has switched on."""
    # Imported here: the bot answers /status without loading the research library.
    from cosmic.research.library.engine import BINANCE_LIKE, COINBASE_LIKE
    from cosmic.research.library.store import active, latest_run, run_evaluations

    qualified = 0
    for costs in (BINANCE_LIKE, COINBASE_LIKE):
        run = await latest_run(conn, costs.name)
        if run is not None:
            qualified += sum(
                1 for e in await run_evaluations(conn, run.id) if e.tier == "QUALIFIED"
            )
    return {"qualified": qualified, "switched_on": len(await active(conn))}
