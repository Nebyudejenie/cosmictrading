"""AI API connections (S81): keys typed by the owner in the portal, encrypted at rest, masked,
used only by the worker and only with their own provider; and the connection test that proves a
real inference with a structured answer, recording each failure mode."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from cosmic.ai import connection_test, keystore
from cosmic.ai.gateway import AIGateway
from cosmic.audit.service import Actor
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events
from cosmic.db.schema_ai import ai_provider_keys, ai_provider_tests
from cosmic.identity.capabilities import Role
from cosmic.web.app import create_app
from tests.ai_fake import FakeAI
from tests.helpers import client_for, csrf_from, full_login, make_user
from tests.integration.test_ai_gateway import setup_routes, task

PORTAL_KEY = "gsk_portal_" + "p" * 30 + "WXYZ"
ANSWER = {
    "direction": "UP",
    "recommendation": "BUY",
    "confidence": "MEDIUM",
    "why": "The hourly and lower timeframes trend up with momentum above 50.",
    "conditions": "London and New York open, normal spread",
    "expected_risk": "A fall below 4,052 support would end the trend.",
    "timing": "Suitable now: both sessions open.",
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    keystore._CACHE.clear()
    yield
    keystore._CACHE.clear()


async def test_a_portal_key_is_encrypted_masked_bound_to_its_provider_and_revocable(
    settings: Settings, db: Database
) -> None:
    clock = FrozenClock(datetime(2026, 11, 11, 9, 0, tzinfo=UTC))
    actor = Actor.system("test")
    async with db.transaction() as conn:
        mask = await keystore.save(conn, settings, "groq", PORTAL_KEY, actor=actor, now=clock.now())
    assert mask == "••••WXYZ"
    async with db.connect() as conn:
        (row,) = list(await conn.execute(select(ai_provider_keys)))
        audit = [
            r.payload
            for r in await conn.execute(
                select(audit_events.c.payload).where(audit_events.c.action == "ai.key_saved")
            )
        ]
    assert PORTAL_KEY.encode() not in bytes(row.ciphertext) and row.last4 == "WXYZ"
    assert audit == [{"provider": "groq", "key": "••••WXYZ"}]  # never the key itself
    async with db.connect() as conn:
        assert await keystore.refresh(conn, settings) == {"groq": PORTAL_KEY}
    assert keystore.cached("groq") == PORTAL_KEY
    assert scrub_text(f"Bearer {PORTAL_KEY}") == "Bearer [REDACTED]"  # never in a log line
    # Bound to its provider: moved to another provider, it cannot be decrypted, so never sent.
    async with db.transaction() as conn:
        await conn.execute(update(ai_provider_keys).values(provider_key="openrouter"))
    async with db.connect() as conn:
        assert await keystore.refresh(conn, settings) == {}
    async with db.transaction() as conn:
        await conn.execute(update(ai_provider_keys).values(provider_key="groq"))
        assert await keystore.revoke(conn, "groq", actor=actor, now=clock.now())
    async with db.connect() as conn:
        assert await keystore.refresh(conn, settings) == {}
    assert keystore.cached("groq") is None


async def test_the_gateway_uses_the_portal_key_and_never_another_providers(
    settings: Settings, db: Database
) -> None:
    clock = FrozenClock(datetime(2026, 11, 11, 9, 0, tzinfo=UTC))
    fake = FakeAI()
    await setup_routes(db, settings, clock)  # groq attested, a model approved; no file key
    async with db.transaction() as conn:
        await keystore.save(
            conn, settings, "groq", PORTAL_KEY, actor=Actor.system("t"), now=clock.now()
        )
    gw = AIGateway(db, clock, settings, transport=fake.transport(), retry_base_seconds=0)
    try:
        result = await gw.run(task())
    finally:
        await gw.close()
    assert result.status == "OK", result.reasons
    assert [c["provider"] for c in fake.calls] == ["groq"]
    assert fake.calls[0]["auth"] == f"Bearer {PORTAL_KEY}"


def _transport(fake: FakeAI, mode: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-big"}]})
        if mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if mode == "auth":
            return httpx.Response(401, text='{"error": "invalid api key"}')
        if mode == "limited":
            return httpx.Response(429, headers={"retry-after": "30"}, text="{}")
        content = json.dumps(ANSWER) if mode == "ok" else '{"thesis": "not the form"}'
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 120},
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("mode", "flags", "said"),
    [
        ("ok", (True, True, True, True), "BUY (MEDIUM)"),
        ("invalid", (True, True, True, False), "not in the required form"),
        ("auth", (False, True, False, False), "authentication refused (HTTP 401)"),
        ("timeout", (False, True, False, False), "provider unavailable: timeout"),
        ("limited", (True, True, False, False), "rate limited by the provider"),
    ],
)
async def test_the_connection_test_proves_a_real_structured_inference_or_says_what_failed(
    settings: Settings,
    db: Database,
    mode: str,
    flags: tuple[bool, bool, bool, bool],
    said: str,
) -> None:
    clock = FrozenClock(datetime(2026, 11, 11, 9, 0, tzinfo=UTC))
    await setup_routes(db, settings, clock)
    async with db.transaction() as conn:
        await keystore.save(
            conn, settings, "groq", PORTAL_KEY, actor=Actor.system("t"), now=clock.now()
        )
    result = await connection_test.run(
        db, settings, "groq", clock.now(), transport=_transport(FakeAI(), mode)
    )
    got = (result["auth_ok"], result["model_ok"], result["inference_ok"], result["schema_ok"])
    assert got == flags, result
    assert said in result["detail"] and result["key_source"] == "PORTAL"
    assert PORTAL_KEY not in result["detail"]
    async with db.connect() as conn:
        (row,) = list(await conn.execute(select(ai_provider_tests)))
    assert row.schema_ok is flags[3] and row.model_id == "llama-big"
    if mode == "ok":
        assert row.answer["recommendation"] == "BUY" and row.prompt_tokens == 900


async def test_the_owner_adds_tests_and_revokes_a_key_in_the_portal_never_seeing_it_again(
    settings: Settings, db: Database
) -> None:
    clock = FrozenClock(datetime(2026, 11, 11, 9, 0, tzinfo=UTC))
    owner = await make_user(db, clock)
    app = create_app(settings, db=db, clock=clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = await c.get("/ai/connections")
        assert page.status_code == 200 and "AI API connections" in page.text
        r = await c.post(
            "/ai/connections/groq/key",
            data={"csrf_token": csrf_from(page.text), "api_key": PORTAL_KEY},
        )
        assert r.status_code == 303 and r.headers["location"] == "/ai/connections?saved=groq"
        page = await c.get("/ai/connections?saved=groq")
        assert "••••WXYZ" in page.text and PORTAL_KEY not in page.text
        assert PORTAL_KEY[:20] not in page.text
        bad = await c.post(
            "/ai/connections/groq/key",
            data={"csrf_token": csrf_from(page.text), "api_key": "short"},
        )
        assert bad.status_code == 422 and "8 to 512 printable characters" in bad.text
        assert PORTAL_KEY not in bad.text
        r = await c.post("/ai/connections/groq/test", data={"csrf_token": csrf_from(page.text)})
        assert r.status_code == 303
        r = await c.post("/ai/connections/groq/revoke", data={"csrf_token": csrf_from(page.text)})
        assert r.status_code == 303
        unknown = await c.post(
            "/ai/connections/acme/key",
            data={"csrf_token": csrf_from(page.text), "api_key": "x" * 9},
        )
        assert unknown.status_code == 404
    async with db.connect() as conn:
        assert await keystore.active(conn) == {}
    viewer = await make_user(db, clock, username="viewer1", roles=[Role.VIEWER])
    async with client_for(create_app(settings, db=db, clock=clock)) as c:
        await full_login(c, viewer, clock)
        assert (await c.get("/ai/connections")).status_code == 403
