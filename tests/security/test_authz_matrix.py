"""Authorization matrix (SEC-02, TST-04): every GET route × {anonymous, viewer, analyst}.

Adding a route without classifying it here fails the test, so access is always decided
deliberately.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.routing import APIRoute

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Role
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user

U = str(uuid.UUID(int=1))
PUBLIC = {"/healthz": 200, "/readyz": 200, "/login": 200, "/internal/metrics": 404}
# path → minimum role that may read it: V(iewer), A(nalyst), O(wner); "session" = any signed-in user
ACCESS = {
    "/": "V",
    "/sources": "V",
    "/market": "V",
    "/market/{canonical_key:path}": "V",
    "/research": "V",
    "/research/{mission_id}": "V",
    "/strategies": "V",
    "/strategies/library": "V",
    "/strategies/library/{key}": "V",
    "/golive": "V",
    "/ops": "V",
    "/ai": "V",
    "/settings/telegram": "V",
    "/approvals": "A",
    "/approvals/{intent_id}": "A",
    "/proposals/new": "A",
    "/portfolio": "A",
    "/trader": "A",  # account results: READ_ACCOUNT, like the portfolio
    "/trader/setup": "A",
    "/trader/competition": "A",
    "/gold": "A",
    "/rehearsal": "A",
    "/orders": "A",
    "/orders/{order_id}": "A",
    "/audit": "A",
    "/settings/risk": "A",
    "/alerts": "V",
    "/search": "V",
    "/mfa": "session",
    "/mfa/enroll": "session",
    "/api/v1/status": "V",
    "/api/v1/sources": "V",
    "/api/v1/instruments": "V",
    "/api/v1/intents": "A",
    "/api/v1/intents/{intent_id}": "A",
    "/api/v1/orders": "A",
    "/api/v1/orders/{order_id}": "A",
    "/api/v1/accounts/{alias}": "A",
    "/api/v1/market/{canonical_key:path}/bars": "V",
    "/api/v1/golive/readiness": "V",
    "/api/v1/ai/providers": "V",
    "/api/v1/missions": "V",
    "/api/v1/missions/{mission_id}": "V",
    "/api/v1/audit": "A",
    "/api/v1/search": "V",
}
ORDER = {"V": 0, "A": 1, "O": 2}


def flatten(routes: list[object]) -> list[object]:
    out: list[object] = []
    for r in routes:
        inner = getattr(r, "original_router", None)
        out += flatten(list(inner.routes)) if inner is not None else [r]
    return out


def sample(path: str) -> str:
    return (
        path.replace("{canonical_key:path}", "SPOT:BTC/USD@COINBASE")
        .replace("{alias}", "paper-main")
        .replace("{intent_id}", U)
        .replace("{order_id}", U)
        .replace("{mission_id}", U)
    )


async def test_every_get_route_is_classified_and_enforced(settings: Settings, db: Database) -> None:
    clock = FrozenClock(datetime(2026, 10, 10, 21, 6, tzinfo=UTC))
    app = create_app(settings, db=db, clock=clock)
    gets = sorted(
        {
            r.path
            for r in flatten(list(app.routes))
            if isinstance(r, APIRoute)
            and "GET" in r.methods
            and not r.path.startswith(("/api/docs", "/api/openapi", "/docs"))
        }
    )
    unclassified = [p for p in gets if p not in ACCESS and p not in PUBLIC]
    assert not unclassified, f"classify these routes in ACCESS: {unclassified}"
    viewer = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    analyst = await make_user(db, clock, username="analyst", roles=[Role.ANALYST])
    async with client_for(app) as anon:
        for path in gets:
            r = await anon.get(sample(path))
            if path in PUBLIC:
                assert r.status_code == PUBLIC[path], path
            elif path.startswith("/api/"):
                assert r.status_code == 401, path
            else:
                assert r.status_code == 303 and r.headers["location"] == "/login", path
    for user, level in ((viewer, "V"), (analyst, "A")):
        async with client_for(app) as c:
            await full_login(c, user, clock)
            for path, need in ACCESS.items():
                r = await c.get(sample(path))
                if need == "session":
                    assert r.status_code in (200, 303), (user.username, path, r.status_code)
                elif ORDER[level] >= ORDER[need]:
                    assert r.status_code in (200, 404), (user.username, path, r.status_code)
                else:
                    assert r.status_code == 403, (user.username, path, r.status_code)


async def test_privileged_posts_refused_for_viewer(settings: Settings, db: Database) -> None:
    clock = FrozenClock(datetime(2026, 10, 10, 21, 6, tzinfo=UTC))
    app = create_app(settings, db=db, clock=clock)
    viewer = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    async with client_for(app) as c:
        await full_login(c, viewer, clock)
        token = csrf_from((await c.get("/")).text)
        for path in (
            f"/approvals/{U}/review",
            f"/approvals/{U}/decide",
            "/ops/emergency-stop",
            "/ops/mode",
            "/ops/resume",
            "/ops/paper-account",
            "/ops/sandbox-account",
            "/settings/risk",
            "/ai/groq/attest",
            "/ai/groq/models",
            "/strategies/library/turtle_55_20/switch",
            "/portfolio/paper-main/exit",
            "/proposals",
            f"/orders/{U}/cancel",
            "/portfolio/paper-main/reconcile",
            "/rehearsal/start",
            "/golive/live-account",
            "/golive/activate",
            "/golive/revoke",
            "/golive/auto-trader",
            "/golive/auto-trader/stop",
        ):
            r = await c.post(
                path,
                data={"csrf_token": token, "mode": "PAPER_TRADING", "reason": "viewer attempt"},
            )
            assert r.status_code == 403, (path, r.status_code)
