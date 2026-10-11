"""Notification outbox. Rows are written in the same transaction as the event they report.

Portal rows are always created for owners; Telegram rows only for owners who linked an
account (delivery is done by the bot process; failures are recorded, never hidden).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import any_, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.redaction import redact
from cosmic.db.schema import notifications, users


async def notify_owners(
    conn: AsyncConnection,
    *,
    kind: str,
    payload: dict[str, Any],
    now: datetime,
    dedup_key: str | None = None,
) -> int:
    safe = redact(to_jsonable(payload))
    owners = list(
        await conn.execute(
            select(users.c.id, users.c.telegram_user_id).where(
                users.c.is_active.is_(True), literal("OWNER") == any_(users.c.roles)
            )
        )
    )
    created = 0
    for owner in owners:
        channels = ["PORTAL"] + (["TELEGRAM"] if owner.telegram_user_id is not None else [])
        for channel in channels:
            key = None if dedup_key is None else f"{dedup_key}:{owner.id}:{channel}"
            result = await conn.execute(
                pg_insert(notifications)
                .values(
                    id=uuid7(),
                    user_id=owner.id,
                    channel=channel,
                    kind=kind,
                    payload=safe,
                    status="PENDING",
                    dedup_key=key,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[notifications.c.dedup_key])
            )
            created += result.rowcount
    return created
