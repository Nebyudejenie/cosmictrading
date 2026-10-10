from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cosmic.ops.health import readiness
from cosmic.web.deps import state

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    ok, checks = await readiness(state(request).db)
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503
    )
