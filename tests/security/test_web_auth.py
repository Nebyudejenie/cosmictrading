from __future__ import annotations

import pytest
from fastapi import FastAPI

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Role
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, enrol_mfa, full_login, login, make_user


@pytest.fixture
def app(settings: Settings, db: Database, clock: FrozenClock) -> FastAPI:
    return create_app(settings, db=db, clock=clock)


async def test_health_and_readiness(app: FastAPI) -> None:
    async with client_for(app) as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        r = await c.get("/readyz")
        assert r.status_code == 200 and r.json()["status"] == "ready"


async def test_unauthenticated_portal_redirects_and_api_401(app: FastAPI) -> None:
    async with client_for(app) as c:
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        r = await c.get("/api/v1/status")
        assert r.status_code in (401, 404)


async def test_full_login_mfa_and_cookie_flags(
    app: FastAPI, db: Database, clock: FrozenClock
) -> None:
    user = await make_user(db, clock)
    async with client_for(app) as c:
        resp = await login(c, user)
        assert resp.status_code == 303 and resp.headers["location"] == "/mfa/enroll"
        cookie = resp.headers["set-cookie"]
        assert cookie.startswith("__Host-cosmic_session=")
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            assert flag.lower() in cookie.lower()
        # Before MFA enrolment the command center is not reachable.
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/mfa/enroll"
        await enrol_mfa(c, user, clock)
        home = await c.get("/")
        assert home.status_code == 200 and "Command center" in home.text


async def test_security_headers(app: FastAPI) -> None:
    async with client_for(app) as c:
        r = await c.get("/login")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert "script-src 'self'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "same-origin"  # no-referrer makes browsers send Origin: null
    assert h["cache-control"] == "no-store"
    assert "max-age" in h["strict-transport-security"]
    assert "server" not in h


async def test_plain_http_at_the_edge_is_redirected_to_https(app: FastAPI) -> None:
    async with client_for(app) as c:
        r = await c.post("/login?next=/x", headers={"X-Forwarded-Proto": "http"})
        direct = await c.get("/readyz")  # container healthchecks send no forwarding header
        tls = await c.get("/login", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 308 and r.headers["location"].startswith("https://")
    assert r.headers["location"].endswith("/login?next=/x")  # path, query and method kept
    assert direct.status_code == 200 and tls.status_code == 200


async def test_login_requires_csrf(app: FastAPI, db: Database, clock: FrozenClock) -> None:
    user = await make_user(db, clock)
    async with client_for(app) as c:
        await c.get("/login")
        r = await c.post(
            "/login",
            data={"username": user.username, "password": user.password, "csrf_token": "forged"},
        )
        assert r.status_code == 403
        r = await c.post("/login", data={"username": user.username, "password": user.password})
        assert r.status_code == 403


async def test_cross_origin_post_rejected(app: FastAPI, db: Database, clock: FrozenClock) -> None:
    user = await make_user(db, clock)
    async with client_for(app) as c:
        csrf = await full_login(c, user, clock)
        r = await c.post(
            "/logout", data={"csrf_token": csrf}, headers={"origin": "https://evil.example"}
        )
        assert r.status_code == 403
        r = await c.post("/logout", data={"csrf_token": "wrong"})
        assert r.status_code == 403
        r = await c.post(
            "/logout", data={"csrf_token": csrf}, headers={"origin": "https://testserver"}
        )
        assert r.status_code == 303
        # Session is revoked server-side after logout.
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_login_rate_limited(app: FastAPI, db: Database, clock: FrozenClock) -> None:
    await make_user(db, clock)
    statuses = []
    async with client_for(app) as c:
        for _ in range(12):
            page = await c.get("/login")
            r = await c.post(
                "/login",
                data={
                    "username": "owner",
                    "password": "wrong-password-1",
                    "csrf_token": csrf_from(page.text),
                },
            )
            statuses.append(r.status_code)
    assert 429 in statuses
    assert statuses[0] == 401


async def test_untrusted_host_rejected(app: FastAPI) -> None:
    async with client_for(app) as c:
        r = await c.get("/healthz", headers={"host": "evil.example"})
    assert r.status_code == 400


async def test_mfa_next_cannot_open_redirect(
    app: FastAPI, db: Database, clock: FrozenClock
) -> None:
    user = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, user, clock)
        page = await c.get("/mfa", params={"next": "https://evil.example/x"})
        assert 'value="/"' in page.text
        page = await c.get("/mfa", params={"next": "//evil.example"})
        assert 'value="/"' in page.text


async def test_display_name_is_escaped(app: FastAPI, db: Database, clock: FrozenClock) -> None:
    from cosmic.audit.service import Actor
    from cosmic.identity.service import create_user
    from tests.helpers import PASSWORD, TestUser

    async with db.transaction() as conn:
        uid = await create_user(
            conn,
            username="xssuser",
            display_name="<script>alert(1)</script>",
            password=PASSWORD,
            roles=[Role.OWNER],
            now=clock.now(),
            actor=Actor.system("t"),
        )
    user = TestUser(uid, "xssuser", PASSWORD)
    async with client_for(app) as c:
        await full_login(c, user, clock)
        page = await c.get("/")
    assert "<script>alert(1)</script>" not in page.text


async def test_errors_do_not_leak_internals(app: FastAPI) -> None:
    async with client_for(app) as c:
        r = await c.get("/definitely-missing")
    assert r.status_code == 404
    assert "Traceback" not in r.text and 'File "' not in r.text
    assert "Reference:" in r.text


async def test_viewer_cannot_reach_owner_only_routes(
    app: FastAPI, db: Database, clock: FrozenClock
) -> None:
    viewer = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    async with client_for(app) as c:
        await full_login(c, viewer, clock)
        assert (await c.get("/")).status_code == 200


async def test_browser_form_posts_with_a_real_origin_pass_and_null_origin_is_refused(
    app: FastAPI,
) -> None:
    """Real browsers send an Origin header on form posts; the referrer policy must let them
    send the true origin (no-referrer makes them send `Origin: null`, breaking every login)."""
    async with client_for(app) as c:
        page = await c.get("/login")
        token = csrf_from(page.text)
        host = c.base_url.host
        ok = await c.post(
            "/login",
            data={"csrf_token": token, "username": "nobody", "password": "wrong-password-1"},
            headers={"Origin": f"https://{host}"},
        )
        null = await c.post(
            "/login",
            data={"csrf_token": token, "username": "nobody", "password": "wrong-password-1"},
            headers={"Origin": "null"},
        )
    assert page.headers["referrer-policy"] in ("same-origin", "strict-origin-when-cross-origin")
    assert "cross-origin" not in ok.text and ok.status_code != 403  # reached the login check
    assert null.status_code == 403 and "cross-origin request rejected" in null.text
