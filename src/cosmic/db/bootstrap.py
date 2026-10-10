"""Idempotent cluster bootstrap: roles and database (run once as the superuser).

Creates ``cosmic_owner`` (schema owner, runs migrations) and ``cosmic_app``
(runtime role without DDL). Passwords come from settings and are passed as
bound parameters through ``format('%L')`` so they never appear in logs.
"""

from __future__ import annotations

import asyncpg

from cosmic.core.config import Settings
from cosmic.core.errors import ConfigError


def ident(name: str) -> str:
    """Quote a PostgreSQL identifier (names come from configuration, not users)."""
    if not name or "\x00" in name:
        raise ConfigError("invalid identifier")
    return '"' + name.replace('"', '""') + '"'


def _pg_dsn(settings: Settings, user: str, password: str, database: str) -> str:
    from urllib.parse import quote

    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{settings.db_host}:{settings.db_port}/{database}"
    )


async def _ensure_role(conn: asyncpg.Connection, role: str, password: str) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    verb = "ALTER" if exists else "CREATE"
    # Identifiers via %I, literal password via %L — no string concatenation of input.
    statement = await conn.fetchval(
        "SELECT format('%s ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION PASSWORD %L', $1::text, $2::text, $3::text)",
        verb,
        role,
        password,
    )
    await conn.execute(statement)


async def bootstrap(settings: Settings, database: str | None = None) -> None:
    if settings.db_superuser_password is None:
        raise ConfigError("COSMIC_DB_SUPERUSER_PASSWORD is required for bootstrap")
    if settings.db_owner_password is None or settings.db_app_password is None:
        raise ConfigError("owner and app database passwords are required for bootstrap")
    db_name = database or settings.db_name
    su = settings.db_superuser_password.get_secret_value()
    conn = await asyncpg.connect(_pg_dsn(settings, settings.db_superuser, su, "postgres"))
    try:
        await _ensure_role(
            conn, settings.db_owner_user, settings.db_owner_password.get_secret_value()
        )
        await _ensure_role(conn, settings.db_app_user, settings.db_app_password.get_secret_value())
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            stmt = await conn.fetchval(
                "SELECT format('CREATE DATABASE %I OWNER %I', $1::text, $2::text)",
                db_name,
                settings.db_owner_user,
            )
            await conn.execute(stmt)
    finally:
        await conn.close()
    conn = await asyncpg.connect(_pg_dsn(settings, settings.db_superuser, su, db_name))
    try:
        owner, app, db = ident(settings.db_owner_user), ident(settings.db_app_user), ident(db_name)
        await conn.execute(f"REVOKE ALL ON DATABASE {db} FROM PUBLIC")
        await conn.execute(f"GRANT CONNECT, TEMPORARY ON DATABASE {db} TO {owner}, {app}")
        await conn.execute(f"ALTER SCHEMA public OWNER TO {owner}")
        await conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {app}")
    finally:
        await conn.close()


async def drop_database(settings: Settings, database: str) -> None:
    """Drop a database (tests only — callers must guard the name)."""
    if settings.db_superuser_password is None:
        raise ConfigError("superuser password required")
    su = settings.db_superuser_password.get_secret_value()
    conn = await asyncpg.connect(_pg_dsn(settings, settings.db_superuser, su, "postgres"))
    try:
        stmt = await conn.fetchval(
            "SELECT format('DROP DATABASE IF EXISTS %I WITH (FORCE)', $1::text)", database
        )
        await conn.execute(stmt)
    finally:
        await conn.close()
