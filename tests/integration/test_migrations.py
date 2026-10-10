from __future__ import annotations

import asyncio
import secrets

import asyncpg
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from cosmic.core.config import Settings
from cosmic.db import bootstrap
from cosmic.db.all_tables import metadata
from cosmic.db.migrate import downgrade, head_revision, upgrade


async def _tables(settings: Settings, dbname: str) -> set[str]:
    assert settings.db_superuser_password
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_superuser,
        password=settings.db_superuser_password.get_secret_value(),
        database=dbname,
    )
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        return {r["tablename"] for r in rows}
    finally:
        await conn.close()


async def test_fresh_upgrade_downgrade_upgrade(settings: Settings) -> None:
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        assert {"users", "audit_events", "jobs"} <= await _tables(settings, name)
        await asyncio.to_thread(downgrade, settings, "base", name)
        assert await _tables(settings, name) == {"alembic_version"}
        await asyncio.to_thread(upgrade, settings, "head", name)
        assert "audit_events" in await _tables(settings, name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_metadata_matches_migrated_schema(settings: Settings, _database: str) -> None:
    engine = create_async_engine(settings.owner_dsn(_database))

    def diff(sync_conn: Connection) -> list[object]:
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return [d for d in compare_metadata(ctx, metadata) if not _ignorable(d)]

    try:
        async with engine.connect() as conn:
            differences = await conn.run_sync(diff)
    finally:
        await engine.dispose()
    assert differences == [], differences


def _ignorable(diff: object) -> bool:
    # alembic_version is managed by Alembic itself.
    return (
        isinstance(diff, tuple)
        and diff[0] == "remove_table"
        and getattr(diff[1], "name", "") == "alembic_version"
    )


def test_head_revision_known() -> None:
    assert head_revision()
