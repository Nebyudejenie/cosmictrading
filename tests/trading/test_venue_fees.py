"""Fees per venue (B-08): the owner's fee for a venue is what proposals estimate, what the cost
check charges and what paper fills cost; other venues keep the general assumption."""

from __future__ import annotations

from decimal import Decimal as D

from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_execution import fills, orders
from cosmic.trading.policy import active_policy, change_policy
from cosmic.trading.proposals import latest_packet
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.trading_env import Env, build_env


async def set_fees(env: Env, fees: list[dict[str, str]]) -> None:
    async with env.db.transaction() as conn:
        _, policy = await active_policy(conn)
        await change_policy(
            conn,
            principal=env.principal(),
            new_params={**policy.model_dump(), "venue_fees": fees},
            reason="the venue's fee is confirmed",
            now=env.clock.now(),
            settings=env.settings,
        )


async def test_proposals_and_paper_fills_use_the_venues_own_fee(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)
    await set_fees(env, [{"venue": "COINBASE", "bps": "10"}])
    p = await env.propose()
    assert p.status == "PROPOSED", p.problems
    async with env.db.connect() as conn:
        packet = (await latest_packet(conn, p.intent_id)).packet
    assert packet["costs"]["fee_bps"] == "10"
    assert (
        packet["costs"]["fee_basis"]
        == "ASSUMPTION: the owner's fee for COINBASE in the risk policy"
    )
    await env.approve(p.intent_id)
    await env.cycle(2)
    async with env.db.connect() as conn:
        fill = (
            await conn.execute(
                select(fills)
                .join(orders, orders.c.id == fills.c.order_id)
                .where(orders.c.purpose == "ENTRY")
            )
        ).one()
    assert fill.fee == (fill.quantity * fill.price * D(10) / D(10_000)).quantize(D("0.00000001"))
    result = await env.svc.reconciler.reconcile(env.account_id, trigger="test")
    assert result.passed, result.mismatches


async def test_without_a_venue_fee_the_general_assumption_applies(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)
    await set_fees(env, [{"venue": "BINANCE", "bps": "10"}])  # another venue
    p = await env.propose()
    async with env.db.connect() as conn:
        packet = (await latest_packet(conn, p.intent_id)).packet
        _, policy = await active_policy(conn)
    assert D(packet["costs"]["fee_bps"]) == policy.fee_bps_assumption
    assert "general fee" in packet["costs"]["fee_basis"]


async def test_the_risk_page_edits_fees_per_venue(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    app = create_app(settings, db=db, clock=env.clock)
    base = {"reason": "the venues' fees are confirmed"}
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        token = csrf_from((await c.get("/settings/risk")).text)
        for value, problem in (
            ("MOON=5", "fee for unknown venue MOON"),
            ("BINANCE", "write each as VENUE=BPS"),
            ("BINANCE=900", "less than or equal to 500"),
        ):
            r = await c.post(
                "/settings/risk", data={"csrf_token": token, **base, "venue_fees": value}
            )
            assert r.status_code == 422 and problem in r.text, (value, r.text[:300])
        r = await c.post(
            "/settings/risk",
            data={"csrf_token": token, **base, "venue_fees": "binance=10, HFM=0"},
        )
        assert r.status_code == 303, r.text
        page = (await c.get("/settings/risk")).text
        assert 'value="BINANCE=10, HFM=0"' in page
        r = await c.post(
            "/settings/risk",
            data={"csrf_token": csrf_from(page), **base, "venue_fees": "none"},
        )
        assert r.status_code == 303
    async with db.connect() as conn:
        version, policy = await active_policy(conn)
    assert version == 3 and policy.venue_fees == ()
