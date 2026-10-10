from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, StepUpRequiredError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.sources.ingest import ensure_registry
from cosmic.trading import ledger
from cosmic.trading.accounts import create_paper_account, get_account
from cosmic.trading.policy import (
    DEFAULT_POLICY,
    active_policy,
    change_policy,
    ensure_default_policy,
)
from tests.helpers import make_user, principal_for

D = Decimal


async def test_default_policy_and_change_workflow(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    async with db.transaction() as conn:
        assert await ensure_default_policy(conn, clock.now()) == 1
        assert await ensure_default_policy(conn, clock.now()) == 1
    owner = await make_user(db, clock)
    new = DEFAULT_POLICY.model_dump(mode="python") | {"max_order_notional": D("250")}
    with pytest.raises(StepUpRequiredError):
        async with db.transaction() as conn:
            await change_policy(
                conn,
                principal=principal_for(owner, clock, mfa_fresh=False),
                new_params=new,
                reason="reduce order size cap",
                now=clock.now(),
                settings=settings,
            )
    with pytest.raises(ValidationFailedError):
        async with db.transaction() as conn:
            await change_policy(
                conn,
                principal=principal_for(owner, clock),
                new_params=new | {"max_leverage": D("3")},
                reason="unsafe change attempt",
                now=clock.now(),
                settings=settings,
            )
    async with db.transaction() as conn:
        v = await change_policy(
            conn,
            principal=principal_for(owner, clock),
            new_params=new,
            reason="reduce order size cap",
            now=clock.now(),
            settings=settings,
        )
    assert v == 2
    async with db.connect() as conn:
        version, params = await active_policy(conn)
    assert version == 2 and params.max_order_notional == D("250")
    with pytest.raises(ConflictError, match="identical"):
        async with db.transaction() as conn:
            await change_policy(
                conn,
                principal=principal_for(owner, clock),
                new_params=new,
                reason="same thing again",
                now=clock.now(),
                settings=settings,
            )


async def test_policy_versions_immutable(db: Database, clock: FrozenClock) -> None:
    async with db.transaction() as conn:
        await ensure_default_policy(conn, clock.now())
    for stmt in ("UPDATE risk_policies SET params = '{}'::jsonb", "DELETE FROM risk_policies"):
        with pytest.raises(DBAPIError):
            async with db.transaction() as conn:
                await conn.execute(text(stmt))


async def test_tampered_policy_fails_closed(
    db: Database, clock: FrozenClock, superuser_conn: asyncpg.Connection
) -> None:
    async with db.transaction() as conn:
        await ensure_default_policy(conn, clock.now())
    async with superuser_conn.transaction():
        await superuser_conn.execute("SET LOCAL session_replication_role = replica")
        await superuser_conn.execute(
            "UPDATE risk_policies SET params = jsonb_set(params, '{max_order_notional}', '\"999999\"')"
        )
    with pytest.raises(ConflictError, match="invalid"):
        async with db.connect() as conn:
            await active_policy(conn)
    # A tamper that stays schema-valid is caught by the content hash.
    async with superuser_conn.transaction():
        await superuser_conn.execute("SET LOCAL session_replication_role = replica")
        await superuser_conn.execute(
            "UPDATE risk_policies SET params = jsonb_set(params, '{max_order_notional}', '\"400\"')"
        )
    with pytest.raises(ConflictError, match="integrity"):
        async with db.connect() as conn:
            await active_policy(conn)


async def test_paper_account_capital_and_ledger(db: Database, clock: FrozenClock) -> None:
    owner = await make_user(db, clock)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        acct_id = await create_paper_account(
            conn,
            alias="paper-main",
            venue_key="COINBASE",
            base_currency="USD",
            initial_capital=D("10000"),
            now=clock.now(),
            actor=principal_for(owner, clock).actor,
        )
    async with db.connect() as conn:
        acct = await get_account(conn, alias="paper-main")
        bal = await ledger.balances(conn, acct_id)
    assert (
        acct.kind == "PAPER"
        and acct.broker_adapter == "paper"
        and acct.reconciliation_status == "UNKNOWN"
    )
    assert bal == {("CASH", "USD"): D("10000"), ("CAPITAL", "USD"): D("-10000")}
    entries = ledger.fill_entries(
        side="BUY",
        base="BTC",
        quote="USD",
        quantity=D("0.004"),
        price=D("83000"),
        fee=D("1.99"),
        fee_asset="USD",
    )
    async with db.transaction() as conn:
        first = await ledger.post(
            conn,
            account_id=acct_id,
            kind="FILL",
            ref_type="fill",
            ref_id="f1",
            description="test fill",
            entries=entries,
            now=clock.now(),
            is_simulated=True,
        )
        again = await ledger.post(
            conn,
            account_id=acct_id,
            kind="FILL",
            ref_type="fill",
            ref_id="f1",
            description="test fill",
            entries=entries,
            now=clock.now(),
            is_simulated=True,
        )
    assert first is not None and again is None  # idempotent per reference
    async with db.connect() as conn:
        assert await ledger.book_balance(conn, acct_id, "CASH", "USD") == D("10000") - D("332") - D(
            "1.99"
        )
        assert await ledger.book_balance(conn, acct_id, "POSITION", "BTC") == D("0.004")


async def test_db_rejects_unbalanced_transaction_at_commit(
    db: Database, clock: FrozenClock
) -> None:
    owner = await make_user(db, clock)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        acct_id = await create_paper_account(
            conn,
            alias="paper-x",
            venue_key="COINBASE",
            base_currency="USD",
            initial_capital=D("100"),
            now=clock.now(),
            actor=principal_for(owner, clock).actor,
        )
    txn = uuid7()
    with pytest.raises(DBAPIError, match="does not balance"):
        async with db.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ledger_txns (id, account_id, kind, ref_type, ref_id, description, is_simulated, created_at) "
                    "VALUES (:t, :a, 'ADJUSTMENT_RECONCILE', 'x', 'y', 'forged', true, now())"
                ),
                {"t": txn, "a": acct_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO ledger_entries (id, txn_id, account_id, book, asset, amount, created_at) "
                    "VALUES (:i, :t, :a, 'CASH', 'USD', 1000000, now())"
                ),
                {"i": uuid7(), "t": txn, "a": acct_id},
            )
    for stmt in ("UPDATE ledger_entries SET amount = 1", "DELETE FROM ledger_txns"):
        with pytest.raises(DBAPIError):
            async with db.transaction() as conn:
                await conn.execute(text(stmt))
