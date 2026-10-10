"""Test harness.

* Unit tests run without a database and with outbound network blocked.
* Tests under integration/, security/, trading/, e2e/, reliability/ are marked ``db``:
  they run against a fresh database migrated to head, connect as the runtime role
  ``cosmic_app`` (exactly like production) and FAIL — not skip — if PostgreSQL is down.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Environment, Settings, load_settings
from cosmic.core.redaction import SECRETS
from cosmic.db import bootstrap as db_bootstrap
from cosmic.db.engine import Database
from cosmic.db.migrate import upgrade

ROOT = Path(__file__).resolve().parent.parent
DB_DIRS = ("integration", "security", "trading", "e2e", "reliability")
START = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


def _load_dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.fspath)).relative_to(ROOT / "tests").parts
        if parts and parts[0] in DB_DIRS:
            item.add_marker(pytest.mark.db)


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block non-loopback connections unless the test is marked live_data."""
    if request.node.get_closest_marker("live_data"):
        return
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if (
            isinstance(host, str)
            and host not in ("127.0.0.1", "::1", "localhost")
            and not host.startswith("/")
        ):
            raise RuntimeError(f"network access blocked in tests: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(scope="session")
def base_env() -> dict[str, str]:
    env = {**_load_dotenv(), **{k: v for k, v in os.environ.items() if k.startswith("COSMIC_")}}
    return env


@pytest.fixture(scope="session")
def test_db_name() -> str:
    return f"cosmic_test_{secrets.token_hex(4)}"


@pytest.fixture(scope="session")
def settings(base_env: dict[str, str], test_db_name: str) -> Settings:
    env = dict(base_env)
    env.update(
        {
            "COSMIC_ENV": Environment.TEST.value,
            "COSMIC_DB_NAME": test_db_name,
            "COSMIC_COOKIE_SECURE": "true",
            "COSMIC_ALLOWED_HOSTS": '["testserver","127.0.0.1","localhost"]',
            "COSMIC_LOG_JSON": "true",
            "COSMIC_PAPER_FAULT_INJECTION": "true",
            "COSMIC_MARKET_DATA_ENABLED": "false",
        }
    )
    env.setdefault("COSMIC_SECRET_KEY", "insecure-dev-test-key")
    s = load_settings(env)
    SECRETS.register(s.secret_values())
    return s


@pytest.fixture(scope="session")
async def _database(settings: Settings, test_db_name: str) -> AsyncIterator[str]:
    try:
        await db_bootstrap.bootstrap(settings, test_db_name)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.fail(f"development PostgreSQL unavailable ({type(exc).__name__}); run `make db-up`")
    await asyncio.to_thread(upgrade, settings, "head", test_db_name)
    yield test_db_name
    await db_bootstrap.drop_database(settings, test_db_name)


@pytest.fixture(scope="session")
async def db(settings: Settings, _database: str) -> AsyncIterator[Database]:
    database = Database.from_settings(settings, database=_database)
    yield database
    await database.dispose()


@pytest.fixture(scope="session")
async def superuser_conn(settings: Settings, _database: str) -> AsyncIterator[asyncpg.Connection]:
    assert settings.db_superuser_password is not None
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_superuser,
        password=settings.db_superuser_password.get_secret_value(),
        database=_database,
    )
    yield conn
    await conn.close()


async def truncate_all(conn: asyncpg.Connection) -> None:
    tables = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
    )
    if not tables:
        return
    names = ", ".join(f'"{r["tablename"]}"' for r in tables)
    async with conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
        # Re-create the singleton seeded by migration 0003 (truncation removed it).
        if any(r["tablename"] == "authority_state" for r in tables):
            await conn.execute(
                "INSERT INTO authority_state (id, mode, version, changed_at, changed_by, reason) "
                "VALUES (1, 'RESEARCH_ONLY', 1, now(), 'migration', 'initial safe default')"
            )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def fixtures_dir() -> Path:
    return ROOT / "tests" / "fixtures"
