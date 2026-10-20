from __future__ import annotations

import pytest
from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.errors import ValidationFailedError
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, notifications
from cosmic.identity.capabilities import Role
from cosmic.notify.alerts import FIELDS, Alert, alert_from_payload
from cosmic.ops.incidents import (
    Category,
    has_open_critical,
    list_unresolved,
    raise_incident,
    resolve_by_key,
)
from tests.helpers import make_user


def alert(severity: str = "WARNING", title: str = "stale") -> Alert:
    return Alert(
        title=title,
        severity=severity,  # type: ignore[arg-type]
        what_happened="source stopped answering",
        verified="last success 10 minutes ago",
        uncertain="cause",
        risk_now="prices are STALE",
        action_taken="labelled STALE",
        owner_action="check the source",
    )


def test_alert_requires_every_answer() -> None:
    a = alert()
    assert all(f in a.payload() for f in FIELDS)
    assert "What you should do: check the source" in a.text()
    with pytest.raises(ValidationFailedError, match="uncertain"):
        Alert(
            title="t",
            severity="INFO",
            what_happened="x",
            verified="x",
            uncertain=" ",
            risk_now="x",
            action_taken="x",
            owner_action="x",
        )
    with pytest.raises(ValidationFailedError, match="severity"):
        Alert(
            title="t",
            severity="LOUD",  # type: ignore[arg-type]
            what_happened="x",
            verified="x",
            uncertain="x",
            risk_now="x",
            action_taken="x",
            owner_action="x",
        )
    assert alert_from_payload({"alert": a.payload()}) == a.payload()
    assert alert_from_payload({"alert": {"title": "partial"}}) is None


async def test_dedup_escalation_resolution_and_alerts(db: Database, clock: FrozenClock) -> None:
    await make_user(db, clock, roles=[Role.OWNER])
    async with db.transaction() as conn:
        i1, new1 = await raise_incident(
            conn, dedup_key="k", category=Category.DATA, alert=alert(), now=clock.now()
        )
        i2, new2 = await raise_incident(
            conn, dedup_key="k", category=Category.DATA, alert=alert(), now=clock.now()
        )
        i3, escalated = await raise_incident(
            conn, dedup_key="k", category=Category.DATA, alert=alert("CRITICAL"), now=clock.now()
        )
    assert new1 and not new2 and escalated and i1 == i2 == i3
    async with db.connect() as conn:
        rows = await list_unresolved(conn)
        assert rows[0]["occurrences"] == 3 and rows[0]["severity"] == "CRITICAL"
        assert await has_open_critical(conn, [Category.DATA])
        assert not await has_open_critical(conn, [Category.SECURITY])
        stored = (await conn.execute(select(incidents.c.detail))).scalar_one()
        sent = [
            n.payload
            for n in await conn.execute(select(notifications).order_by(notifications.c.id))
        ]
    assert stored["alert"]["owner_action"] == "check the source"
    # one alert when it opened, one when it escalated, none for the repeat (rate limit)
    assert [p["alert"]["severity"] for p in sent] == ["WARNING", "CRITICAL"]
    assert all(alert_from_payload(p) for p in sent)
    async with db.transaction() as conn:
        assert await resolve_by_key(conn, dedup_key="k", now=clock.now(), resolution="fixed")
    async with db.transaction() as conn:
        _, new_again = await raise_incident(
            conn,
            dedup_key="k",
            category=Category.DATA,
            alert=alert("INFO", "again"),
            now=clock.now(),
        )
    assert new_again


async def test_an_incident_title_names_its_account(db: Database, clock: FrozenClock) -> None:
    """DIR-02: the incident lists, the inbox and Telegram all show the account's environment."""
    await make_user(db, clock, roles=[Role.OWNER])
    a = Alert(**{**alert().payload(), "account": "hfm-demo [SIMULATED · DEMO]"})  # type: ignore[arg-type]
    async with db.transaction() as conn:
        await raise_incident(
            conn, dedup_key="acct", category=Category.TRADING, alert=a, now=clock.now()
        )
    async with db.connect() as conn:
        title = (await conn.execute(select(incidents.c.title))).scalar_one()
        sent = [n.payload for n in await conn.execute(select(notifications))]
    assert title == "hfm-demo [SIMULATED · DEMO]: stale"
    assert all(p["alert"]["account"] == "hfm-demo [SIMULATED · DEMO]" for p in sent)
