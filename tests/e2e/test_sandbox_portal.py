"""Operations → Binance test network: the card, and the form that queues the worker job."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import jobs
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user

CAPTURE = datetime(2026, 10, 10, 21, 6, 0, tzinfo=UTC)


async def test_owner_queues_a_test_network_account_for_the_worker(
    settings: Settings, db: Database
) -> None:
    clock = FrozenClock(CAPTURE)
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = (await c.get("/ops")).text
        assert "Binance test network" in page and "never run" in page
        assert 'action="/ops/sandbox-account"' in page
        token = csrf_from(page)
        bad = await c.post(
            "/ops/sandbox-account",
            data={"csrf_token": token, "alias": "Bad Alias!", "capital": "100"},
        )
        assert bad.status_code == 422
        r = await c.post(
            "/ops/sandbox-account",
            data={"csrf_token": token, "alias": "binance-test", "capital": "250.5"},
        )
        assert r.status_code == 303, r.text
    async with db.connect() as conn:
        rows = list(
            await conn.execute(
                select(jobs.c.payload).where(jobs.c.kind == "trading.open_sandbox_account")
            )
        )
    assert [r.payload["alias"] for r in rows] == ["binance-test"]
    assert rows[0].payload["managed_capital"] == "250.5"
    assert rows[0].payload["user_id"] == str(owner.user_id)
