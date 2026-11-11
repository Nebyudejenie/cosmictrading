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


async def test_the_total_loss_stop_is_kept_by_the_database_and_blocks_downgrade(
    settings: Settings,
) -> None:
    """0013 (ADR-030 addendum 3): a mandate may carry a total loss limit and end STOPPED; the
    limit is immutable like its other terms, and the downgrade refuses while either is used."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue, account, user, mandate = (uuid.uuid4() for _ in range(4))
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'PAPERV', 'paper', 'SIMULATED', 'UTC', '24x7', now())",
                venue,
            )
            await conn.execute(
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at) VALUES ($1, 'paper-x', "
                "'PAPER', $2, 'paper', 'USD', 'ACTIVE', 'UNKNOWN', now(), now())",
                account,
                venue,
            )
            await conn.execute(
                "INSERT INTO users (id, username, display_name, password_hash, roles, "
                "password_changed_at, created_at, updated_at) VALUES ($1, 'owner', 'o', 'x', "
                "ARRAY['OWNER'], now(), now(), now())",
                user,
            )
            await conn.execute(
                "INSERT INTO trading_mandates (id, account_id, strategy_key, timeframe, "
                "instruments, max_loss_per_trade, max_trades_per_day, max_open_positions, "
                "daily_loss_limit, status, granted_by, granted_at, expires_at, total_loss_limit) "
                "VALUES ($1, $2, 'turtle_20_10', '1h', ARRAY['X'], 30, 5, 2, 90, 'ACTIVE', $3, "
                "now(), now() + interval '1 day', 300)",
                mandate,
                account,
                user,
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="immutable"):
                await conn.execute(
                    "UPDATE trading_mandates SET total_loss_limit = 3000 WHERE id = $1", mandate
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE trading_mandates SET status = 'GONE', ended_at = now() WHERE id = $1",
                    mandate,
                )
            await conn.execute(
                "UPDATE trading_mandates SET status = 'STOPPED', ended_at = now(), "
                "ended_by = 'system:mandate', end_reason = 'total loss limit' WHERE id = $1",
                mandate,
            )
        finally:
            await conn.close()
        with pytest.raises(Exception, match="use the total loss stop"):
            await asyncio.to_thread(downgrade, settings, "0012_mandate_money", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_daily_mandates_are_allowed_and_block_downgrade(settings: Settings) -> None:
    """0015 (ADR-030 addendum 5): a mandate may run on daily bars ('1d') but on no other
    timeframe, and the downgrade refuses while a daily mandate exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue, account, user = (uuid.uuid4() for _ in range(3))
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'PAPERV', 'paper', 'SIMULATED', 'UTC', '24x7', now())",
                venue,
            )
            await conn.execute(
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at) VALUES ($1, 'paper-x', "
                "'PAPER', $2, 'paper', 'USD', 'ACTIVE', 'UNKNOWN', now(), now())",
                account,
                venue,
            )
            await conn.execute(
                "INSERT INTO users (id, username, display_name, password_hash, roles, "
                "password_changed_at, created_at, updated_at) VALUES ($1, 'owner', 'o', 'x', "
                "ARRAY['OWNER'], now(), now(), now())",
                user,
            )
            insert = (
                "INSERT INTO trading_mandates (id, account_id, strategy_key, timeframe, "
                "instruments, max_loss_per_trade, max_trades_per_day, max_open_positions, "
                "daily_loss_limit, status, granted_by, granted_at, expires_at) "
                "VALUES ($1, $2, 'ema_200_trend', $3, ARRAY['X'], 2, 5, 2, 6, 'ACTIVE', $4, "
                "now(), now() + interval '90 days')"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, uuid.uuid4(), account, "1w", user)
            await conn.execute(insert, uuid.uuid4(), account, "1d", user)
        finally:
            await conn.close()
        with pytest.raises(Exception, match="daily mandates exist"):
            await asyncio.to_thread(downgrade, settings, "0014_auto_competitions", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_competition_runs_are_append_only_and_block_downgrade(settings: Settings) -> None:
    """0014 (ADR-031): a competition run can be added, never changed or deleted, and the
    downgrade refuses while one exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            run = uuid.uuid4()
            await conn.execute(
                "INSERT INTO auto_competitions (id, created_at, code_version, forward_from, "
                "data_until, trials, result, result_hash) VALUES ($1, now(), 'v', now(), now(), "
                "1, '{}'::jsonb, 'h')",
                run,
            )
            for sql in (
                "UPDATE auto_competitions SET trials = 2 WHERE id = $1",
                "DELETE FROM auto_competitions WHERE id = $1",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
                    await conn.execute(sql, run)
        finally:
            await conn.close()
        with pytest.raises(Exception, match="competition runs exist"):
            await asyncio.to_thread(downgrade, settings, "0013_mandate_total_stop", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_live_locks_hold_in_the_database_and_block_downgrade(settings: Settings) -> None:
    """0016 (ADR-034): a LIVE account only with the live Binance adapter, activations only for
    LIVE accounts and immutable, and the downgrade back to 0015 works while unused but refuses
    once a LIVE account exists (tested at 0016 itself, before 0017's own refusal)."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        await asyncio.to_thread(downgrade, settings, "0015_mandate_timeframe", name)
        assert "live_key_checks" not in await _tables(settings, name)
        await asyncio.to_thread(upgrade, settings, "0016_live_locks", name)
        conn = await _superuser(settings, name)
        try:
            venue, user, live, activation = (uuid.uuid4() for _ in range(4))
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'BINANCE', 'Binance', 'EXCHANGE', 'UTC', '24x7', now())",
                venue,
            )
            await conn.execute(
                "INSERT INTO users (id, username, display_name, password_hash, roles, "
                "password_changed_at, created_at, updated_at) VALUES ($1, 'owner', 'o', 'x', "
                "ARRAY['OWNER'], now(), now(), now())",
                user,
            )
            row = (
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at) VALUES ($1, $2, 'LIVE', "
                "$3, $4, 'USDT', 'ACTIVE', 'UNKNOWN', now(), now())"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(row, uuid.uuid4(), "real-x", venue, "some_live")
            await conn.execute(row, live, "real-100", venue, "binance_spot_live")
            await conn.execute(
                "INSERT INTO live_activations (id, activated_by, readiness_snapshot, "
                "activated_at, account_id, cap, expires_at, reason) VALUES ($1, $2, '{}', now(), "
                "$3, 100, now() + interval '30 days', 'start with $100')",
                activation,
                user,
                live,
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
                await conn.execute(
                    "UPDATE live_activations SET cap = 1000 WHERE id = $1", activation
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO live_activations (id, activated_by, readiness_snapshot, "
                    "activated_at, account_id, cap, expires_at, reason) VALUES ($1, $2, '{}', "
                    "now(), $3, 100, now() + interval '91 days', 'too long a time')",
                    uuid.uuid4(),
                    user,
                    live,
                )
        finally:
            await conn.close()
        with pytest.raises(Exception, match="live activations, key checks or LIVE accounts exist"):
            await asyncio.to_thread(downgrade, settings, "0015_mandate_timeframe", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_a_live_account_names_its_key_for_good_and_blocks_downgrade(
    settings: Settings,
) -> None:
    """0017 (ADR-034): a LIVE account needs its key's fingerprint (16 hex characters), keeps it
    and stays LIVE; no account becomes LIVE by an update; one active LIVE account at most; the
    downgrade refuses while one exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue = uuid.uuid4()
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'BINANCE', 'Binance', 'EXCHANGE', 'UTC', '24x7', now())",
                venue,
            )
            row = (
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at, key_fingerprint) VALUES "
                "($1, $2, $3, $4, $5, 'USDT', 'ACTIVE', 'UNKNOWN', now(), now(), $6)"
            )
            live = "binance_spot_live"
            for bad in (None, "", "not-a-fingerprint", "0123456789ABCDEF"):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(row, uuid.uuid4(), "real-x", "LIVE", venue, live, bad)
            first = uuid.uuid4()
            await conn.execute(row, first, "real-a", "LIVE", venue, live, "0123456789abcdef")
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(row, uuid.uuid4(), "real-b", "LIVE", venue, live, "0" * 16)
            paper = uuid.uuid4()
            await conn.execute(row, paper, "paper-a", "PAPER", venue, "paper", None)
            for sql, account in (
                ("UPDATE accounts SET key_fingerprint = 'ffffffffffffffff' WHERE id = $1", first),
                (
                    "UPDATE accounts SET kind = 'PAPER', broker_adapter = 'paper' WHERE id = $1",
                    first,
                ),
                (
                    "UPDATE accounts SET kind = 'LIVE', broker_adapter = 'binance_spot_live', "
                    "key_fingerprint = '1111111111111111' WHERE id = $1",
                    paper,
                ),
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(sql, account)
            await conn.execute("UPDATE accounts SET status = 'DISABLED' WHERE id = $1", first)
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0017 cannot hold their keys"):
            await asyncio.to_thread(downgrade, settings, "0016_live_locks", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_self_renewing_runs_are_paper_only_and_block_downgrade(settings: Settings) -> None:
    """0019 (S49): only a PAPER account's run may renew itself, a run is continued at most once,
    a stop is append-only, and the downgrade refuses while any of them exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue, paper, sandbox, user = (uuid.uuid4() for _ in range(4))
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'PAPERV', 'paper', 'SIMULATED', 'UTC', '24x7', now())",
                venue,
            )
            await conn.execute(
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at) VALUES ($1, 'paper-x', "
                "'PAPER', $2, 'paper', 'USD', 'ACTIVE', 'UNKNOWN', now(), now())",
                paper,
                venue,
            )
            await conn.execute(SANDBOX_ROW, sandbox, "sandbox-x", venue, "binance_spot_testnet")
            await conn.execute(
                "INSERT INTO users (id, username, display_name, password_hash, roles, "
                "password_changed_at, created_at, updated_at) VALUES ($1, 'owner', 'o', 'x', "
                "ARRAY['OWNER'], now(), now(), now())",
                user,
            )
            insert = (
                "INSERT INTO trading_mandates (id, account_id, strategy_key, timeframe, "
                "instruments, max_loss_per_trade, max_trades_per_day, max_open_positions, "
                "daily_loss_limit, status, granted_by, granted_at, expires_at, capital, "
                "total_loss_limit, auto_renew) VALUES ($1, $2, 'turtle_55_20', '1d', "
                "ARRAY['X'], 2, 5, 2, 6, $3, $4, now(), now() + interval '90 days', 100, 20, true)"
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="simulated"):
                await conn.execute(insert, uuid.uuid4(), sandbox, "ACTIVE", user)
            await conn.execute(insert, uuid.uuid4(), paper, "ACTIVE", user)
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0019 cannot hold them"):
            await asyncio.to_thread(downgrade, settings, "0018_fill_book_checks", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_gold_reading_decisions_are_append_only_and_block_downgrade(
    settings: Settings,
) -> None:
    """0021 (S64): the HFM demo's gold-reading decisions are append-only, one per mandate and
    candle, and the downgrade refuses while any exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue, paper, user, mandate = (uuid.uuid4() for _ in range(4))
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'PAPERV', 'paper', 'SIMULATED', 'UTC', '24x7', now())",
                venue,
            )
            await conn.execute(
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at) VALUES ($1, 'paper-x', "
                "'PAPER', $2, 'paper', 'USD', 'ACTIVE', 'UNKNOWN', now(), now())",
                paper,
                venue,
            )
            await conn.execute(
                "INSERT INTO users (id, username, display_name, password_hash, roles, "
                "password_changed_at, created_at, updated_at) VALUES ($1, 'owner', 'o', 'x', "
                "ARRAY['OWNER'], now(), now(), now())",
                user,
            )
            await conn.execute(
                "INSERT INTO trading_mandates (id, account_id, strategy_key, timeframe, "
                "instruments, max_loss_per_trade, max_trades_per_day, max_open_positions, "
                "daily_loss_limit, status, granted_by, granted_at, expires_at) VALUES ($1, $2, "
                "'gold_reading', '1h', ARRAY['X'], 2, 5, 1, 6, 'ACTIVE', $3, now(), "
                "now() + interval '90 days')",
                mandate,
                paper,
                user,
            )
            insert = (
                "INSERT INTO gold_hfm_decisions (id, mandate_id, bar, at, decision, action, "
                "reasons) VALUES ($1, $2, '2026-11-04 12:00+00', now(), 'WAIT', 'WAIT', '[]')"
            )
            await conn.execute(insert, uuid.uuid4(), mandate)
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(insert, uuid.uuid4(), mandate)  # once per candle
            with pytest.raises(asyncpg.PostgresError, match="is append-only"):
                await conn.execute("UPDATE gold_hfm_decisions SET action = 'PROPOSED'")
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0021 cannot hold them"):
            await asyncio.to_thread(downgrade, settings, "0020_gold_simulation", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_shorts_only_on_a_cfd_of_a_simulated_account_and_block_downgrade(
    settings: Settings,
) -> None:
    """0022 (S66): a position may be below zero only on a CFD of a PAPER or SANDBOX account;
    spot stays long-only; the downgrade refuses while a short exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue, paper, base, quote, spot, cfd = (uuid.uuid4() for _ in range(6))
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'PAPERV', 'paper', 'SIMULATED', 'UTC', '24x7', now())",
                venue,
            )
            await conn.execute(
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at) VALUES ($1, 'paper-x', "
                "'PAPER', $2, 'paper', 'USD', 'ACTIVE', 'UNKNOWN', now(), now())",
                paper,
                venue,
            )
            for aid, sym, kind in ((base, "XAU", "METAL"), (quote, "USD", "FIAT")):
                await conn.execute(
                    "INSERT INTO assets (id, symbol, name, kind, display_decimals) "
                    "VALUES ($1, $2, $2, $3, 2)",
                    aid,
                    sym,
                    kind,
                )
            for iid, key, cls in (
                (spot, "SPOT:XAU/USD@PAPERV", "CRYPTO_SPOT"),
                (cfd, "CFD:XAU/USD@PAPERV", "CFD"),
            ):
                await conn.execute(
                    "INSERT INTO instruments (id, canonical_key, pair_key, venue_id, "
                    "base_asset_id, quote_asset_id, asset_class, status, tradable, updated_at) "
                    "VALUES ($1, $2, 'XAU/USD', $3, $4, $5, $6, 'ONLINE', true, now())",
                    iid,
                    key,
                    venue,
                    base,
                    quote,
                    cls,
                )
            insert = (
                "INSERT INTO positions (account_id, instrument_id, quantity, updated_at) "
                "VALUES ($1, $2, -1, now())"
            )
            with pytest.raises(asyncpg.CheckViolationError, match="only possible on a CFD"):
                await conn.execute(insert, paper, spot)
            await conn.execute(insert, paper, cfd)  # a short of a CFD on a simulated account
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0022 cannot hold them"):
            await asyncio.to_thread(downgrade, settings, "0021_gold_hfm_decisions", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_a_second_hfm_demo_account_is_allowed_and_blocks_downgrade(
    settings: Settings,
) -> None:
    """0026 (S74): a SANDBOX account may use the second HFM demo adapter, one active at a time
    like every adapter; unknown adapters stay refused; the downgrade refuses while one exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue = uuid.uuid4()
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'HFM', 'HFM', 'BROKER', 'UTC', 'weekdays', now())",
                venue,
            )
            await conn.execute(SANDBOX_ROW, uuid.uuid4(), "hfm-demo", venue, "hfm_mt5_demo")
            await conn.execute(SANDBOX_ROW, uuid.uuid4(), "hfm-demo-2", venue, "hfm_mt5_demo_2")
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(SANDBOX_ROW, uuid.uuid4(), "hfm-x", venue, "hfm_mt5_demo_2")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(SANDBOX_ROW, uuid.uuid4(), "hfm-y", venue, "hfm_mt5_demo_3")
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0026 cannot hold it"):
            await asyncio.to_thread(downgrade, settings, "0025_selftest_replaced", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_only_the_daily_gold_robot_may_risk_over_5_percent_and_blocks_downgrade(
    settings: Settings,
) -> None:
    """0027 (S75): a mandate's risk per trade stays at most 5 % except the daily gold robot's,
    up to 15 %; the downgrade refuses while a mandate above 5 % exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue, account = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'HFM', 'HFM', 'BROKER', 'UTC', 'weekdays', now())",
                venue,
            )
            await conn.execute(SANDBOX_ROW, account, "hfm-demo-2", venue, "hfm_mt5_demo_2")
            row = (
                "INSERT INTO trading_mandates (id, account_id, strategy_key, instruments, "
                "max_loss_per_trade, max_trades_per_day, max_open_positions, daily_loss_limit, "
                "status, granted_by, granted_at, expires_at, capital, risk_pct, mode, timeframe) "
                "VALUES ($1, $2, $3, ARRAY['CFD:XAU/USD@HFM'], 240, 1, 1, 240, 'ACTIVE', $4, "
                "now(), now() + interval '1 day', 2000, $5, 'AUTO', '1d')"
            )
            owner = uuid.uuid4()
            await conn.execute(
                "INSERT INTO users (id, username, display_name, password_hash, roles, "
                "password_changed_at, created_at, updated_at) VALUES ($1, 'owner', 'o', 'x', "
                "ARRAY['OWNER'], now(), now(), now())",
                owner,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(row, uuid.uuid4(), account, "turtle_55_20", owner, 6)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(row, uuid.uuid4(), account, "gold_daily", owner, 16)
            await conn.execute(row, uuid.uuid4(), account, "gold_daily", owner, 12)
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0027 cannot hold it"):
            await asyncio.to_thread(downgrade, settings, "0026_second_hfm_demo", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_robot_controls_are_bounded_and_block_downgrade(settings: Settings) -> None:
    """0028 (S79): a robot's controls keep the owner's rules within bounds and one of four
    statuses; the downgrade refuses while one exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue, account, owner, mandate = (uuid.uuid4() for _ in range(4))
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'HFM', 'HFM', 'BROKER', 'UTC', 'weekdays', now())",
                venue,
            )
            await conn.execute(SANDBOX_ROW, account, "hfm-demo", venue, "hfm_mt5_demo")
            await conn.execute(
                "INSERT INTO users (id, username, display_name, password_hash, roles, "
                "password_changed_at, created_at, updated_at) VALUES ($1, 'owner', 'o', 'x', "
                "ARRAY['OWNER'], now(), now(), now())",
                owner,
            )
            await conn.execute(
                "INSERT INTO trading_mandates (id, account_id, strategy_key, timeframe, "
                "instruments, max_loss_per_trade, max_trades_per_day, max_open_positions, "
                "daily_loss_limit, status, granted_by, granted_at, expires_at) VALUES ($1, $2, "
                "'gold_reading', '1h', ARRAY['CFD:XAU/USD@HFM'], 2, 5, 1, 6, 'ACTIVE', $3, now(), "
                "now() + interval '1 day')",
                mandate,
                account,
                owner,
            )
            row = (
                "INSERT INTO robot_controls (mandate_id, pause_after_losses, take_profit_r, "
                "daily_target, measure, session_started_at, status, status_reason, status_at, "
                "updated_at, updated_by) VALUES ($1, $2, $3, $4, $5, now(), $6, 'x', now(), now(), "
                "'test')"
            )
            for bad in (
                (0, 2, None, "REALIZED", "RUNNING"),  # pause after 0 losses
                (3, 20, None, "REALIZED", "RUNNING"),  # take-profit 20 × the risk
                (3, 2, 0, "REALIZED", "RUNNING"),  # a goal of 0
                (3, 2, None, "GROSS", "RUNNING"),
                (3, 2, None, "REALIZED", "DONE"),
            ):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(row, mandate, *bad)
            await conn.execute(row, mandate, 3, 2, 4, "REALIZED_AND_FLOATING", "TARGET_REACHED")
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0028 cannot hold them"):
            await asyncio.to_thread(downgrade, settings, "0027_gold_daily_risk", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_ai_keys_one_active_per_provider_tests_append_only_and_block_downgrade(
    settings: Settings,
) -> None:
    """0029 (S81): at most one active portal key per provider; test results are append-only;
    the downgrade refuses while either holds a row."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            key = (
                "INSERT INTO ai_provider_keys (id, provider_key, ciphertext, nonce, last4, "
                "added_at, added_by) VALUES ($1, 'groq', '\\x00'::bytea, $2, 'WXYZ', now(), 'x')"
            )
            await conn.execute(key, uuid.uuid4(), b"0" * 12)
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(key, uuid.uuid4(), b"1" * 12)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(key, uuid.uuid4(), b"short")
            test_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO ai_provider_tests (id, provider_key, at, key_source, auth_ok, "
                "model_ok, inference_ok, schema_ok, detail) VALUES ($1, 'groq', now(), 'PORTAL', "
                "true, true, true, true, 'ok')",
                test_id,
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
                await conn.execute(
                    "UPDATE ai_provider_tests SET detail = 'x' WHERE id = $1", test_id
                )
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0029 cannot hold them"):
            await asyncio.to_thread(downgrade, settings, "0028_robot_controls", name)
    finally:
        await bootstrap.drop_database(settings, name)


async def test_a_replaced_self_test_has_its_own_outcome_and_blocks_downgrade(
    settings: Settings,
) -> None:
    """0025 (S72): a self-test run replaced by the owner's newer START ends REPLACED, not
    FAILED; other outcomes stay refused; the downgrade refuses while a replaced run exists."""
    name = f"cosmic_migr_{secrets.token_hex(3)}"
    await bootstrap.bootstrap(settings, name)
    try:
        await asyncio.to_thread(upgrade, settings, "head", name)
        conn = await _superuser(settings, name)
        try:
            venue, paper, user, mandate = (uuid.uuid4() for _ in range(4))
            await conn.execute(
                "INSERT INTO venues (id, key, name, kind, timezone, session, created_at) "
                "VALUES ($1, 'PAPERV', 'paper', 'SIMULATED', 'UTC', '24x7', now())",
                venue,
            )
            await conn.execute(
                "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, base_currency, "
                "status, reconciliation_status, created_at, updated_at) VALUES ($1, 'paper-x', "
                "'PAPER', $2, 'paper', 'USD', 'ACTIVE', 'UNKNOWN', now(), now())",
                paper,
                venue,
            )
            await conn.execute(
                "INSERT INTO users (id, username, display_name, password_hash, roles, "
                "password_changed_at, created_at, updated_at) VALUES ($1, 'owner', 'o', 'x', "
                "ARRAY['OWNER'], now(), now(), now())",
                user,
            )
            await conn.execute(
                "INSERT INTO trading_mandates (id, account_id, strategy_key, timeframe, "
                "instruments, max_loss_per_trade, max_trades_per_day, max_open_positions, "
                "daily_loss_limit, status, granted_by, granted_at, expires_at) VALUES ($1, $2, "
                "'gold_reading', '1h', ARRAY['X'], 2, 5, 1, 6, 'ACTIVE', $3, now(), "
                "now() + interval '90 days')",
                mandate,
                paper,
                user,
            )
            insert = (
                "INSERT INTO gold_hfm_selftests (id, mandate_id, requested_by, requested_at, "
                "phase, outcome, updated_at, finished_at) VALUES ($1, $2, $3, now(), 'DONE', $4, "
                "now(), now())"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, uuid.uuid4(), mandate, user, "SKIPPED")
            await conn.execute(insert, uuid.uuid4(), mandate, user, "REPLACED")
        finally:
            await conn.close()
        with pytest.raises(Exception, match="the schema before 0025 cannot hold them"):
            await asyncio.to_thread(downgrade, settings, "0024_ai_gold_opinions", name)
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
