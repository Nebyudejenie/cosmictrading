"""Process heartbeats (worker, bot): liveness plus non-secret status reports."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema import worker_heartbeats


async def beat(
    conn: AsyncConnection,
    *,
    worker_id: str,
    process: str,
    started_at: datetime,
    now: datetime,
    info: dict[str, Any],
) -> None:
    stmt = pg_insert(worker_heartbeats).values(
        worker_id=worker_id, process=process, started_at=started_at, last_beat_at=now, info=info
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[worker_heartbeats.c.worker_id],
            set_={"last_beat_at": now, "info": info, "started_at": started_at},
        )
    )
