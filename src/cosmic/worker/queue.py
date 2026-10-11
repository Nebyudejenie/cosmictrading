"""PostgreSQL job queue (``FOR UPDATE SKIP LOCKED``). Handlers must be idempotent."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.redaction import scrub_text
from cosmic.db.schema import jobs

# Priority bands: lower runs first. Background work (>= BACKGROUND) is shed under pressure.
PRIORITY_SAFETY = 10
PRIORITY_TRADING = 20
PRIORITY_DATA = 50
PRIORITY_DEFAULT = 100
PRIORITY_BACKGROUND = 200


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    priority: int


async def enqueue(
    conn: AsyncConnection,
    *,
    kind: str,
    now: datetime,
    payload: dict[str, Any] | None = None,
    run_at: datetime | None = None,
    priority: int = PRIORITY_DEFAULT,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> uuid.UUID | None:
    """Insert a job; returns its ID, or None if the idempotency key already exists."""
    job_id = uuid7()
    stmt = (
        pg_insert(jobs)
        .values(
            id=job_id,
            kind=kind,
            payload=to_jsonable(payload or {}),
            priority=priority,
            run_at=run_at or now,
            status="QUEUED",
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[jobs.c.idempotency_key])
        .returning(jobs.c.id)
    )
    result = await conn.execute(stmt)
    row = result.first()
    return None if row is None else row.id


async def claim(
    conn: AsyncConnection,
    *,
    worker_id: str,
    now: datetime,
    lease_seconds: int = 300,
    max_priority: int | None = None,
    kinds: list[str] | None = None,
) -> Job | None:
    conditions = [jobs.c.status == "QUEUED", jobs.c.run_at <= now]
    if max_priority is not None:
        conditions.append(jobs.c.priority < max_priority)
    if kinds:
        conditions.append(jobs.c.kind.in_(kinds))
    row = (
        await conn.execute(
            select(jobs.c.id)
            .where(and_(*conditions))
            .order_by(jobs.c.priority, jobs.c.run_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).first()
    if row is None:
        return None
    claimed = (
        await conn.execute(
            update(jobs)
            .where(jobs.c.id == row.id)
            .values(
                status="RUNNING",
                attempts=jobs.c.attempts + 1,
                locked_by=worker_id,
                locked_until=now + timedelta(seconds=lease_seconds),
                started_at=now,
            )
            .returning(
                jobs.c.id,
                jobs.c.kind,
                jobs.c.payload,
                jobs.c.attempts,
                jobs.c.max_attempts,
                jobs.c.priority,
            )
        )
    ).one()
    return Job(
        claimed.id,
        claimed.kind,
        dict(claimed.payload),
        claimed.attempts,
        claimed.max_attempts,
        claimed.priority,
    )


async def complete(conn: AsyncConnection, *, job_id: uuid.UUID, now: datetime) -> None:
    await conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .values(status="DONE", finished_at=now, locked_by=None, locked_until=None, last_error=None)
    )


def backoff_seconds(attempt: int, base: float = 5.0, cap: float = 600.0) -> float:
    """Full-jitter exponential backoff."""
    return random.uniform(0, min(cap, base * (2 ** max(0, attempt - 1))))  # noqa: S311 - jitter  # nosec B311


async def fail(
    conn: AsyncConnection, *, job: Job, now: datetime, error: str, retryable: bool = True
) -> str:
    """Record a failure; requeue with backoff or mark DEAD. Returns the new status."""
    dead = not retryable or job.attempts >= job.max_attempts
    status = "DEAD" if dead else "QUEUED"
    await conn.execute(
        update(jobs)
        .where(jobs.c.id == job.id)
        .values(
            status=status,
            last_error=scrub_text(error)[:2000],
            locked_by=None,
            locked_until=None,
            finished_at=now if dead else None,
            run_at=now if dead else now + timedelta(seconds=backoff_seconds(job.attempts)),
        )
    )
    return status


async def recover_expired_leases(conn: AsyncConnection, *, now: datetime) -> int:
    """Requeue jobs whose worker died mid-run (lease expired). Idempotent handlers required."""
    result = await conn.execute(
        update(jobs)
        .where(jobs.c.status == "RUNNING", jobs.c.locked_until < now)
        .values(
            status="QUEUED",
            locked_by=None,
            locked_until=None,
            run_at=now,
            last_error="lease expired (worker stopped mid-run)",
        )
    )
    return int(result.rowcount)


async def queue_stats(conn: AsyncConnection, *, now: datetime) -> list[dict[str, Any]]:
    from sqlalchemy import func

    rows = await conn.execute(
        select(
            jobs.c.kind,
            jobs.c.status,
            func.count().label("n"),
            func.min(jobs.c.run_at).label("oldest"),
        )
        .where(jobs.c.status.in_(["QUEUED", "RUNNING", "DEAD"]))
        .group_by(jobs.c.kind, jobs.c.status)
        .order_by(jobs.c.kind)
    )
    out = []
    for r in rows:
        age = (
            (now - r.oldest).total_seconds()
            if r.oldest is not None and r.status == "QUEUED"
            else None
        )
        out.append({"kind": r.kind, "status": r.status, "count": int(r.n), "oldest_age_s": age})
    return out
