"""The MetaTrader 5 bridge's endpoints (ADR-028).

The bridge on the Windows computer calls ``POST /bridge/v1/sync`` every second or so: it sends
its snapshot and command results and receives new commands. From bridge 8 it also keeps
``POST /bridge/v1/fast`` open all the time (S87): HFM's newest prices up, and an order down the
moment one waits. It is a machine client: no session, no form, a bearer token compared in
constant time. Without a configured token the endpoints do not exist (404).
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
from cosmic.trading.bridge_fast import OFFER, BridgeFast, apply_fast
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.trading.brokers.mt5_bridge import BRIDGE, BRIDGE_2, BRIDGE_REAL, BRIDGES
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


async def _bridge(request: Request) -> tuple[str, bytes]:
    """The bridge this request's token belongs to, and its body."""
    tokens = _tokens(state(request).settings)
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
    return matched, body


@router.post("/bridge/v1/sync", include_in_schema=False)
async def bridge_sync(request: Request) -> Response:
    st = state(request)
    matched, body = await _bridge(request)
    try:
        sync = BridgeSync.model_validate_json(body)
    except ValidationError as exc:
        raise ValidationFailedError(f"invalid sync: {exc.error_count()} problem(s)") from None
    if sync.bridge != matched:  # a token is one bridge's, never another's
        raise PermissionDeniedError("this token belongs to another bridge")
    commands = await apply_sync(st.db, st.clock, sync, st.settings)
    answer: dict[str, object] = {
        "commands": commands,
        "server_time_ms": int(st.clock.now().timestamp() * 1000),
    }
    if matched in BRIDGES:
        # This server has the fast lane (S87), for the terminals that trade; a bridge before
        # version 8 ignores it. The read-only terminal is never offered it.
        answer["fast"] = OFFER
    return JSONResponse(answer)


@router.post("/bridge/v1/fast", include_in_schema=False)
async def bridge_fast(request: Request) -> Response:
    """The fast lane (S87): prices and order results up; orders down, at once or the moment one
    waits within the request's hold. The two times let the bridge measure its clock against
    the server's and the round trip, whatever the hold was."""
    st = state(request)
    matched, body = await _bridge(request)
    received_ms = int(st.clock.now().timestamp() * 1000)
    try:
        fast = BridgeFast.model_validate_json(body)
    except ValidationError as exc:
        raise ValidationFailedError(f"invalid request: {exc.error_count()} problem(s)") from None
    if fast.bridge != matched:
        raise PermissionDeniedError("this token belongs to another bridge")
    commands = await apply_fast(st.db, st.clock, fast, st.settings)
    replied_ms = int(st.clock.now().timestamp() * 1000)
    return JSONResponse(
        {"commands": commands, "received_ms": received_ms, "replied_ms": replied_ms}
    )
