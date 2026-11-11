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
from pydantic import SecretStr, ValidationError

from cosmic.core.config import Settings
from cosmic.core.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_2, BRIDGE_REAL
from cosmic.web.deps import state

router = APIRouter()
MAX_BODY = 2_000_000


def _tokens(settings: Settings) -> dict[str, SecretStr]:
    """Each configured bridge's token (S74: one per MT5 terminal, so a bridge can only ever sync
    as itself)."""
    found = {
        BRIDGE: settings.hfm_bridge_token,
        BRIDGE_2: settings.hfm_bridge2_token,
        BRIDGE_REAL: settings.hfm_bridge3_token,  # S83: the read-only terminal (REAL account)
    }
    return {name: t for name, t in found.items() if t is not None and t.get_secret_value()}


@router.post("/bridge/v1/sync", include_in_schema=False)
async def bridge_sync(request: Request) -> Response:
    st = state(request)
    tokens = _tokens(st.settings)
    if not tokens:
        raise NotFoundError("no bridge is configured")
    given = request.headers.get("authorization", "").encode()
    matched = None
    for name, token in tokens.items():  # every comparison runs, in constant time each
        if hmac.compare_digest(given, f"Bearer {token.get_secret_value()}".encode()):
            matched = name
    if matched is None:
        raise AuthenticationError("bridge token required")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ValidationFailedError("sync too large")
    try:
        sync = BridgeSync.model_validate_json(body)
    except ValidationError as exc:
        raise ValidationFailedError(f"invalid sync: {exc.error_count()} problem(s)") from None
    if sync.bridge != matched:  # a token is one bridge's, never another's
        raise PermissionDeniedError("this token belongs to another bridge")
    commands = await apply_sync(st.db, st.clock, sync, st.settings)
    now_ms = int(st.clock.now().timestamp() * 1000)
    return JSONResponse({"commands": commands, "server_time_ms": now_ms})
