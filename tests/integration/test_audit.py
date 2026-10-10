from __future__ import annotations

import asyncio

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cosmic.audit.service import Actor, events_for, head, record, verify_chain
from cosmic.core.clock import FrozenClock
from cosmic.db.engine import Database


async def test_chain_links_and_verifies(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        s1 = await record(
            conn,
            action="test.one",
            actor=Actor.system("t"),
            occurred_at=clock.now(),
            entity_type="thing",
            entity_id="1",
            payload={"a": 1},
        )
        s2 = await record(
            conn,
            action="test.two",
            actor=Actor.system("t"),
            occurred_at=clock.now(),
            entity_type="thing",
            entity_id="1",
            payload={"b": "x"},
        )
    assert (s1, s2) == (1, 2)
    async with db.connect() as conn:
        result = await verify_chain(conn)
        rows = await events_for(conn, "thing", "1")
        h = await head(conn)
    assert result.ok and result.checked == 2
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    assert rows[0]["prev_hash"] == "0" * 64
    assert h == (2, rows[1]["hash"])


async def test_payload_is_redacted(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        await record(
            conn,
            action="test.secret",
            actor=Actor.system("t"),
            occurred_at=clock.now(),
            entity_type="x",
            entity_id="1",
            payload={"api_key": "gsk_" + "a" * 40, "note": "token gsk_" + "b" * 40},
        )
    async with db.connect() as conn:
        rows = await events_for(conn, "x", "1")
    assert rows[0]["payload"]["api_key"] == "[REDACTED]"
    assert "gsk_" not in rows[0]["payload"]["note"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET action = 'forged'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
async def test_app_role_cannot_modify_audit(
    db: Database, clock: FrozenClock, statement: str
) -> None:
    async with db.transaction() as conn:
        await record(conn, action="test.x", actor=Actor.system("t"), occurred_at=clock.now())
    with pytest.raises(DBAPIError) as exc:
        async with db.transaction() as conn:
            await conn.execute(text(statement))
    assert "permission denied" in str(exc.value).lower() or "append-only" in str(exc.value).lower()


async def test_even_owner_triggers_block_update(
    superuser_conn: asyncpg.Connection, db: Database, clock: FrozenClock
) -> None:
    async with db.transaction() as conn:
        await record(conn, action="test.x", actor=Actor.system("t"), occurred_at=clock.now())
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
        await superuser_conn.execute("UPDATE audit_events SET action='forged'")


async def test_tamper_is_detected(
    superuser_conn: asyncpg.Connection, db: Database, clock: FrozenClock
) -> None:
    async with db.transaction() as conn:
        for i in range(3):
            await record(conn, action=f"test.{i}", actor=Actor.system("t"), occurred_at=clock.now())
    # A superuser bypassing triggers (replica role) can alter rows — the chain makes it evident.
    async with superuser_conn.transaction():
        await superuser_conn.execute("SET LOCAL session_replication_role = replica")
        await superuser_conn.execute(
            "UPDATE audit_events SET payload = '{\"forged\": true}' WHERE seq = 2"
        )
    async with db.connect() as conn:
        result = await verify_chain(conn)
    assert not result.ok and result.first_bad_seq == 2 and result.reason == "content hash mismatch"


async def test_deletion_gap_is_detected(
    superuser_conn: asyncpg.Connection, db: Database, clock: FrozenClock
) -> None:
    async with db.transaction() as conn:
        for i in range(3):
            await record(conn, action=f"test.{i}", actor=Actor.system("t"), occurred_at=clock.now())
    async with superuser_conn.transaction():
        await superuser_conn.execute("SET LOCAL session_replication_role = replica")
        await superuser_conn.execute("DELETE FROM audit_events WHERE seq = 2")
    async with db.connect() as conn:
        result = await verify_chain(conn)
    assert not result.ok and result.reason == "sequence gap"


async def test_concurrent_writers_produce_linear_chain(db: Database, clock: FrozenClock) -> None:
    async def writer(n: int) -> None:
        for i in range(10):
            async with db.transaction() as conn:
                await record(
                    conn, action=f"w{n}.{i}", actor=Actor.system("t"), occurred_at=clock.now()
                )

    await asyncio.gather(*(writer(n) for n in range(4)))
    async with db.connect() as conn:
        result = await verify_chain(conn)
    assert result.ok and result.checked == 40


async def test_app_role_has_no_ddl(db: Database) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with db.transaction() as conn:
            await conn.execute(text("CREATE TABLE evil (x int)"))
    with pytest.raises(DBAPIError, match="permission denied"):
        async with db.transaction() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = 'x'"))
