"""Privileged portal forms accept the authenticator code inline when the last one is stale."""

from __future__ import annotations

from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_trading import accounts
from cosmic.identity import mfa
from cosmic.web.app import create_app
from tests.e2e.test_trading_portal import _setup
from tests.helpers import client_for, csrf_from, full_login, make_user


async def test_paper_account_with_inline_code(settings: Settings, db: Database) -> None:
    clock, _ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        fresh_page = (await c.get("/ops")).text
        assert 'name="code"' not in fresh_page  # verified moments ago: no extra field
        clock.advance(minutes=11)  # the sign-in code is now too old for privileged actions
        page = (await c.get("/ops")).text
        assert 'id="code-paper"' in page and 'id="code-mode"' in page
        token = csrf_from(page)
        form = {"csrf_token": token, "alias": "paper-main", "capital": "10000"}
        r = await c.post(
            "/ops/paper-account", data=form, headers={"Referer": "http://testserver/ops"}
        )
        assert r.status_code == 403 and "Enter your authenticator code" in r.text
        assert 'href="/mfa?next=/ops"' in r.text  # back to the form after verifying
        r = await c.post("/ops/paper-account", data={**form, "code": "000000"})
        assert r.status_code == 403 and "wrong or already used" in r.text
        clock.advance(seconds=30)  # a new 30-second step: a code not used before
        code = mfa.code_at(owner.mfa_secret or "", clock.now())
        r = await c.post("/ops/paper-account", data={**form, "code": code})
        assert r.status_code == 303, r.text
        assert 'name="code"' not in (await c.get("/ops")).text  # verified again
    async with db.connect() as conn:
        assert (await conn.execute(select(accounts.c.alias))).scalar_one() == "paper-main"
