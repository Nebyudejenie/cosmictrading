"""Async engine and transaction helpers (SQLAlchemy Core over asyncpg)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from cosmic.core.config import Settings


class Database:
    """Owns the application engine. All business writes go through ``transaction()``."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @classmethod
    def from_settings(cls, settings: Settings, *, database: str | None = None) -> Database:
        engine = create_async_engine(
            settings.app_dsn(database),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_size,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"server_settings": {"application_name": f"cosmic-{settings.env.value}"}},
        )
        return cls(engine)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        """READ COMMITTED transaction; commits on success, rolls back on error."""
        async with self.engine.begin() as conn:
            yield conn

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.connect() as conn:
            yield conn

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
