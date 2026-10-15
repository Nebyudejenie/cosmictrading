"""Operations → Binance test network: the card, and the form that queues the worker job."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import jobs
from cosmic.sources.ingest import ensure_registry
from cosmic.trading.policy import active_policy, ensure_default_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user

CAPTURE = datetime(2026, 10, 10, 21, 6, 0, tzinfo=UTC)


async def test_owner_queues_a_test_network_account_for_the_worker(
    settings: Settings, db: Database
) -> None:
    clock = FrozenClock(CAPTURE)
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = (await c.get("/ops")).text
        assert "Binance test network" in page and "never run" in page
        assert 'action="/ops/sandbox-account"' in page
        token = csrf_from(page)
        bad = await c.post(
            "/ops/sandbox-account",
            data={"csrf_token": token, "alias": "Bad Alias!", "capital": "100"},
        )
        assert bad.status_code == 422
        r = await c.post(
            "/ops/sandbox-account",
            data={"csrf_token": token, "alias": "binance-test", "capital": "250.5"},
        )
        assert r.status_code == 303, r.text
    async with db.connect() as conn:
        rows = list(
            await conn.execute(
                select(jobs.c.payload).where(jobs.c.kind == "trading.open_sandbox_account")
            )
        )
    assert [r.payload["alias"] for r in rows] == ["binance-test"]
    assert rows[0].payload["managed_capital"] == "250.5"
    assert rows[0].payload["user_id"] == str(owner.user_id)


async def test_owner_allows_the_test_network_in_the_risk_policy(
    settings: Settings, db: Database
) -> None:
    """The risk page edits what may be traded; added entries must exist, be tradable and
    come with their venue and currency."""
    clock = FrozenClock(CAPTURE)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    btc = "SPOT:BTC/USDT@BINANCE_TESTNET"
    base = {"reason": "trade on the Binance test network"}
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        page = (await c.get("/settings/risk")).text
        assert (
            'value="SPOT:BTC/USDT@BINANCE, SPOT:PAXG/USDT@BINANCE, SPOT:BTC/USD@COINBASE, '
            'SPOT:ETH/USD@COINBASE"' in page
        )
        token = csrf_from(page)
        for fields, problem in (
            ({"allowed_instruments": f"SPOT:BTC/USD@COINBASE, {btc}"}, "also needs venue"),
            ({"allowed_instruments": "SPOT:DOGE/USD@NOWHERE"}, "unknown instrument"),
            ({"allowed_instruments": "SPOT:XAUT/USDT@BINANCE"}, "not tradable"),
            ({"allowed_venues": "COINBASE, MOON"}, "unknown venue"),
        ):
            r = await c.post("/settings/risk", data={"csrf_token": token, **base, **fields})
            assert r.status_code == 422 and problem in r.text, (fields, r.status_code)
        r = await c.post(
            "/settings/risk",
            data={
                "csrf_token": token,
                **base,
                "allowed_instruments": f"SPOT:BTC/USD@COINBASE, SPOT:ETH/USD@COINBASE, {btc}",
                "allowed_venues": "COINBASE, BINANCE_TESTNET",
                "allowed_quote_currencies": "USD, USDT",
            },
        )
        assert r.status_code == 303, r.text
    async with db.connect() as conn:
        version, policy = await active_policy(conn)
    assert version == 2 and btc in policy.allowed_instruments
    assert policy.allowed_venues == ("COINBASE", "BINANCE_TESTNET")
    assert policy.allowed_quote_currencies == ("USD", "USDT")
