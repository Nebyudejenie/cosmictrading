"""Approved position management: stops, gaps, take-profit, unfilled cancel, protection failures."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, notifications
from cosmic.db.schema_execution import (
    order_events,
    orders,
    paper_orders,
    post_trade_reviews,
    trade_intents,
)
from cosmic.db.schema_trading import positions
from cosmic.trading.accounts import get_account
from cosmic.trading.context_builder import account_state
from tests.trading_env import Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def filled_with_stop(env: Env, **kw: object) -> object:
    p = await env.propose(**kw)
    assert p.status == "PROPOSED", p.problems
    await env.approve(p.intent_id)
    await env.cycle(2)
    return p


async def test_stop_loss_fills_and_creates_loss_review(env: Env) -> None:
    p = await filled_with_stop(env)
    env.clock.advance(seconds=5)
    await env.set_market("80990.00", "80990.50")  # through the 81,000 stop, above its 80,900 limit
    await env.cycle(2)
    async with env.db.connect() as conn:
        stop = (await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))).one()
        pos = (await conn.execute(select(positions))).one()
        review = (await conn.execute(select(post_trade_reviews))).one()
        acct = await get_account(conn, account_id=env.account_id)
        state = await account_state(conn, acct, env.clock.now())
    assert stop.status == "FILLED"
    assert pos.quantity == 0
    assert review.intent_id == p.intent_id  # type: ignore[attr-defined]
    assert review.realized_pnl < 0 and review.assessment["outcome"] == "LOSS"
    assert review.assessment["stayed_within_authorisation"] is True
    assert review.is_simulated is True
    assert state.loss_streak == 1
    result = await env.svc.reconciler.reconcile(env.account_id, trigger="test")
    assert result.passed, result.mismatches


async def test_gap_through_stop_limit_leaves_position_unprotected_honestly(env: Env) -> None:
    await filled_with_stop(env)
    env.clock.advance(seconds=5)
    await env.set_market("80500.00", "80500.50")  # gapped below the 80,900 limit
    await env.cycle(2)
    async with env.db.connect() as conn:
        stop = (await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))).one()
        pos = (await conn.execute(select(positions))).one()
        venue = (
            await conn.execute(
                select(paper_orders).where(paper_orders.c.order_type == "STOP_LIMIT")
            )
        ).one()
    assert venue.triggered is False or venue.status == "OPEN"
    assert stop.status == "ACKNOWLEDGED"  # resting, not filled: a stop is not a guaranteed exit
    assert pos.quantity == D("0.004")


async def test_take_profit_cancels_stop_then_sells(env: Env) -> None:
    p = await filled_with_stop(
        env,
        management_plan={
            "stop_loss": {"stop_price": D("81000.00"), "limit_price": D("80900.00")},
            "take_profit_price": D("85000.00"),
        },
    )
    env.clock.advance(seconds=5)
    await env.set_market("85010.00", "85010.50")
    await env.cycle(3)  # cancel stop → confirm → place TP IOC → fill
    async with env.db.connect() as conn:
        rows = {o.purpose: o for o in await conn.execute(select(orders))}
        pos = (await conn.execute(select(positions))).one()
        review = (await conn.execute(select(post_trade_reviews))).one()
    assert rows["STOP_LOSS"].status == "CANCELLED"
    assert rows["TAKE_PROFIT"].status == "FILLED" and rows["TAKE_PROFIT"].reduce_only
    assert pos.quantity == 0
    assert review.realized_pnl > 0 and review.assessment["outcome"] == "GAIN"
    assert review.intent_id == p.intent_id  # type: ignore[attr-defined]


async def test_unfilled_entry_cancelled_after_approved_time(env: Env) -> None:
    p = await env.propose(
        limit_price=D("82700.00"), management_plan={"cancel_unfilled_after_seconds": 120}
    )
    await env.approve(p.intent_id)
    await env.cycle(2)  # resting below the market
    env.clock.advance(seconds=121)
    await env.set_market("82986.50", "82986.51")
    await env.cycle()
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders))).one()
        intent = (await conn.execute(select(trade_intents.c.status))).scalar_one()
    assert entry.status == "CANCELLED" and entry.filled_qty == 0
    assert intent == "EXECUTION_FAILED"


async def test_protective_rejection_degrades_and_retries_boundedly(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle()  # entry acknowledged
    await env.svc.paper.inject_fault("REJECT_NEXT", 10)  # every stop placement is rejected
    for _ in range(6):
        await env.cycle()
    async with env.db.connect() as conn:
        stops = list(await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS")))
        pos = (await conn.execute(select(positions))).one()
        inc = (
            await conn.execute(
                select(incidents).where(
                    incidents.c.dedup_key == f"trading.protection_degraded:{p.intent_id}"
                )
            )
        ).one()
        alerts = [n.kind for n in await conn.execute(select(notifications))]
    assert all(s.status == "REJECTED" for s in stops)
    assert len(stops) == 3  # initial + protective_retry_max (2): bounded, never widened
    assert {s.stop_price for s in stops} == {D("81000.00")}
    assert pos.protection_state == "DEGRADED"
    assert inc.severity == "CRITICAL"
    assert "protection_degraded" in alerts


async def test_repropose_after_expired_approval(env: Env) -> None:
    from cosmic.audit.service import Actor
    from cosmic.trading.approvals import expire_sweep
    from cosmic.trading.proposals import repropose

    p = await env.propose()
    await env.approve(p.intent_id)
    env.clock.advance(seconds=901)
    async with env.db.transaction() as conn:
        await expire_sweep(conn, now=env.clock.now())
    await env.set_market("82986.50", "82986.51")
    stale = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2508.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={},
    )
    assert any(x.startswith("R03") for x in stale.problems)  # last reconciliation older than 900 s
    assert (await env.svc.reconciler.reconcile(env.account_id, trigger="scheduled")).passed
    async with env.db.transaction() as conn:
        r = await repropose(
            conn, intent_id=p.intent_id, actor=Actor.user(env.owner.user_id), now=env.clock.now()
        )
    assert r.status == "PROPOSED" and r.packet_hash != p.packet_hash
    await env.approve(p.intent_id)
    await env.cycle(2)
    async with env.db.connect() as conn:
        assert (
            await conn.execute(select(orders.c.status).where(orders.c.purpose == "ENTRY"))
        ).scalar_one() == "FILLED"


async def test_cancel_fill_race_is_resolved_from_the_venue(env: Env) -> None:
    """TRD-41: a cancel request is not a cancellation. The venue fills the resting entry just
    before our cancel arrives; the order must end FILLED (broker truth), never CANCELLED."""
    p = await env.propose(limit_price=D("82700.00"))
    await env.approve(p.intent_id)
    await env.cycle(2)  # resting below the market
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders))).one()
    assert entry.status == "ACKNOWLEDGED"
    env.clock.advance(seconds=5)
    await env.set_market("82690.00", "82699.00")  # the market comes to the order …
    await env.svc.paper.tick()  # … and the venue fills it before our cancel arrives
    outcome = await env.svc.execution.request_cancel(entry.id, reason="operator cancel")
    assert outcome.code == "CANCEL_PENDING"  # we asked; we did not cancel
    await env.cycle()
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.id == entry.id))).one()
        events = [
            (e.to_status, e.source)
            for e in await conn.execute(
                select(order_events)
                .where(order_events.c.order_id == entry.id)
                .order_by(order_events.c.seq)
            )
        ]
        pos = (await conn.execute(select(positions))).one()
    assert entry.status == "FILLED" and entry.filled_qty == D("0.004")
    assert ("CANCEL_PENDING", "LOCAL") in events and events[-1] == ("FILLED", "BROKER")
    assert "CANCELLED" not in {s for s, _ in events}
    assert pos.quantity == D("0.004")  # the fill is real exposure


async def test_venue_overfill_is_refused_and_blocks_the_account(env: Env) -> None:
    """A venue report of more fills than the order quantity is never applied: the account
    is marked reconciliation FAILED (blocking new exposure) and a complete alert is sent."""
    import uuid as _uuid

    from cosmic.db.schema_execution import paper_fills
    from cosmic.notify.alerts import alert_from_payload

    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(2)  # filled
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.purpose == "ENTRY"))).one()
        po = (
            await conn.execute(
                select(paper_orders).where(paper_orders.c.client_order_id == entry.client_order_id)
            )
        ).one()
    assert entry.status == "FILLED"
    async with env.db.transaction() as conn:  # a venue-side bug: one extra fill report
        await conn.execute(
            paper_fills.insert().values(
                id=_uuid.uuid4(),
                paper_order_id=po.id,
                venue_fill_id="bogus-extra-fill",
                quantity=D("0.004"),
                price=D("83000.00"),
                fee=D("0"),
                fee_asset="USD",
                liquidity="TAKER",
                filled_at=env.clock.now(),
            )
        )
    await env.svc.execution.sync_order(entry.id)
    async with env.db.connect() as conn:
        entry_after = (await conn.execute(select(orders).where(orders.c.id == entry.id))).one()
        pos = (await conn.execute(select(positions))).one()
        acct = await get_account(conn, account_id=env.account_id)
        alerts = [
            n.payload
            for n in await conn.execute(select(notifications))
            if n.payload.get("category") == "ACCOUNTING"
        ]
    assert entry_after.filled_qty == D("0.004") and pos.quantity == D("0.004")  # not applied
    assert acct.reconciliation_status == "FAILED"
    assert alerts and alert_from_payload(alerts[0]) is not None
    assert "NOT applied" in alerts[0]["alert"]["action_taken"]
