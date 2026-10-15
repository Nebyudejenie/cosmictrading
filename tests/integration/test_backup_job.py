"""The worker's backup check: incidents open when backups are late and close by themselves;
the heartbeat carries the report the Operations page shows (ADR-019)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, worker_heartbeats
from cosmic.web.app import create_app
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from cosmic.worker.main import Worker
from tests.backup_files import make_set, record_drill
from tests.helpers import client_for, full_login, make_user


async def _open(db: Database) -> dict[str, str]:
    async with db.connect() as conn:
        rows = await conn.execute(
            select(incidents.c.dedup_key, incidents.c.severity).where(
                incidents.c.status != "RESOLVED"
            )
        )
        return {r.dedup_key: r.severity for r in rows}


async def test_late_backups_open_incidents_that_close_by_themselves(
    settings: Settings, db: Database, clock: FrozenClock, tmp_path: Path
) -> None:
    ctx = WorkerContext(
        settings=settings.model_copy(update={"backup_check_dir": tmp_path}),
        db=db,
        clock=clock,
        worker_id="t",
    )
    registry = build_registry(ctx)
    check = registry.handlers["ops.backup_check"]
    now = clock.now()
    make_set(tmp_path, now - timedelta(hours=30))
    await check(ctx, {})
    assert await _open(db) == {"ops.backup_overdue": "WARNING", "ops.restore_drill_due": "WARNING"}
    fresh = make_set(tmp_path, now - timedelta(hours=1))
    record_drill(tmp_path, fresh, now - timedelta(minutes=5))
    await check(ctx, {})
    assert await _open(db) == {}
    await Worker(ctx, registry).heartbeat()
    async with db.connect() as conn:
        info = await conn.scalar(select(worker_heartbeats.c.info))
    assert info["backups"]["newest"]["stem"] == fresh and info["backups"]["drill"]["passed"]

    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = (await c.get("/ops")).text
    assert fresh in page and "Restore drill" in page and "10 checks, workstation" in page


async def test_without_a_backup_directory_nothing_is_checked(
    settings: Settings, db: Database, clock: FrozenClock
) -> None:
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="t")
    registry = build_registry(ctx)
    await registry.handlers["ops.backup_check"](ctx, {})
    assert await _open(db) == {}
    await Worker(ctx, registry).heartbeat()
    async with db.connect() as conn:
        info = await conn.scalar(select(worker_heartbeats.c.info))
    assert "backups" not in info
