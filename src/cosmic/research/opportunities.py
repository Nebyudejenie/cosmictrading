"""Opportunity lifecycle (QNT-12): IDEA → RESEARCH → CHALLENGED → QUALIFIED → APPROVAL_REQUIRED →
APPROVED / REJECTED / EXPIRED → EXECUTION → REVIEW; plus NON_EXECUTABLE and NO_TRADE.
No path leads from IDEA straight to execution."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable_lenient
from cosmic.db.schema_research import opportunities, opportunity_transitions

ALLOWED: dict[str, frozenset[str]] = {
    "IDEA": frozenset({"RESEARCH", "NO_TRADE", "NON_EXECUTABLE"}),
    "RESEARCH": frozenset({"CHALLENGED", "NO_TRADE", "NON_EXECUTABLE"}),
    "CHALLENGED": frozenset({"QUALIFIED", "NO_TRADE", "NON_EXECUTABLE"}),
    "QUALIFIED": frozenset({"APPROVAL_REQUIRED", "NON_EXECUTABLE", "NO_TRADE"}),
    "APPROVAL_REQUIRED": frozenset({"APPROVED", "REJECTED", "EXPIRED"}),
    "APPROVED": frozenset({"EXECUTION", "EXPIRED", "REJECTED"}),
    "EXECUTION": frozenset({"REVIEW"}),
    "REJECTED": frozenset(),
    "EXPIRED": frozenset(),
    "REVIEW": frozenset(),
    "NON_EXECUTABLE": frozenset(),
    "NO_TRADE": frozenset(),
}

INTENT_TO_OPPORTUNITY = {
    "PROPOSED": "APPROVAL_REQUIRED",
    "APPROVED": "APPROVED",
    "REJECTED": "REJECTED",
    "CHANGES_REQUESTED": "REJECTED",
    "EXPIRED": "EXPIRED",
    "EXECUTING": "EXECUTION",
    "EXECUTED": "EXECUTION",
    "EXECUTION_FAILED": "EXPIRED",
    "BLOCKED": "EXPIRED",
    "REVOKED": "REJECTED",
}


async def create(
    conn: AsyncConnection,
    *,
    mission_id: uuid.UUID,
    instrument_id: uuid.UUID,
    now: datetime,
    strategy_version_id: uuid.UUID | None = None,
) -> uuid.UUID:
    oid = uuid7()
    await conn.execute(
        opportunities.insert().values(
            id=oid,
            mission_id=mission_id,
            instrument_id=instrument_id,
            strategy_version_id=strategy_version_id,
            status="IDEA",
            created_at=now,
            updated_at=now,
        )
    )
    await conn.execute(
        opportunity_transitions.insert().values(
            id=uuid7(),
            opportunity_id=oid,
            from_status=None,
            to_status="IDEA",
            reason="mission opened",
            occurred_at=now,
        )
    )
    return oid


async def advance(
    conn: AsyncConnection,
    opportunity_id: uuid.UUID,
    to: str,
    reason: str,
    now: datetime,
    **values: Any,
) -> str:
    row = (
        await conn.execute(
            select(opportunities).where(opportunities.c.id == opportunity_id).with_for_update()
        )
    ).one()
    if to == row.status:
        return str(row.status)
    if to not in ALLOWED[row.status]:
        raise ConflictError(f"opportunity cannot move {row.status} → {to}")
    if "summary" in values:
        values["summary"] = to_jsonable_lenient(values["summary"])
    await conn.execute(
        update(opportunities)
        .where(opportunities.c.id == opportunity_id)
        .values(status=to, updated_at=now, **values)
    )
    await conn.execute(
        opportunity_transitions.insert().values(
            id=uuid7(),
            opportunity_id=opportunity_id,
            from_status=row.status,
            to_status=to,
            reason=reason[:500],
            occurred_at=now,
        )
    )
    return to


async def sync_from_intents(conn: AsyncConnection, now: datetime) -> int:
    """Follow the trade intent's status (APPROVAL_REQUIRED → APPROVED → EXECUTION …)."""
    from cosmic.db.schema_execution import trade_intents

    rows = list(
        await conn.execute(
            select(
                opportunities.c.id,
                opportunities.c.status,
                trade_intents.c.status.label("intent_status"),
            )
            .join(trade_intents, trade_intents.c.id == opportunities.c.intent_id)
            .where(opportunities.c.status.in_(["APPROVAL_REQUIRED", "APPROVED", "EXECUTION"]))
        )
    )
    moved = 0
    for r in rows:
        target = INTENT_TO_OPPORTUNITY.get(r.intent_status)
        if not target or target == r.status:
            continue
        for step in path(r.status, target):
            await advance(conn, r.id, step, f"trade intent is {r.intent_status}", now)
            moved += 1
    return moved


def path(current: str, target: str) -> list[str]:
    """Shortest legal path (breadth-first) so a late sync never skips or blocks a stage."""
    frontier: list[list[str]] = [[current]]
    seen = {current}
    while frontier:
        route = frontier.pop(0)
        for nxt in sorted(ALLOWED[route[-1]]):
            if nxt == target:
                return [*route[1:], nxt]
            if nxt not in seen:
                seen.add(nxt)
                frontier.append([*route, nxt])
    return []
