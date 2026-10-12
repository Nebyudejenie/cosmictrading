"""Incidents: deduplicated, severity-ranked, acknowledged and resolved by the operator."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.errors import NotFoundError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.redaction import redact
from cosmic.db.schema import incidents
from cosmic.notify.alerts import Alert, notify_alert


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Category(StrEnum):
    ACCOUNTING = "ACCOUNTING"
    SECURITY = "SECURITY"
    DATA = "DATA"
    AI = "AI"
    OPS = "OPS"
    TRADING = "TRADING"


_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


async def raise_incident(
    conn: AsyncConnection,
    *,
    dedup_key: str,
    category: Category,
    alert: Alert,
    now: datetime,
    detail: dict[str, Any] | None = None,
    notify_kind: str = "incident",
    context: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, bool]:
    """Open an incident or bump the open one with the same key.

    The alert (all six answers, TRD-33) is stored with the incident and sent to the owners
    when the incident opens or escalates — never on repeats, which rate-limits alerts to
    state changes. Returns (incident id, is_new_or_escalated).
    """
    severity, title = Severity(alert.severity), alert.title
    safe_detail = redact(to_jsonable({**(detail or {}), "alert": alert.payload()}))
    existing = (
        await conn.execute(
            select(incidents.c.id, incidents.c.severity)
            .where(incidents.c.dedup_key == dedup_key, incidents.c.status != "RESOLVED")
            .with_for_update()
        )
    ).first()
    if existing is not None:
        escalated = _RANK[severity] > _RANK[Severity(existing.severity)]
        values: dict[str, Any] = {
            "occurrences": incidents.c.occurrences + 1,
            "last_seen_at": now,
            "detail": safe_detail,
        }
        if escalated:
            values["severity"] = severity.value
            values["status"] = "OPEN"
        await conn.execute(update(incidents).where(incidents.c.id == existing.id).values(**values))
        if escalated:
            await _notify(conn, existing.id, category, alert, now, notify_kind, context)
        return existing.id, escalated
    incident_id = uuid7()
    inserted = (
        await conn.execute(
            pg_insert(incidents)
            .values(
                id=incident_id,
                dedup_key=dedup_key,
                category=category.value,
                severity=severity.value,
                status="OPEN",
                title=title[:300],
                detail=safe_detail,
                opened_at=now,
                last_seen_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[incidents.c.dedup_key],
                index_where=text("status <> 'RESOLVED'"),
            )
            .returning(incidents.c.id)
        )
    ).first()
    if inserted is None:  # concurrent insert won
        return incident_id, False
    await record(
        conn,
        action="incident.opened",
        actor=Actor.system("incidents"),
        occurred_at=now,
        entity_type="incident",
        entity_id=incident_id,
        payload={
            "dedup_key": dedup_key,
            "category": category.value,
            "severity": severity.value,
            "title": title,
        },
    )
    await _notify(conn, incident_id, category, alert, now, notify_kind, context)
    return incident_id, True


async def _notify(
    conn: AsyncConnection,
    incident_id: uuid.UUID,
    category: Category,
    alert: Alert,
    now: datetime,
    kind: str,
    context: dict[str, Any] | None,
) -> None:
    await notify_alert(
        conn,
        kind=kind,
        alert=alert,
        now=now,
        dedup_key=f"incident:{incident_id}:{alert.severity}",
        context={**(context or {}), "incident_id": incident_id, "category": category.value},
    )


async def acknowledge(
    conn: AsyncConnection, *, incident_id: uuid.UUID, user_id: uuid.UUID, now: datetime
) -> None:
    result = await conn.execute(
        update(incidents)
        .where(incidents.c.id == incident_id, incidents.c.status == "OPEN")
        .values(status="ACKNOWLEDGED", acknowledged_at=now, acknowledged_by=user_id)
    )
    if result.rowcount == 0:
        raise NotFoundError("no open incident with that id")
    await record(
        conn,
        action="incident.acknowledged",
        actor=Actor.user(user_id),
        occurred_at=now,
        entity_type="incident",
        entity_id=incident_id,
    )


async def resolve(
    conn: AsyncConnection,
    *,
    incident_id: uuid.UUID,
    actor: Actor,
    resolved_by: uuid.UUID | None,
    now: datetime,
    resolution: str,
) -> None:
    result = await conn.execute(
        update(incidents)
        .where(incidents.c.id == incident_id, incidents.c.status != "RESOLVED")
        .values(
            status="RESOLVED",
            resolved_at=now,
            resolved_by=resolved_by,
            resolution=resolution[:1000],
        )
    )
    if result.rowcount == 0:
        raise NotFoundError("no unresolved incident with that id")
    await record(
        conn,
        action="incident.resolved",
        actor=actor,
        occurred_at=now,
        entity_type="incident",
        entity_id=incident_id,
        payload={"resolution": resolution},
    )


async def resolve_by_key(
    conn: AsyncConnection, *, dedup_key: str, now: datetime, resolution: str
) -> bool:
    row = (
        await conn.execute(
            select(incidents.c.id).where(
                incidents.c.dedup_key == dedup_key, incidents.c.status != "RESOLVED"
            )
        )
    ).first()
    if row is None:
        return False
    await resolve(
        conn,
        incident_id=row.id,
        actor=Actor.system("incidents"),
        resolved_by=None,
        now=now,
        resolution=resolution,
    )
    return True


async def list_unresolved(conn: AsyncConnection, limit: int = 100) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(incidents)
        .where(incidents.c.status != "RESOLVED")
        .order_by(
            text("CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END"),
            incidents.c.opened_at.desc(),
        )
        .limit(limit)
    )
    return [dict(r._mapping) for r in rows]


async def has_open_critical(conn: AsyncConnection, categories: list[Category]) -> bool:
    row = (
        await conn.execute(
            select(incidents.c.id)
            .where(
                incidents.c.status != "RESOLVED",
                incidents.c.severity == Severity.CRITICAL.value,
                incidents.c.category.in_([c.value for c in categories]),
            )
            .limit(1)
        )
    ).first()
    return row is not None
