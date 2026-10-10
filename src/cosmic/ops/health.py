"""Readiness: database reachable and schema at the expected migration head."""

from __future__ import annotations

from sqlalchemy import text

from cosmic.db.engine import Database
from cosmic.db.migrate import head_revision


async def current_revision(db: Database) -> str | None:
    try:
        async with db.connect() as conn:
            value = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            return None if value is None else str(value)
    except Exception:
        return None


async def readiness(db: Database) -> tuple[bool, dict[str, bool]]:
    db_ok = await db.ping()
    rev = await current_revision(db) if db_ok else None
    schema_ok = rev is not None and rev == head_revision()
    return db_ok and schema_ok, {"database": db_ok, "schema": schema_ok}
