"""Portal alerts inbox: alerts are visible without Telegram, unread first, markable as read."""

from __future__ import annotations

import re

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Role
from cosmic.web.app import create_app
from tests.e2e.test_trading_portal import _setup
from tests.helpers import client_for, csrf_from, full_login, make_user


async def test_alert_inbox(settings: Settings, db: Database) -> None:
    clock, _ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        assert "unread alert" not in (await c.get("/")).text
        token = csrf_from((await c.get("/ops")).text)
        r = await c.post(
            "/ops/emergency-stop", data={"csrf_token": token, "reason": "inbox test stop"}
        )
        assert r.status_code == 303
        home = (await c.get("/")).text
        assert "1 unread alert(s)" in home
        page = (await c.get("/alerts")).text
        assert "Emergency stop activated" in page and "UNREAD" in page
        for label in (
            "What happened",
            "Verified",
            "Uncertain",
            "Risk now",
            "Already done",
            "What you should do",
        ):
            assert f"<dt>{label}</dt>" in page, label
        alert_id = re.search(r'name="alert_id" value="([0-9a-f-]{36})"', page)
        assert alert_id is not None
        token = csrf_from(page)
        r = await c.post("/alerts/read", data={"csrf_token": token, "alert_id": alert_id[1]})
        assert r.status_code == 303
        page = (await c.get("/alerts")).text
        assert "UNREAD" not in page and "Emergency stop activated" in page  # kept, now read
        assert "unread alert" not in (await c.get("/")).text
        r = await c.post("/alerts/read", data={"alert_id": alert_id[1]})  # no CSRF token
        assert r.status_code == 403
    viewer = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    async with client_for(app) as v:
        await full_login(v, viewer, clock)
        assert "No alerts." in (await v.get("/alerts")).text  # alerts go to owners only
