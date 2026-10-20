"""Portal alert inbox: the PORTAL-channel notifications of one user.

For the portal channel, ``PENDING`` means unread and ``SENT`` (with ``sent_at``) means the
user has seen and acknowledged it. Telegram rows are delivered by the bot process.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import case, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.db.schema import notifications
from cosmic.notify.alerts import alert_from_payload, alert_headline


async def portal_alerts(
    conn: AsyncConnection, user_id: uuid.UUID, *, limit: int = 100
) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(notifications)
        .where(notifications.c.user_id == user_id, notifications.c.channel == "PORTAL")
        .order_by(
            case((notifications.c.status == "PENDING", 0), else_=1),
            desc(notifications.c.created_at),
        )
        .limit(limit)
    )
    items = []
    for r in rows:
        alert = alert_from_payload(r.payload)
        items.append(
            {
                "id": r.id,
                "kind": r.kind,
                "unread": r.status == "PENDING",
                "created_at": r.created_at,
                "alert": alert,
                "headline": alert_headline(alert) if alert else None,  # with the account
                "payload": r.payload,
            }
        )
    return items


async def unread_count(conn: AsyncConnection, user_id: uuid.UUID) -> int:
    n = await conn.scalar(
        select(func.count())
        .select_from(notifications)
        .where(
            notifications.c.user_id == user_id,
            notifications.c.channel == "PORTAL",
            notifications.c.status == "PENDING",
        )
    )
    return int(n or 0)


async def mark_read(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    now: datetime,
    alert_id: uuid.UUID | None = None,
) -> int:
    """Mark one (or every) unread portal alert of this user as read."""
    q = update(notifications).where(
        notifications.c.user_id == user_id,
        notifications.c.channel == "PORTAL",
        notifications.c.status == "PENDING",
    )
    if alert_id is not None:
        q = q.where(notifications.c.id == alert_id)
    result = await conn.execute(q.values(status="SENT", sent_at=now))
    if result.rowcount:
        await record(
            conn,
            action="alerts.read",
            actor=Actor.user(user_id),
            occurred_at=now,
            entity_type="user",
            entity_id=user_id,
            payload={"count": result.rowcount, "alert_id": alert_id},
        )
    return int(result.rowcount)
