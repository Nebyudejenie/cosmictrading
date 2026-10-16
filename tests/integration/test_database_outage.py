"""The database is unavailable (directive Phase 11): readiness says so, pages fail without
revealing internals, and the worker neither crashes at start nor exits while it is down."""

from __future__ import annotations

import asyncio

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.ops.health import readiness
from cosmic.web.app import create_app
from cosmic.worker.context import WorkerContext
from cosmic.worker.main import Worker
from cosmic.worker.registry import Registry
from tests.helpers import client_for

LEAKS = ("Traceback", "asyncpg", "Connect call failed", "127.0.0.1:1", "password")


def _down(settings: Settings) -> Settings:
    return settings.model_copy(update={"db_port": 1})  # nothing listens there


async def test_readiness_and_pages_fail_closed_and_quietly(
    settings: Settings, clock: FrozenClock
) -> None:
    db = Database.from_settings(_down(settings))
    try:
        assert await readiness(db) == (False, {"database": False, "schema": False})
        app = create_app(_down(settings), db=db, clock=clock)
        async with client_for(app, raise_app_exceptions=False) as c:
            ready = await c.get("/readyz")
            assert ready.status_code == 503
            assert ready.json() == {
                "status": "not_ready",
                "checks": {"database": False, "schema": False},
            }
            c.cookies.set(settings.cookie_name, "a-session-token")  # forces a session lookup
            page = await c.get("/")
            assert page.status_code == 500
            for leak in LEAKS:
                assert leak not in page.text, leak
    finally:
        await db.dispose()


async def test_the_worker_keeps_running_while_the_database_is_down(
    settings: Settings, clock: FrozenClock
) -> None:
    db = Database.from_settings(_down(settings))
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="t")
    worker = Worker(ctx, Registry())
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(1)
        assert not task.done()  # neither the start nor the loop let the outage end the process
    finally:
        worker.stopping.set()
        await asyncio.wait_for(task, timeout=15)  # stops cleanly once asked
        await db.dispose()
