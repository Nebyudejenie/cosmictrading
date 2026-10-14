"""Research missions, reports and strategies (read-only pages)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response

from cosmic.core.errors import NotFoundError
from cosmic.identity.capabilities import Capability
from cosmic.research import queries
from cosmic.research.library.catalog import BY_KEY as LIBRARY_KEYS
from cosmic.research.search import search
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
    # The 100 library rules have their own pages (/strategies/library).
    rows = [r for r in rows if r["strategy"].key not in LIBRARY_KEYS]
    return await render(request, "strategies.html", principal, strategies=rows)


@router.get("/search", include_in_schema=False)
async def search_page(request: Request) -> Response:
    principal = await authorize(request, Capability.VIEW)
    q = request.query_params.get("q", "")[:200]
    hits = []
    if q.strip():
        async with state(request).db.connect() as conn:
            hits = await search(conn, q, include_proposals=principal.has(Capability.READ_ACCOUNT))
    return await render(request, "search.html", principal, q=q, hits=hits)
