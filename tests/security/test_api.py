from __future__ import annotations

from datetime import UTC, datetime

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Role
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user

NOW = datetime(2026, 10, 10, 21, 6, tzinfo=UTC)


async def test_api_auth_csrf_and_roles(settings: Settings, db: Database) -> None:
    clock = FrozenClock(NOW)
    app = create_app(settings, db=db, clock=clock)
    async with client_for(app) as anon:
        r = await anon.get("/api/v1/status")
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        r = await c.get("/api/v1/status")
        assert r.status_code == 200 and r.json()["authority"]["mode"] == "RESEARCH_ONLY"
        assert r.json()["authority"]["live_enabled"] is False
        # Unsafe method without CSRF header → 403
        r = await c.post("/api/v1/authority/emergency-stop", json={"reason": "x"})
        assert r.status_code == 403
        token = csrf_from((await c.get("/")).text)
        r = await c.post(
            "/api/v1/authority/emergency-stop",
            json={"reason": "api test"},
            headers={"X-CSRF-Token": token},
        )
        assert r.status_code == 202 and r.json()["mode"] == "EMERGENCY_STOP"
        assert (await c.get("/api/v1/intents/not-a-uuid")).status_code == 404
    viewer = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    async with client_for(app) as v:
        await full_login(v, viewer, clock)
        assert (await v.get("/api/v1/intents")).status_code == 403
        assert (await v.get("/api/v1/status")).status_code == 200
        assert (await v.get("/api/v1/sources")).status_code == 200
