from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders, trade_intents
from cosmic.identity import mfa
from cosmic.identity.capabilities import Role
from cosmic.market.jobs import build_ingest_service
from cosmic.sources.ingest import ensure_registry
from cosmic.trading.policy import ensure_default_policy
from cosmic.web.app import create_app
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from tests.helpers import client_for, csrf_from, full_login, make_user
from tests.market_mock import MarketMock
from tests.worker_helpers import run_queued

CAPTURE = datetime(2026, 10, 10, 21, 6, 0, tzinfo=UTC)

PROPOSAL = {
    "account_alias": "paper-main",
    "instrument_key": "SPOT:BTC/USD@COINBASE",
    "quantity": "0.004",
    "limit_price": "83000.00",
    "price_band_low": "82600",
    "price_band_high": "83400",
    "max_slippage_bps": "10",
    "time_in_force": "GTC",
    "stop_price": "81000",
    "stop_limit_price": "80900",
    "horizon": "days",
    "plain_summary": "Buy a small amount of BTC because the short-term trend is up.",
    "thesis": "Trend continuation after a higher low on the hourly chart <script>alert(1)</script>.",
    "counterargument": "Momentum often fails in ranges.",
    "red_team": "Volume does not confirm the move.",
    "scenario": "Gradual drift higher.",
    "invalidation": "Hourly close below 82,000.",
    "entry_conditions": "Limit buy at or below 83,000.",
}


async def _setup(settings: Settings, db: Database) -> tuple[FrozenClock, WorkerContext]:
    clock = FrozenClock(CAPTURE)
    ctx = WorkerContext(settings=settings, db=db, clock=clock, worker_id="test")
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    ingest = build_ingest_service(ctx, transport=MarketMock().transport())
    for key in ("coinbase_exchange_public", "bitstamp_public"):
        await ingest.sync_instruments(key)
        await ingest.ingest_quotes(key)
    await ingest.ingest_venue_status("coinbase_status")
    return clock, ctx


async def _post(c, path: str, data: dict[str, str], page: str = "/") -> object:  # type: ignore[no-untyped-def]
    token = csrf_from((await c.get(page)).text)
    return await c.post(path, data={**data, "csrf_token": token})


async def test_full_paper_approval_flow_through_portal(settings: Settings, db: Database) -> None:
    clock, ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    ctx.services["trading"] = app.state.trading
    registry = build_registry(ctx)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        r = await _post(
            c, "/ops/mode", {"mode": "PAPER_TRADING", "reason": "start paper trading"}, "/ops"
        )
        assert r.status_code == 303, r.text
        r = await _post(
            c, "/ops/paper-account", {"alias": "paper-main", "capital": "10000"}, "/ops"
        )
        assert r.status_code == 303, r.text
        assert "trading.reconcile" in await run_queued(ctx, registry)
        r = await _post(c, "/proposals", PROPOSAL, "/proposals/new")
        assert r.status_code == 303, r.text
        intent_url = r.headers["location"]
        detail = await c.get(intent_url)
        assert "Buy a small amount of BTC" in detail.text and "PROPOSED" in detail.text
        assert "<script>alert(1)</script>" not in detail.text  # escaped
        review = await _post(c, f"{intent_url}/review", {}, intent_url)
        assert review.status_code == 200
        assert (
            "Approve exact trade" in review.text
            and "0.004" in review.text
            and "83000" in review.text
        )
        token = re.search(r'name="challenge" value="([^"]+)"', review.text).group(1)  # type: ignore[union-attr]
        phash = re.search(r'name="packet_hash" value="([^"]+)"', review.text).group(1)  # type: ignore[union-attr]
        clock.advance(seconds=30)
        code = mfa.code_at(owner.mfa_secret or "", clock.now()) if owner.mfa_secret else ""
        decide = await c.post(
            f"{intent_url}/decide",
            data={
                "csrf_token": csrf_from(review.text),
                "challenge": token,
                "packet_hash": phash,
                "decision": "APPROVE",
                "code": code,
            },
        )
        assert decide.status_code == 303, decide.text
        svc = app.state.trading
        await svc.execution.run_cycle()
        await svc.paper.tick()
        await svc.execution.run_cycle()
        orders_page = await c.get("/orders")
        assert "FILLED" in orders_page.text and "SIMULATED" in orders_page.text
        portfolio = await c.get("/portfolio")
        assert "SPOT:BTC/USD@COINBASE" in portfolio.text and "ACTIVE" in portfolio.text
        stop = await _post(c, "/ops/emergency-stop", {"reason": "portal test stop"}, "/ops")
        assert stop.status_code == 303
        assert "trading.emergency_followup" in await run_queued(ctx, registry)
        ops = await c.get("/ops")
        assert "Recovery checklist" in ops.text and "EMERGENCY_STOP" in ops.text
        audit = await c.get("/audit")
        assert "killswitch.activated" in audit.text and "approval.approved" in audit.text
    async with db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.purpose == "ENTRY"))).one()
        stop_order = (
            await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))
        ).one()
    assert entry.status == "FILLED" and stop_order.status == "ACKNOWLEDGED"


async def test_viewer_and_csrf_and_stale_mfa_are_refused(settings: Settings, db: Database) -> None:
    clock, ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    viewer = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        await _post(
            c, "/ops/mode", {"mode": "PAPER_TRADING", "reason": "start paper trading"}, "/ops"
        )
        await _post(c, "/ops/paper-account", {"alias": "paper-main", "capital": "10000"}, "/ops")
        ctx.services["trading"] = app.state.trading
        await run_queued(ctx, build_registry(ctx))
        r = await _post(c, "/proposals", PROPOSAL, "/proposals/new")
        intent_url = r.headers["location"]
        # Missing CSRF token
        assert (await c.post(f"{intent_url}/review", data={})).status_code == 403
        # Stale MFA: confirm without a code is refused and nothing is approved.
        review = await _post(c, f"{intent_url}/review", {}, intent_url)
        clock.advance(minutes=11)
        token = re.search(r'name="challenge" value="([^"]+)"', review.text).group(1)  # type: ignore[union-attr]
        phash = re.search(r'name="packet_hash" value="([^"]+)"', review.text).group(1)  # type: ignore[union-attr]
        refused = await c.post(
            f"{intent_url}/decide",
            data={
                "csrf_token": csrf_from(review.text),
                "challenge": token,
                "packet_hash": phash,
                "decision": "APPROVE",
                "code": "",
            },
        )
        assert refused.status_code == 403
    async with client_for(app) as v:
        await full_login(v, viewer, clock)
        assert (await v.get(intent_url)).status_code == 403
        assert (await v.get("/portfolio")).status_code == 403
        page = await v.get("/ops")
        assert page.status_code == 200 and "Emergency stop</button>" not in page.text
    async with db.connect() as conn:
        status = (await conn.execute(select(trade_intents.c.status))).scalar_one()
    assert status == "PROPOSED"


@pytest.mark.parametrize(
    "path", ["/approvals", "/portfolio", "/orders", "/ops", "/audit", "/proposals/new"]
)
async def test_trading_pages_require_login(settings: Settings, db: Database, path: str) -> None:
    app = create_app(settings, db=db, clock=FrozenClock(CAPTURE))
    async with client_for(app) as c:
        r = await c.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"
