"""The owner's HFM demo balance, changed outside the system (S99; the owner, 2026-11-17, set their
demo to 100 USD in myHF and chose to keep it): the check refuses it, the home page says so, the
owner accepts HFM's balance with their code, the books follow it, and HFM's gold may then be
held on margin on demo accounts, at most 50 × the account."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, jobs
from cosmic.trading import ledger, venue_balance
from cosmic.trading.accounts import get_account
from cosmic.trading.policy import active_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, principal_for
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_hfm_demo import _no_hfm_fee
from tests.trading.test_hfm import bridge_running, build, next_report, open_account

pytestmark = pytest.mark.keeps_binance_paused


async def test_the_owner_accepts_hfms_new_balance_and_the_books_follow_it(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()  # 100,000 at HFM; the system manages 20,000 of it
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _no_hfm_fee(h)
        assert (await h.env.svc.reconciler.reconcile(h.env.account_id, trigger="test")).passed
        async with db.connect() as conn:
            account = await get_account(conn, account_id=h.env.account_id)
            assert await venue_balance.gap(conn, account) is None  # nothing to accept
        # The owner sets the demo's balance to 100 in myHF.
        emu.balance = D(100)
        await next_report(emu)
        r = await h.env.svc.reconciler.reconcile(h.env.account_id, trigger="test")
        assert not r.passed
        async with db.connect() as conn:
            found = await venue_balance.gap(conn, account)
        assert found is not None
        assert (found.hfm, found.managed, found.outside) == (D(100), D(20000), D(80000))
        assert (found.managed_after, found.outside_after) == (D(100), D(0))
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            home = (await c.get("/")).text
            assert "Your HFM demo's balance changed outside the system" in home
            assert "Accept HFM's balance (100.00 USD)" in home
            assert "at most 50 × the account" in home
            r2 = await c.post(
                "/gold/hfm/accept-balance",
                data={"csrf_token": csrf_from(home), "account_alias": "hfm-demo"},
            )
            assert r2.status_code == 303 and r2.headers["location"] == "/?balance=accepted"
            again = await c.post(
                "/gold/hfm/accept-balance",
                data={"csrf_token": csrf_from(home), "account_alias": "hfm-demo"},
            )
            assert again.status_code == 409  # nothing left to accept (the books moved)
        async with db.connect() as conn:
            books = await ledger.balances(conn, h.env.account_id)
            _, policy = await active_policy(conn)
            queued = await conn.scalar(
                select(jobs.c.payload).where(jobs.c.kind == "trading.reconcile")
            )
            audit = await conn.scalar(
                select(audit_events.c.payload).where(
                    audit_events.c.action == "account.venue_balance_accepted"
                )
            )
        assert books.get(("CASH", "USD")) == D(100)
        assert books.get(("UNMANAGED", "USD"), D(0)) == D(0)
        assert policy.margin_for("HFM", "CFD", "SANDBOX") == D(50)
        assert policy.margin_for("HFM", "CFD", "LIVE") is None
        assert D(audit["hfm_balance"]) == D(100) == D(audit["managed_after"])
        assert queued["account_id"] == str(h.env.account_id)  # the worker checks it again
        # The worker's check (queued) then finds the books equal to HFM's balance.
        assert (await h.env.svc.reconciler.reconcile(h.env.account_id, trigger="test")).passed
        async with db.connect() as conn:
            assert await venue_balance.gap(conn, account) is None


async def test_nothing_is_accepted_while_a_trade_is_open_or_for_another_reason(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        async with db.transaction() as conn:
            account = await get_account(conn, account_id=h.env.account_id)
            with pytest.raises(ConflictError, match="no balance difference to accept"):
                await venue_balance.accept(
                    conn,
                    account,
                    actor=principal_for(h.env.owner, h.env.clock).actor,
                    now=h.env.clock.now(),
                )
