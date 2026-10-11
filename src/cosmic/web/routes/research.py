"""Research missions, reports and strategies (read-only pages)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response

from cosmic.core.errors import NotFoundError
from cosmic.identity.capabilities import Capability
from cosmic.research import queries
from cosmic.web.deps import authorize, state
from cosmic.web.views import render

router = APIRouter()


@router.get("/research", include_in_schema=False)
async def research_page(request: Request) -> Response:
    principal = await authorize(request, Capability.VIEW)
    async with state(request).db.connect() as conn:
        rows = await queries.mission_list(conn)
        board = await queries.task_board(conn)
    return await render(request, "research.html", principal, missions=rows, board=board)


@router.get("/research/{mission_id}", include_in_schema=False)
async def mission_page(request: Request, mission_id: str) -> Response:
    principal = await authorize(request, Capability.VIEW)
    try:
        mid = uuid.UUID(mission_id)
    except ValueError:
        raise NotFoundError("unknown mission") from None
    async with state(request).db.connect() as conn:
        d = await queries.mission_detail(conn, mid)
    if d is None:
        raise NotFoundError("unknown mission")
    report = next((a.content for a in d["artifacts"] if a.kind == "report"), None)
    return await render(request, "mission.html", principal, d=d, report=report)


@router.get("/strategies", include_in_schema=False)
async def strategies_page(request: Request) -> Response:
    principal = await authorize(request, Capability.VIEW)
    async with state(request).db.connect() as conn:
        rows = await queries.strategy_overview(conn)
    return await render(request, "strategies.html", principal, strategies=rows)
