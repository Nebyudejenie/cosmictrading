"""Task engine (WF-04/05/06): dependencies, depth, attempts, verification, escalation, expiry."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import canonical_hash_lenient, to_jsonable_lenient
from cosmic.core.redaction import redact, scrub_text
from cosmic.db.schema_research import (
    artifacts,
    escalations,
    task_attempts,
    task_deps,
    task_reviews,
    tasks,
)
from cosmic.notify.alerts import Alert, notify_alert
from cosmic.workforce.roles import ROLES

MAX_DEPTH = 3
OPEN_STATES = ("QUEUED", "WAITING_FOR_AI", "RUNNING", "IN_REVIEW", "BLOCKED")


async def create_task(
    conn: AsyncConnection,
    *,
    mission_id: uuid.UUID,
    role_key: str,
    kind: str,
    title: str,
    now: datetime,
    expires_at: datetime,
    idempotency_key: str,
    acceptance: dict[str, Any],
    input: dict[str, Any] | None = None,
    depends_on: list[uuid.UUID] | None = None,
    parent_task_id: uuid.UUID | None = None,
    depth: int = 0,
    priority: int = 100,
) -> uuid.UUID:
    role = ROLES[role_key]
    if depth > MAX_DEPTH:
        raise ConflictError(f"delegation depth {depth} exceeds {MAX_DEPTH}")
    existing = (
        await conn.execute(select(tasks.c.id).where(tasks.c.idempotency_key == idempotency_key))
    ).first()
    if existing is not None:
        return uuid.UUID(str(existing.id))
    tid = uuid7()
    await conn.execute(
        tasks.insert().values(
            id=tid,
            mission_id=mission_id,
            parent_task_id=parent_task_id,
            role_key=role_key,
            kind=kind,
            title=title[:200],
            status="QUEUED",
            priority=priority,
            depth=depth,
            expires_at=expires_at,
            input=to_jsonable_lenient(input or {}),
            acceptance=to_jsonable_lenient(acceptance),
            idempotency_key=idempotency_key,
            max_attempts=role.max_attempts,
            ai_call_budget=role.ai_call_budget,
            reviewer_role=role.reviewer,
            created_at=now,
            updated_at=now,
        )
    )
    for dep in depends_on or []:
        await conn.execute(task_deps.insert().values(task_id=tid, depends_on=dep))
    return tid


async def runnable(conn: AsyncConnection, now: datetime, limit: int = 20) -> list[Any]:
    """QUEUED/WAITING_FOR_AI tasks whose dependencies are all DONE (or SKIPPED) and not expired."""
    dep = task_deps.alias("d")
    blocker = tasks.alias("b")
    unfinished = exists(
        select(dep.c.task_id)
        .join(blocker, blocker.c.id == dep.c.depends_on)
        .where(and_(dep.c.task_id == tasks.c.id, blocker.c.status.notin_(["DONE", "SKIPPED"])))
    )
    rows = await conn.execute(
        select(tasks)
        .where(
            tasks.c.status.in_(["QUEUED", "WAITING_FOR_AI"]), tasks.c.expires_at > now, ~unfinished
        )
        .order_by(tasks.c.priority, tasks.c.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows)


async def start(conn: AsyncConnection, task: Any, *, worker_id: str, now: datetime) -> uuid.UUID:
    await conn.execute(
        update(tasks)
        .where(tasks.c.id == task.id)
        .values(status="RUNNING", attempts=tasks.c.attempts + 1, updated_at=now)
    )
    attempt_id = uuid7()
    await conn.execute(
        task_attempts.insert().values(
            id=attempt_id,
            task_id=task.id,
            attempt_no=task.attempts + 1,
            worker_id=worker_id,
            started_at=now,
            status="RUNNING",
        )
    )
    return attempt_id


async def finish_attempt(
    conn: AsyncConnection,
    attempt_id: uuid.UUID,
    status: str,
    now: datetime,
    error: str | None = None,
) -> None:
    await conn.execute(
        update(task_attempts)
        .where(task_attempts.c.id == attempt_id)
        .values(
            finished_at=now, status=status, error=None if error is None else scrub_text(error)[:500]
        )
    )


async def submit_output(
    conn: AsyncConnection, task: Any, output: dict[str, Any], now: datetime
) -> str:
    """An agent's claim of completion is not proof: tasks with a reviewer go to IN_REVIEW."""
    status = "IN_REVIEW" if task.reviewer_role else "DONE"
    await conn.execute(
        update(tasks)
        .where(tasks.c.id == task.id)
        .values(
            status=status,
            output=redact(to_jsonable_lenient(output)),
            updated_at=now,
            completed_at=now if status == "DONE" else None,
        )
    )
    return status


