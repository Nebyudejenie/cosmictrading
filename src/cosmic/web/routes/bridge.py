"""The MetaTrader 5 bridge's single endpoint (ADR-028).

The bridge on the Windows computer calls ``POST /bridge/v1/sync`` about once a second: it sends
its snapshot and command results and receives new commands. It is a machine client: no session,
no form, a bearer token compared in constant time. Without a configured token the endpoint does
not exist (404).
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from cosmic.core.errors import AuthenticationError, NotFoundError, ValidationFailedError
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.web.deps import state

router = APIRouter()
MAX_BODY = 2_000_000


@router.post("/bridge/v1/sync", include_in_schema=False)
async def bridge_sync(request: Request) -> Response:
    st = state(request)
    token = st.settings.hfm_bridge_token
    if token is None:
        raise NotFoundError("no bridge is configured")
    expected = f"Bearer {token.get_secret_value()}".encode()
    if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected):
        raise AuthenticationError("bridge token required")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ValidationFailedError("sync too large")
    try:
        sync = BridgeSync.model_validate_json(body)
    except ValidationError as exc:
        raise ValidationFailedError(f"invalid sync: {exc.error_count()} problem(s)") from None
    commands = await apply_sync(st.db, st.clock, sync)
    now_ms = int(st.clock.now().timestamp() * 1000)
    return JSONResponse({"commands": commands, "server_time_ms": now_ms})
