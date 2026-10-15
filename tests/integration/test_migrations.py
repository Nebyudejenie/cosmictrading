from __future__ import annotations

import asyncio
import secrets
import uuid

import asyncpg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
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


async def _superuser(settings: Settings, dbname: str) -> asyncpg.Connection:
    assert settings.db_superuser_password
    return await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_superuser,
        password=settings.db_superuser_password.get_secret_value(),
        database=dbname,
    )


SANDBOX_ROW = (
    "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, status, "
    "reconciliation_status, created_at, updated_at) VALUES ($1, $2, 'SANDBOX', $3, $4, 'USDT', "
    "'ACTIVE', 'UNKNOWN', now(), now())"
)


async def test_sandbox_accounts_are_constrained_and_block_downgrade(settings: Settings) -> None:
    """0009 (ADR-026): a SANDBOX account must use the test-network adapter, only one may be
    active per adapter, and the downgrade refuses while one exists (nothing is deleted)."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue = uuid.uuid4()
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'BINANCE_TESTNET', 'test network', 'SIMULATED', 'UTC', '24x7', now())",
                venue,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(SANDBOX_ROW, uuid.uuid4(), "sbx-paper", venue, "paper")
            await conn.execute(SANDBOX_ROW, uuid.uuid4(), "sbx-one", venue, "binance_spot_testnet")
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    SANDBOX_ROW, uuid.uuid4(), "sbx-two", venue, "binance_spot_testnet"
                )
        finally:
            await conn.close()
        with pytest.raises(Exception, match="SANDBOX accounts exist"):
            await asyncio.to_thread(downgrade, settings, "0008_strategy_library", name)
        assert "broker_contract_runs" in await _tables(settings, name)
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


# Expression (full-text) indexes from migration 0007: Alembic cannot compare expression
# indexes reliably, so they are checked by name below and by query plan in test_search.py.
EXPRESSION_INDEXES = frozenset({"claims_fts", "artifacts_report_fts", "trade_intents_fts"})


def _ignorable(diff: object) -> bool:
    if not isinstance(diff, tuple):
        return False
    # alembic_version is managed by Alembic itself.
    if diff[0] == "remove_table" and getattr(diff[1], "name", "") == "alembic_version":
        return True
    return diff[0] == "remove_index" and getattr(diff[1], "name", "") in EXPRESSION_INDEXES


async def test_expression_indexes_exist(settings: Settings, _database: str) -> None:
    engine = create_async_engine(settings.owner_dsn(_database))
    try:
        async with engine.connect() as conn:
            found = {
                r[0]
                for r in await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
                )
            }
    finally:
        await engine.dispose()
    assert found >= EXPRESSION_INDEXES


def test_head_revision_known() -> None:
    assert head_revision()
