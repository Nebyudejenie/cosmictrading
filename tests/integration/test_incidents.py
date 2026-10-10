from __future__ import annotations

from cosmic.core.clock import FrozenClock
from cosmic.db.engine import Database
from cosmic.ops.incidents import (
    Category,
    Severity,
    has_open_critical,
    list_unresolved,
    raise_incident,
    resolve_by_key,
)


async def test_dedup_escalation_and_resolution(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        i1, new1 = await raise_incident(
            conn,
            dedup_key="k",
            category=Category.DATA,
            severity=Severity.WARNING,
            title="stale",
            now=clock.now(),
        )
        i2, new2 = await raise_incident(
            conn,
            dedup_key="k",
            category=Category.DATA,
            severity=Severity.WARNING,
            title="stale",
            now=clock.now(),
        )
        i3, escalated = await raise_incident(
            conn,
            dedup_key="k",
            category=Category.DATA,
            severity=Severity.CRITICAL,
            title="stale",
            now=clock.now(),
        )
    assert new1 and not new2 and escalated and i1 == i2 == i3
    async with db.connect() as conn:
        rows = await list_unresolved(conn)
        assert rows[0]["occurrences"] == 3 and rows[0]["severity"] == "CRITICAL"
        assert await has_open_critical(conn, [Category.DATA])
        assert not await has_open_critical(conn, [Category.SECURITY])
    async with db.transaction() as conn:
        assert await resolve_by_key(conn, dedup_key="k", now=clock.now(), resolution="fixed")
    async with db.transaction() as conn:
        _, new_again = await raise_incident(
            conn,
            dedup_key="k",
            category=Category.DATA,
            severity=Severity.INFO,
            title="again",
            now=clock.now(),
        )
    assert new_again
