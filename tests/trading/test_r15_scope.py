"""R15's scope (S120, the owner's choice of 2026-11-26): an accounting alarm about one account
(its books against its own venue) stops that account's trades only; a security alarm, and an
accounting alarm that names no single account, still stop every account (fail closed).

The case: Robot B's HFM demo failed its reconciliation after the owner's hand trades and refill,
and that alone refused every trade on their Exness demo."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from cosmic.db.engine import Database
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident
from cosmic.trading.context_builder import critical_categories, incident_account
from cosmic.trading.risk.checks import Status, incidents_check

NOW = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)


def _alert() -> Alert:
    return Alert(
        title="test",
        severity="CRITICAL",
        what_happened="a test",
        verified="a test",
        uncertain="none",
        risk_now="none",
        action_taken="none",
        owner_action="none",
    )


def _account(alias: str) -> Any:
    return SimpleNamespace(alias=alias, id=uuid.uuid4())


def test_the_account_an_accounting_alarm_names() -> None:
    assert incident_account("accounting.reconciliation_failed:hfm-demo") == "hfm-demo"
    assert incident_account("accounting.venue_unreachable:hfm-demo:money") == "hfm-demo"
    assert incident_account("accounting.venue_unreachable:hfm-demo") == "hfm-demo"
    account_id = str(uuid.uuid4())
    assert incident_account(f"accounting.foreign_fee:{account_id}:BNB") == account_id
    # An order's alarm names no single account here: it counts for every account.
    assert incident_account(f"accounting.overfill:{uuid.uuid4()}") is None
    assert incident_account("trading.bridge_not_demo:hfm-demo-2") is None


async def test_an_accounting_alarm_stops_its_own_account_and_a_security_alarm_every_one(
    db: Database,
) -> None:
    hfm, exness = _account("hfm-demo"), _account("exness-demo")
    async with db.transaction() as conn:
        await raise_incident(
            conn,
            dedup_key="accounting.reconciliation_failed:hfm-demo",
            category=Category.ACCOUNTING,
            alert=_alert(),
            now=NOW,
        )
    async with db.connect() as conn:
        assert await critical_categories(conn, hfm) == {"ACCOUNTING"}
        assert await critical_categories(conn, exness) == frozenset()
        assert await critical_categories(conn) == {"ACCOUNTING"}  # the readiness panel: all
    assert incidents_check(frozenset()).status is Status.PASS
    assert incidents_check(frozenset({"ACCOUNTING"})).status is Status.FAIL
    # An accounting alarm that names no single account still stops every account.
    async with db.transaction() as conn:
        await raise_incident(
            conn,
            dedup_key=f"accounting.overfill:{uuid.uuid4()}",
            category=Category.ACCOUNTING,
            alert=_alert(),
            now=NOW,
        )
    async with db.connect() as conn:
        assert await critical_categories(conn, exness) == {"ACCOUNTING"}
    # A security alarm stops every account, whichever it is about.
    async with db.transaction() as conn:
        await raise_incident(
            conn,
            dedup_key="trading.bridge_not_demo:hfm-demo-2",
            category=Category.SECURITY,
            alert=_alert(),
            now=NOW,
        )
    async with db.connect() as conn:
        assert await critical_categories(conn, exness) == {"ACCOUNTING", "SECURITY"}
