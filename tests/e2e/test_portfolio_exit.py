"""Closing a position from the portfolio page: an approved exit that replaces its stop."""

from __future__ import annotations

import re
from decimal import Decimal

import httpx
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders, trade_intents
from cosmic.db.schema_market import fetch_runs, instruments, quotes, sources
from cosmic.db.schema_trading import positions
from cosmic.identity import mfa
from cosmic.web.app import create_app
from cosmic.worker.jobs import build_registry
from tests import helpers
from tests.e2e.test_trading_portal import PROPOSAL, _post, _setup
from tests.helpers import client_for, csrf_from, full_login, make_user
from tests.worker_helpers import run_queued


async def fresh_quotes(db: Database, now: object, bid: str, ask: str) -> None:
    """New quotes for both BTC/USD venues at ``now`` (the frozen clock moves during tests)."""
    async with db.transaction() as conn:
        for r in await conn.execute(
            select(instruments.c.id, instruments.c.canonical_key).where(
                instruments.c.pair_key == "BTC/USD"
            )
        ):
            key = (
                "coinbase_exchange_public"
                if r.canonical_key.endswith("COINBASE")
                else "bitstamp_public"
            )
            src = (
                await conn.execute(select(sources.c.id).where(sources.c.key == key))
            ).scalar_one()
            run = uuid7()
            await conn.execute(
                fetch_runs.insert().values(
                    id=run,
                    source_id=src,
                    kind="test_quote",
                    started_at=now,
                    finished_at=now,
                    status="SUCCESS",
                )
            )
            await conn.execute(
                quotes.insert().values(
                    id=uuid7(),
                    instrument_id=r.id,
                    source_id=src,
                    bid=Decimal(bid),
                    ask=Decimal(ask),
                    last=Decimal(bid),
                    source_ts=now,
                    ingested_at=now,
                    fetch_run_id=run,
                )
            )


async def approve(c: httpx.AsyncClient, url: str, owner: helpers.TestUser, clock: object) -> None:
    review = await _post(c, f"{url}/review", {}, url)
    token = re.search(r'name="challenge" value="([^"]+)"', review.text).group(1)  # type: ignore[union-attr,attr-defined]
    phash = re.search(r'name="packet_hash" value="([^"]+)"', review.text).group(1)  # type: ignore[union-attr,attr-defined]
    clock.advance(seconds=30)  # type: ignore[attr-defined]
    code = mfa.code_at(owner.mfa_secret or "", clock.now())  # type: ignore[attr-defined]
    r = await c.post(
        f"{url}/decide",
        data={
            "csrf_token": csrf_from(review.text),
            "challenge": token,
            "packet_hash": phash,  # type: ignore[attr-defined]
            "decision": "APPROVE",
            "code": code,
        },
    )
    assert r.status_code == 303, r.text


async def test_owner_closes_a_protected_position(settings: Settings, db: Database) -> None:
    clock, ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    ctx.services["trading"] = app.state.trading
    registry = build_registry(ctx)
    owner = await make_user(db, clock)
    svc = app.state.trading
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        await _post(c, "/ops/mode", {"mode": "PAPER_TRADING", "reason": "paper period"}, "/ops")
        await _post(
            c,
            "/ops/paper-account",
            {"alias": "paper-main", "capital": "10000", "venue": "COINBASE"},
            "/ops",
        )
        await run_queued(ctx, registry)
        entry_url = (await _post(c, "/proposals", PROPOSAL, "/proposals/new")).headers["location"]  # type: ignore[attr-defined]
        await approve(c, entry_url, owner, clock)
        await svc.execution.run_cycle()
        await svc.paper.tick()
        await svc.execution.run_cycle()  # filled; protective stop placed
        page = (await c.get("/portfolio")).text
        assert "Propose closing" in page and "ACTIVE" in page
        await fresh_quotes(db, clock.now(), "82990.00", "83000.00")
        r = await c.post(
            "/portfolio/paper-main/exit",
            data={
                "csrf_token": csrf_from(page),
                "instrument_key": "SPOT:BTC/USD@COINBASE",
                "reason": "taking the paper profit",
            },
        )
        assert r.status_code == 303, r.text
        exit_url = r.headers["location"]
        detail = (await c.get(exit_url)).text
        entry_code = re.search(r"T-\d{8}-[0-9A-Z]{4}", (await c.get(entry_url)).text).group(0)  # type: ignore[union-attr]
        assert "Closes</th>" in detail and entry_code in detail
        assert "taking the paper profit" in detail
        await approve(c, exit_url, owner, clock)
        await fresh_quotes(db, clock.now(), "82990.00", "83000.00")
        await svc.execution.run_cycle()  # stop cancelled, exit sent
        await svc.paper.tick()
        await svc.execution.run_cycle()  # exit filled
        again = (await c.get("/portfolio")).text
        assert "Propose closing" not in again  # nothing left to close
        refused = await c.post(
            "/portfolio/paper-main/exit",
            data={
                "csrf_token": csrf_from(again),
                "instrument_key": "SPOT:BTC/USD@COINBASE",
                "reason": "nothing to sell",
            },
        )
        assert refused.status_code == 422
    async with db.connect() as conn:
        held = await conn.scalar(select(positions.c.quantity))
        by_purpose = {o.purpose: o.status for o in await conn.execute(select(orders))}
        sells = list(
            await conn.execute(select(trade_intents.c.status).where(trade_intents.c.side == "SELL"))
        )
    assert held == 0
    assert by_purpose == {"ENTRY": "FILLED", "STOP_LOSS": "CANCELLED", "EXIT": "FILLED"}
    assert [s.status for s in sells] == ["EXECUTED"]