async def review(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    checks: dict[str, bool],
    now: datetime,
    reviewer: str = "task_reviewer",
) -> str:
    task = (await conn.execute(select(tasks).where(tasks.c.id == task_id).with_for_update())).one()
    verdict = "PASS" if checks and all(checks.values()) else "FAIL"
    await conn.execute(
        task_reviews.insert().values(
            id=uuid7(),
            task_id=task_id,
            reviewer_role=reviewer,
            verdict=verdict,
            checks=checks,
            created_at=now,
        )
    )
    if verdict == "PASS":
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(status="DONE", completed_at=now, updated_at=now)
        )
        return "DONE"
    if task.attempts >= task.max_attempts:
        await escalate(
            conn,
            task,
            f"failed review after {task.attempts} attempts: "
            + ", ".join(k for k, v in checks.items() if not v),
            now,
        )
        return "ESCALATED"
    await conn.execute(
        update(tasks)
        .where(tasks.c.id == task_id)
        .values(
            status="QUEUED",
            blocker="reopened by reviewer: " + ", ".join(k for k, v in checks.items() if not v),
            updated_at=now,
        )
    )
    return "REOPENED"


async def fail(
    conn: AsyncConnection, task: Any, error: str, now: datetime, *, retryable: bool = True
) -> str:
    if retryable and task.attempts < task.max_attempts:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task.id)
            .values(status="QUEUED", blocker=scrub_text(error)[:300], updated_at=now)
        )
        return "QUEUED"
    await escalate(conn, task, error, now)
    return "ESCALATED"


async def wait_for_ai(conn: AsyncConnection, task: Any, reason: str, now: datetime) -> None:
    """No eligible AI route: wait without consuming an attempt (AI-15/22)."""
    await conn.execute(
        update(tasks)
        .where(tasks.c.id == task.id)
        .values(
            status="WAITING_FOR_AI",
            attempts=tasks.c.attempts - 1,
            blocker=reason[:300],
            updated_at=now,
        )
    )


async def escalate(conn: AsyncConnection, task: Any, reason: str, now: datetime) -> None:
    await conn.execute(
        update(tasks)
        .where(tasks.c.id == task.id)
        .values(status="ESCALATED", blocker=reason[:300], updated_at=now)
    )
    await conn.execute(
        escalations.insert().values(
            id=uuid7(), task_id=task.id, reason=scrub_text(reason)[:500], created_at=now
        )
    )
    await notify_alert(
        conn,
        kind="task_escalated",
        alert=Alert(
            title=f"Research task needs you: {task.title}",
            severity="INFO",
            what_happened=f"The task was escalated: {scrub_text(reason)[:200]}",
            verified="The task stopped; its earlier work and evidence are kept.",
            uncertain="Whether retrying would help.",
            risk_now="None to capital: research cannot trade.",
            action_taken="Task marked ESCALATED; it will not retry by itself.",
            owner_action="Open the mission on the Research page and decide: retry, skip or "
            "close it.",
        ),
        now=now,
        dedup_key=f"escalated:{task.id}",
        context={"task": task.title},
    )


async def skip(conn: AsyncConnection, task_id: uuid.UUID, reason: str, now: datetime) -> None:
    await conn.execute(
        update(tasks)
        .where(tasks.c.id == task_id, tasks.c.status.in_(OPEN_STATES))
        .values(status="SKIPPED", blocker=reason[:300], updated_at=now, completed_at=now)
    )


async def expire_sweep(conn: AsyncConnection, now: datetime) -> int:
    result = await conn.execute(
        update(tasks)
        .where(tasks.c.status.in_(OPEN_STATES), tasks.c.expires_at <= now)
        .values(status="EXPIRED", updated_at=now)
    )
    return int(result.rowcount)


async def add_artifact(
    conn: AsyncConnection,
    *,
    mission_id: uuid.UUID,
    task_id: uuid.UUID | None,
    kind: str,
    content: dict[str, Any],
    now: datetime,
) -> uuid.UUID:
    safe = redact(to_jsonable_lenient(content))
    aid = uuid7()
    await conn.execute(
        artifacts.insert().values(
            id=aid,
            mission_id=mission_id,
            task_id=task_id,
            kind=kind,
            content=safe,
            content_hash=canonical_hash_lenient(safe),
            created_at=now,
        )
    )
    return aid


def default_expiry(now: datetime, hours: int = 2) -> datetime:
    return now + timedelta(hours=hours)
