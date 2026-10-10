"""Shared test helpers."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import httpx
from fastapi import FastAPI

from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.db.engine import Database
from cosmic.identity import mfa
from cosmic.identity.capabilities import Role
from cosmic.identity.service import Principal, create_user

PASSWORD = "correct-horse-battery-9!"


@dataclass
class TestUser:
    user_id: uuid.UUID
    username: str
    password: str
    mfa_secret: str | None = None


async def make_user(
    db: Database, clock: FrozenClock, username: str = "owner", roles: list[Role] | None = None
) -> TestUser:
    async with db.transaction() as conn:
        user_id = await create_user(
            conn,
            username=username,
            display_name=username.title(),
            password=PASSWORD,
            roles=roles or [Role.OWNER],
            now=clock.now(),
            actor=Actor.system("test"),
        )
    return TestUser(user_id, username, PASSWORD)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token not found in page"
    return match.group(1)


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        follow_redirects=False,
    )


async def login(client: httpx.AsyncClient, user: TestUser) -> httpx.Response:
    page = await client.get("/login")
    token = csrf_from(page.text)
    return await client.post(
        "/login", data={"username": user.username, "password": user.password, "csrf_token": token}
    )


async def enrol_mfa(client: httpx.AsyncClient, user: TestUser, clock: FrozenClock) -> None:
    page = await client.get("/mfa/enroll")
    assert page.status_code == 200, page.text
    secret = re.search(r"<code>([A-Z2-7 ]{20,})</code>", page.text)
    assert secret
    user.mfa_secret = secret.group(1).replace(" ", "")
    resp = await client.post(
        "/mfa/enroll",
        data={
            "code": mfa.code_at(user.mfa_secret, clock.now()),
            "csrf_token": csrf_from(page.text),
        },
    )
    assert resp.status_code == 303, resp.text


async def verify_mfa(
    client: httpx.AsyncClient, user: TestUser, clock: FrozenClock
) -> httpx.Response:
    assert user.mfa_secret
    page = await client.get("/mfa")
    clock.advance(seconds=30)  # new TOTP step (replay protection rejects reused steps)
    return await client.post(
        "/mfa",
        data={
            "code": mfa.code_at(user.mfa_secret, clock.now()),
            "csrf_token": csrf_from(page.text),
            "next": "/",
        },
    )


async def full_login(client: httpx.AsyncClient, user: TestUser, clock: FrozenClock) -> str:
    """Sign in, enrol MFA if needed, verify; returns the session CSRF token."""
    resp = await login(client, user)
    assert resp.status_code == 303, resp.text
    if resp.headers["location"] == "/mfa/enroll":
        await enrol_mfa(client, user, clock)
    else:
        r = await verify_mfa(client, user, clock)
        assert r.status_code == 303, r.text
    home = await client.get("/")
    assert home.status_code == 200, home.text
    return csrf_from(home.text)


def principal_for(
    user: TestUser,
    clock: FrozenClock,
    *,
    roles: tuple[str, ...] = ("OWNER",),
    mfa_fresh: bool = True,
) -> Principal:
    """A Principal as the web layer would build it (for service-level tests)."""
    from cosmic.identity.capabilities import capabilities_for_roles
    from cosmic.identity.service import Principal

    return Principal(
        user_id=user.user_id,
        username=user.username,
        display_name=user.username,
        roles=roles,
        capabilities=capabilities_for_roles(roles),
        session_id=None,
        csrf_token=None,
        mfa_enabled=True,
        mfa_verified_at=clock.now() if mfa_fresh else None,
    )
