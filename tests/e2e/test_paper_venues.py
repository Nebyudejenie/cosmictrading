"""Operations → Paper account: Binance (USDT) by default; retired venues are not offered."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_market import venues
from cosmic.db.schema_trading import accounts
from cosmic.sources.ingest import ensure_registry
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user

CAPTURE = datetime(2026, 10, 10, 21, 6, 0, tzinfo=UTC)


async def test_paper_accounts_default_to_binance_and_skip_retired_venues(
    settings: Settings, db: Database
) -> None:
    clock = FrozenClock(CAPTURE)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
    retired = settings.model_copy(update={"retired_venues": ["COINBASE", "BITSTAMP"]})
    app = create_app(retired, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = (await c.get("/ops")).text
        assert '<option value="BINANCE">BINANCE (USDT)</option>' in page
        assert 'value="COINBASE"' not in page
        token = csrf_from(page)
        refused = await c.post(
            "/ops/paper-account",
            data={"csrf_token": token, "alias": "paper-cb", "capital": "100", "venue": "COINBASE"},
        )
        assert refused.status_code == 422
        r = await c.post(
            "/ops/paper-account",
            data={"csrf_token": token, "alias": "paper-binance", "capital": "1000"},
        )
        assert r.status_code == 303, r.text
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(accounts.c.alias, accounts.c.base_currency, venues.c.key).join(
                    venues, venues.c.id == accounts.c.venue_id
                )
            )
        ).one()
    assert tuple(row) == ("paper-binance", "USDT", "BINANCE")
