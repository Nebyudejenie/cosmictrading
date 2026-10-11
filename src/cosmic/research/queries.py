"""Read models for research pages."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema_market import claims, instruments
from cosmic.db.schema_research import (
    artifacts,
    backtest_runs,
    missions,
    opportunities,
    opportunity_transitions,
    strategies,
    strategy_versions,
    task_reviews,
    tasks,
)


async def mission_list(conn: AsyncConnection, limit: int = 50) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(
            missions,
            instruments.c.canonical_key,
            opportunities.c.status.label("opportunity_status"),
        )
        .join(instruments, instruments.c.id == missions.c.instrument_id, isouter=True)
        .join(opportunities, opportunities.c.mission_id == missions.c.id, isouter=True)
        .order_by(desc(missions.c.created_at))
        .limit(limit)
    )
    return [dict(r._mapping) for r in rows]


async def task_board(conn: AsyncConnection) -> dict[str, int]:
    rows = await conn.execute(
        select(tasks.c.status, func.count().label("n")).group_by(tasks.c.status)
    )
    return {r.status: int(r.n) for r in rows}


async def mission_detail(conn: AsyncConnection, mission_id: uuid.UUID) -> dict[str, Any] | None:
    m = (await conn.execute(select(missions).where(missions.c.id == mission_id))).first()
    if m is None:
        return None
    ts = list(
        await conn.execute(
            select(tasks).where(tasks.c.mission_id == mission_id).order_by(tasks.c.created_at)
        )
    )
    reviews = (
        {
            r.task_id: r
            for r in await conn.execute(
                select(task_reviews).where(task_reviews.c.task_id.in_([t.id for t in ts]))
            )
        }
        if ts
        else {}
    )
    arts = list(await conn.execute(select(artifacts).where(artifacts.c.mission_id == mission_id)))
    cl = list(
        await conn.execute(
            select(claims).where(claims.c.mission_id == mission_id).order_by(claims.c.created_at)
        )
    )
    o = (
        await conn.execute(select(opportunities).where(opportunities.c.mission_id == mission_id))
    ).first()
    trans = (
        []
        if o is None
        else list(
            await conn.execute(
                select(opportunity_transitions)
                .where(opportunity_transitions.c.opportunity_id == o.id)
                .order_by(opportunity_transitions.c.occurred_at)
            )
        )
    )
    return {
        "mission": m,
        "tasks": ts,
        "reviews": reviews,
        "artifacts": arts,
        "claims": cl,
        "opportunity": o,
        "transitions": trans,
    }


async def strategy_overview(conn: AsyncConnection) -> list[dict[str, Any]]:
    out = []
    for s in await conn.execute(select(strategies).order_by(strategies.c.key)):
        versions = list(
            await conn.execute(
                select(strategy_versions)
                .where(strategy_versions.c.strategy_id == s.id)
                .order_by(desc(strategy_versions.c.version))
            )
        )
        runs = (
            list(
                await conn.execute(
                    select(backtest_runs, instruments.c.canonical_key)
                    .join(instruments, instruments.c.id == backtest_runs.c.instrument_id)
                    .where(backtest_runs.c.strategy_version_id.in_([v.id for v in versions]))
                    .order_by(desc(backtest_runs.c.created_at))
                    .limit(10)
                )
            )
            if versions
            else []
        )
        out.append({"strategy": s, "versions": versions, "runs": runs})
    return out
