"""JSON API read models and actions that previously existed only in the portal (doc 10)."""

from __future__ import annotations

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Role
from cosmic.web.app import create_app
from tests.e2e.test_trading_portal import _setup
from tests.helpers import client_for, csrf_from, full_login, make_user

BTC = "SPOT:BTC/USD@COINBASE"


async def test_read_models(settings: Settings, db: Database) -> None:
    clock, ctx = await _setup(settings, db)
    from cosmic.market.jobs import build_ingest_service
    from tests.market_mock import MarketMock

    await build_ingest_service(ctx, transport=MarketMock().transport()).ingest_bars(
        "coinbase_exchange_public", "1h"
    )
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        r = await c.get(f"/api/v1/market/{BTC}/bars?timeframe=1h&limit=24")
        body = r.json()
        assert r.status_code == 200 and body["label"] == "DELAYED" and len(body["bars"]) == 24
        assert all(b["is_complete"] for b in body["bars"])  # the open bar is never served
        assert body["bars"][0]["open_time"] < body["bars"][-1]["open_time"]
        assert (await c.get(f"/api/v1/market/{BTC}/bars?timeframe=7m")).status_code == 422
        assert (await c.get("/api/v1/market/SPOT:NOPE/USD@COINBASE/bars")).status_code == 404

        g = (await c.get("/api/v1/golive/readiness")).json()
        assert g["live_trading"] == "DISABLED" and [x["key"] for x in g["gates"]] == list(
            "ABCDEFGHI"
        )

        ai = await c.get("/api/v1/ai/providers")
        assert ai.status_code == 200 and ai.json()["policy"] == "FREE_CLOUD_AI_ONLY"
        assert all(p["key_configured"] is False for p in ai.json()["providers"])
        assert "api_key" not in ai.text and "Bearer" not in ai.text

        m = (await c.get("/api/v1/missions")).json()
        assert m["missions"] == [] and isinstance(m["task_board"], dict)
        assert (await c.get("/api/v1/missions/not-a-uuid")).status_code == 404

        audit = (await c.get("/api/v1/audit?limit=5")).json()
        assert 0 < len(audit) <= 5 and audit[0]["seq"] > audit[-1]["seq"]  # newest first
        mine = (await c.get(f"/api/v1/audit?entity_id={owner.user_id}")).json()
        assert mine and all(e["entity_id"] == str(owner.user_id) for e in mine)


async def test_mode_change_and_revoke_via_api(settings: Settings, db: Database) -> None:
    clock, _ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        token = csrf_from((await c.get("/")).text)
        h = {"X-CSRF-Token": token}
        body = {"mode": "PAPER_TRADING", "reason": "api paper trading test"}
        assert (await c.post("/api/v1/authority/mode", json=body)).status_code == 403  # no CSRF
        r = await c.post("/api/v1/authority/mode", json=body, headers=h)
        assert r.status_code == 200 and r.json()["authority"]["mode"] == "PAPER_TRADING"
        bad = await c.post("/api/v1/authority/mode", json={"mode": "LIVE"}, headers=h)
        assert bad.status_code == 422
        clock.advance(minutes=11)  # step-up MFA is no longer fresh
        safer = await c.post(
            "/api/v1/authority/mode",
            json={"mode": "RESEARCH_ONLY", "reason": "restricting needs no fresh code"},
            headers=h,
        )
        assert safer.status_code == 200  # reducing authority is never made harder
        riskier = await c.post("/api/v1/authority/mode", json=body, headers=h)
        assert riskier.status_code == 403  # enabling paper trading needs a fresh code
        unknown = "00000000-0000-7000-8000-000000000001"
        r = await c.post(f"/api/v1/approvals/{unknown}/revoke", json={}, headers=h)
        assert r.status_code in (403, 404)  # stale MFA or unknown approval: never 2xx
    viewer = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    async with client_for(app) as v:
        await full_login(v, viewer, clock)
        vt = {"X-CSRF-Token": csrf_from((await v.get("/")).text)}
        r = await v.post("/api/v1/authority/mode", json=body, headers=vt)
        assert r.status_code == 403
        r = await v.post(f"/api/v1/approvals/{unknown}/revoke", json={}, headers=vt)
        assert r.status_code == 403
        assert (await v.get("/api/v1/audit")).status_code == 403
        assert (await v.get("/api/v1/golive/readiness")).status_code == 200
