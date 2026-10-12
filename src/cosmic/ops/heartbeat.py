"""Process heartbeats (worker, bot): liveness plus non-secret status reports."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete
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


STALE_AFTER = timedelta(hours=1)


async def prune_stale(conn: AsyncConnection, now: datetime) -> int:
    """Remove rows of processes that stopped beating over an hour ago. Every container
    restart starts a new process id, so old rows would otherwise pile up and show as
    failing workers. A dead *current* worker still shows as failing for that hour, then
    the operations page reports that no worker heartbeat exists at all."""
    result = await conn.execute(
        delete(worker_heartbeats).where(worker_heartbeats.c.last_beat_at < now - STALE_AFTER)
    )
    return int(result.rowcount)
