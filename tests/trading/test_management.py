"""Approved position management: stops, gaps, take-profit, unfilled cancel, protection failures."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, incidents, notifications
from cosmic.db.schema_execution import (
    order_events,
    orders,
    paper_orders,
    post_trade_reviews,
    trade_intents,
)
from cosmic.db.schema_trading import positions
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.base import BrokerOrderNotFoundError, BrokerUnavailableError
from cosmic.trading.context_builder import account_state
from cosmic.trading.orders import Source
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
    # QNT-11: the parts add up to the realised result, and each has the expected sign
    a = review.actual["attribution"]
    assert a["complete"] is True
    assert abs(D(a["residual"])) < D("0.00000001")
    assert D(a["market_move_thesis_and_luck"]) < 0  # the market fell through the stop
    assert D(a["entry_execution"]) <= 0 and D(a["exit_execution"]) <= 0 and D(a["fees"]) < 0
    sizing = review.assessment["sizing"]
    assert D(sizing["filled_quantity"]) == D(sizing["approved_quantity"]) == D("0.004")
    assert sizing["loss_vs_planned_max"] is not None
    assert D(sizing["loss_vs_planned_max"]) <= D("1.00")  # stop held: loss within the plan
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
        limit_price=D("82700.00"),
        management_plan={
            "stop_loss": {"stop_price": D("81000.00"), "limit_price": D("80900.00")},
            "cancel_unfilled_after_seconds": 120,
        },  # without a stop, its whole value would be open risk (R19 refuses above 3 %)
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


async def test_review_is_shown_on_the_trade_page(env: Env) -> None:
    from cosmic.identity.capabilities import Role
    from cosmic.web.app import create_app
    from tests.helpers import client_for, full_login, make_user

    p = await filled_with_stop(env)
    env.clock.advance(seconds=5)
    await env.set_market("80990.00", "80990.50")
    await env.cycle(2)
    app = create_app(env.settings, db=env.db, clock=env.clock)
    owner = await make_user(env.db, env.clock, username="owner2", roles=[Role.OWNER])
    async with client_for(app) as c:
        await full_login(c, owner, env.clock)
        page = (await c.get(f"/approvals/{p.intent_id}")).text
        api = (await c.get(f"/api/v1/intents/{p.intent_id}")).json()
    assert "Post-trade review" in page and "Market move while held" in page
    assert "LOSS" in page and "SIMULATED" in page
    assert api["review"]["actual"]["attribution"]["complete"] is True


async def test_a_trade_without_a_stop_counts_its_whole_value_as_open_risk(env: Env) -> None:
    """R19 (directive Phase 5): with no stop, the whole position is at risk. 0.004 BTC at
    83,000 is 3.32 % of the 10,000 account, above the default 3 %; 0.003 BTC is 2.49 %."""
    big = await env.propose(management_plan={})
    assert big.status == "NON_EXECUTABLE"
    assert any(p.startswith("R19") and "3.32% of equity" in p for p in big.problems), big.problems
    small = await env.propose(management_plan={}, quantity=D("0.003"))
    assert small.status == "PROPOSED", small.problems


async def test_a_venue_rejection_of_an_approved_entry_is_final_and_reported(env: Env) -> None:
    """Directive Phase 11: the venue refuses the entry itself. The order is REJECTED with the
    venue's reason, the trade EXECUTION_FAILED, the owner is told, and it is never re-sent."""
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.svc.paper.inject_fault("REJECT_NEXT")
    await env.cycle(3)
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.purpose == "ENTRY"))).one()
        intent = (
            await conn.execute(
                select(trade_intents.c.status, trade_intents.c.status_reason).where(
                    trade_intents.c.id == p.intent_id
                )
            )
        ).one()
        told = list(
            await conn.execute(
                select(notifications.c.kind).where(notifications.c.kind == "order_rejected")
            )
        )
    assert entry.status == "REJECTED"
    assert intent.status == "EXECUTION_FAILED" and "venue rejected" in intent.status_reason
    assert told  # an order_rejected notification
    assert await env.count(paper_orders) == 1  # sent once, never re-sent


async def test_a_stop_ended_at_the_venue_is_reported_and_replaced(env: Env) -> None:
    """Directive Phase 11: the stop disappears at the venue without our asking. The owner is
    told (it may mean someone else has access), and a replacement under the same plan protects
    the position again. Cancels the system asks for itself raise nothing."""
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(2)
    async with env.db.connect() as conn:
        sl1 = (await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))).one()
    await env.svc.paper.cancel_order(env.account_id, sl1.client_order_id)  # not by us
    await env.cycle(3)
    async with env.db.connect() as conn:
        stops = {
            o.client_order_id: o
            for o in await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))
        }
        pos = (await conn.execute(select(positions))).one()
        inc = (
            await conn.execute(
                select(incidents).where(
                    incidents.c.dedup_key == f"trading.protection_ended_at_venue:{sl1.id}"
                )
            )
        ).one()
    assert stops[sl1.client_order_id].status == "CANCELLED"
    replacement = next(o for k, o in stops.items() if k != sl1.client_order_id)
    assert replacement.status == "ACKNOWLEDGED" and replacement.stop_price == sl1.stop_price
    assert pos.protection_state == "ACTIVE"
    assert inc.severity == "WARNING" and "not by COSMICTRADING" in inc.title
    await env.svc.execution.request_cancel(replacement.id, reason="test: a cancel we ask for")
    await env.cycle()
    assert (
        await env.count(
            incidents,
            incidents.c.dedup_key == f"trading.protection_ended_at_venue:{replacement.id}",
        )
        == 0
    )


async def test_an_approved_entry_that_cannot_reach_the_venue_is_reported(env: Env) -> None:
    """The venue is unavailable when the approved entry goes out: it is definitely not sent,
    the trade fails, and the owner is told why (before, only the portal showed it)."""
    from cosmic.notify.telegram.runner import notification_text

    p = await env.propose()
    await env.approve(p.intent_id)
    await env.svc.paper.inject_fault("UNAVAILABLE")
    await env.cycle()
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.purpose == "ENTRY"))).one()
        intent = (
            await conn.execute(
                select(trade_intents.c.status).where(trade_intents.c.id == p.intent_id)
            )
        ).scalar_one()
        note = (
            await conn.execute(
                select(notifications.c.payload).where(
                    notifications.c.kind == "order_rejected", notifications.c.channel == "PORTAL"
                )
            )
        ).scalar_one()
    assert entry.status == "REJECTED" and intent == "EXECUTION_FAILED"
    assert await env.count(paper_orders) == 0  # it never reached the venue
    text = notification_text("order_rejected", note)
    assert "REJECTED" in text and "the venue was unavailable" in text


async def test_cash_and_minimum_size_refuse_a_proposal_end_to_end(
    settings: Settings, db: Database
) -> None:
    """Directive Phase 11: insufficient funds (R07) and a quantity below the venue minimum
    (R16) make a real proposal NON_EXECUTABLE, with the reason."""
    small = await build_env(settings, db, capital="300")
    p = await small.propose()  # 0.004 BTC ≈ 332 USD plus fees, with 300 USD of cash
    assert p.status == "NON_EXECUTABLE"
    assert any(x.startswith("R07") and "free cash" in x for x in p.problems), p.problems
    tiny = await small.propose(quantity=D("0.00001"))  # about 0.83 USD: below the minimum
    assert tiny.status == "NON_EXECUTABLE"
    assert any(x.startswith("R16") for x in tiny.problems), tiny.problems


async def _resting_entry(env: Env) -> uuid.UUID:
    p = await env.propose(
        limit_price=D("82700.00"),
        management_plan={"stop_loss": {"stop_price": D("81000.00"), "limit_price": D("80900.00")}},
    )
    await env.approve(p.intent_id)
    await env.cycle(2)  # resting below the market
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders.c.id, orders.c.status))).one()
    assert entry.status == "ACKNOWLEDGED"
    return entry.id


def _cancels_fail(
    env: Env, monkeypatch: pytest.MonkeyPatch, *, lost: int = 0, until: datetime | None = None
) -> list[str]:
    """Cancel requests that never reach the venue: the first `lost` ones, or all of them until
    the clock reaches `until`."""
    real = env.svc.paper.cancel_order
    calls: list[str] = []

    async def cancel(account_ref, client_order_id):  # type: ignore[no-untyped-def]
        calls.append(client_order_id)
        if len(calls) <= lost or (until is not None and env.clock.now() < until):
            raise BrokerUnavailableError("the venue did not answer")
        await real(account_ref, client_order_id)

    monkeypatch.setattr(env.svc.paper, "cancel_order", cancel)
    return calls


async def _open_alerts(env: Env, prefix: str) -> dict[str, str]:
    async with env.db.connect() as conn:
        rows = await conn.execute(
            select(incidents.c.dedup_key, incidents.c.severity).where(
                incidents.c.status != "RESOLVED", incidents.c.dedup_key.like(f"{prefix}%")
            )
        )
        return {r.dedup_key: r.severity for r in rows}


async def _minutes(env: Env, n: int) -> None:
    for _ in range(n):
        env.clock.advance(seconds=61)
        await env.cycle()


async def test_a_cancel_that_never_reached_the_venue_is_sent_again(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emergency stop's cancel is lost on the way. The order stays open at the venue, so
    the cancel is sent again a minute later, and the venue confirms it."""
    from cosmic.audit.service import Actor

    order_id = await _resting_entry(env)
    calls = _cancels_fail(env, monkeypatch, lost=1)
    await env.svc.emergency_stop(actor=Actor.user(env.owner.user_id), reason="test")
    await env.cycle()  # within the minute: not sent again yet
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "CANCEL_PENDING"
        assert (await conn.scalar(select(paper_orders.c.status))) == "OPEN"
    await _minutes(env, 1)
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.id == order_id))).one()
        resent = await conn.scalar(
            select(func.count())
            .select_from(audit_events)
            .where(audit_events.c.action == "trade.cancel_resent")
        )
    assert len(calls) == 2 and resent == 1
    assert entry.status == "CANCELLED" and entry.filled_qty == 0


async def test_a_cancel_the_venue_never_confirms_is_reported_and_kept_up(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five sends a minute apart, then one CRITICAL incident for the owner (an open entry can
    still fill), and the cancel is still sent every five minutes. The incident closes by itself
    once the venue reports the order finished."""
    order_id = await _resting_entry(env)
    calls = _cancels_fail(env, monkeypatch, lost=1000)
    await env.svc.execution.request_cancel(order_id, reason="operator cancel")
    await _minutes(env, 7)
    assert len(calls) == 5  # the request and four re-sends, a minute apart
    key = f"trading.cancel_unconfirmed:{order_id}"
    assert await _open_alerts(env, "trading.cancel_unconfirmed:") == {key: "CRITICAL"}
    env.clock.advance(minutes=5)
    await env.cycle()
    assert len(calls) == 6  # still sent, now every five minutes
    monkeypatch.delattr(env.svc.paper, "cancel_order")  # the venue answers again
    async with env.db.connect() as conn:
        account = await get_account(conn, account_id=env.account_id)
        coid = await conn.scalar(select(orders.c.client_order_id))
    await env.svc.paper.cancel_order(account.id, coid)  # the owner cancels it in the venue's app
    await env.cycle()
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "CANCELLED"
    assert await _open_alerts(env, "trading.cancel_unconfirmed:") == {}


async def test_a_long_outage_does_not_end_the_cancel_attempts(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The venue cannot be reached for six minutes: every early send fails, the owner is told,
    and the next send after the outage cancels the order; the incident then closes."""
    order_id = await _resting_entry(env)
    calls = _cancels_fail(env, monkeypatch, until=env.clock.now() + timedelta(minutes=6))
    await env.svc.execution.request_cancel(order_id, reason="operator cancel")
    await _minutes(env, 5)
    assert await _open_alerts(env, "trading.cancel_unconfirmed:") != {}
    await _minutes(env, 5)
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.id == order_id))).one()
    assert entry.status == "CANCELLED" and len(calls) == 6
    assert await _open_alerts(env, "trading.cancel_unconfirmed:") == {}


async def test_an_order_settled_meanwhile_gets_no_fresh_alert(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is settled (the owner cancels it at the venue, a reconciliation records it) just
    before the follow-up raises its alert. The alert is not raised for a finished order."""
    order_id = await _resting_entry(env)
    real_cancel = env.svc.paper.cancel_order
    _cancels_fail(env, monkeypatch, lost=1000)
    await env.svc.execution.request_cancel(order_id, reason="operator cancel")
    await _minutes(env, 4)  # five sends, none confirmed
    real_alert = env.svc.execution._cancel_unconfirmed

    async def settled_first(p, sends, now):  # type: ignore[no-untyped-def]
        await real_cancel(env.account_id, p.client_order_id)
        await env.svc.execution.sync_order(p.id)
        await real_alert(p, sends, now)

    monkeypatch.setattr(env.svc.execution, "_cancel_unconfirmed", settled_first)
    await _minutes(env, 1)
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "CANCELLED"
        opened = await conn.scalar(
            select(func.count())
            .select_from(incidents)
            .where(incidents.c.dedup_key.like("trading.cancel_unconfirmed:%"))
        )
    assert opened == 0


async def _forget_at_venue(env: Env, purpose: str) -> uuid.UUID:
    """The venue loses the order, as the Binance test network does when it resets (the paper
    venue also releases what the order held, as a reset does)."""
    async with env.db.transaction() as conn:
        o = (await conn.execute(select(orders).where(orders.c.purpose == purpose))).one()
        row = (
            await conn.execute(
                select(paper_orders).where(paper_orders.c.client_order_id == o.client_order_id)
            )
        ).one()
        await env.svc.paper._close(conn, row, "CANCELLED")  # releases its hold
        await conn.execute(paper_orders.delete().where(paper_orders.c.id == row.id))
    return uuid.UUID(str(o.id))


async def test_an_order_the_venue_no_longer_has_is_ended_and_reported(env: Env) -> None:
    """An acknowledged entry disappears at the venue. After a minute of 'no such order' it is
    recorded as EXPIRED (it can no longer fill), with a WARNING incident; nothing is invented."""
    await _resting_entry(env)
    order_id = await _forget_at_venue(env, "ENTRY")
    await env.cycle()  # the venue confirmed it moments ago: not yet
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "ACKNOWLEDGED"
    await _minutes(env, 1)
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.id == order_id))).one()
        intent = await conn.scalar(select(trade_intents.c.status))
    assert entry.status == "EXPIRED" and entry.filled_qty == 0 and intent == "EXECUTION_FAILED"
    assert await _open_alerts(env, "trading.order_missing_at_venue:") == {
        f"trading.order_missing_at_venue:{order_id}": "WARNING"
    }


async def test_one_no_such_order_answer_after_a_silence_does_not_end_the_order(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is heard from the venue for ten minutes, then one stray "no such order". The
    minute is counted from the first such answer, not from the last confirmation, so the order
    stays open; the venue knows it again on the next sync."""
    await _resting_entry(env)
    env.clock.advance(minutes=10)
    real = env.svc.paper.get_order
    stray = [True]

    async def once_not_found(account_ref, client_order_id):  # type: ignore[no-untyped-def]
        if stray:
            stray.pop()
            raise BrokerOrderNotFoundError(client_order_id)
        return await real(account_ref, client_order_id)

    monkeypatch.setattr(env.svc.paper, "get_order", once_not_found)
    await env.cycle()
    await _minutes(env, 2)
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "ACKNOWLEDGED"
    assert not stray and await _open_alerts(env, "trading.order_missing_at_venue:") == {}


async def test_a_sighting_between_two_stray_answers_resets_the_minute(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "No such order" at 0 s, the order seen again at 30 s, "no such order" at 70 s: two stray
    answers more than a minute apart, with a sighting between, never end an open order."""
    await _resting_entry(env)
    real = env.svc.paper.get_order
    script = ["missing", "seen", "missing"]

    async def scripted(account_ref, client_order_id):  # type: ignore[no-untyped-def]
        if script and script.pop(0) == "missing":
            raise BrokerOrderNotFoundError(client_order_id)
        return await real(account_ref, client_order_id)

    monkeypatch.setattr(env.svc.paper, "get_order", scripted)
    for wait in (0, 30, 40, 5):
        env.clock.advance(seconds=wait)
        await env.cycle()
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "ACKNOWLEDGED"
    assert not script and await _open_alerts(env, "trading.order_missing_at_venue:") == {}


async def test_a_cancel_for_an_order_the_venue_no_longer_has_ends_without_a_futile_alert(
    env: Env,
) -> None:
    """The cancel is answered 'no such order'. The order is recorded as CANCELLED once that has
    held for a minute, instead of being sent five cancels and an alert nobody can act on."""
    order_id = await _resting_entry(env)
    await _forget_at_venue(env, "ENTRY")
    await env.svc.execution.request_cancel(order_id, reason="operator cancel")
    await _minutes(env, 6)
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "CANCELLED"
    assert await _open_alerts(env, "trading.cancel_unconfirmed:") == {}
    assert list(await _open_alerts(env, "trading.order_missing_at_venue:")) == [
        f"trading.order_missing_at_venue:{order_id}"
    ]


async def test_a_stop_the_venue_no_longer_has_is_replaced(env: Env) -> None:
    """A test-network reset wipes the protective stop. It is recorded as EXPIRED and a
    replacement at the same level protects the position again."""
    await filled_with_stop(env)
    sl1 = await _forget_at_venue(env, "STOP_LOSS")
    await env.cycle()  # the first "no such order"
    env.clock.advance(seconds=61)
    await env.set_market("82986.50", "82986.51")
    await env.cycle()
    async with env.db.connect() as conn:
        stops = list(await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS")))
        pos = (await conn.execute(select(positions))).one()
    old = next(o for o in stops if o.id == sl1)
    new = next(o for o in stops if o.id != sl1)
    assert old.status == "EXPIRED" and new.status == "ACKNOWLEDGED"
    assert new.stop_price == old.stop_price and pos.protection_state == "ACTIVE"
    assert list(await _open_alerts(env, "trading.")) == [f"trading.order_missing_at_venue:{sl1}"]


async def test_one_positions_failure_does_not_leave_the_others_unprotected(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Managing one position fails; the other still gets its stop, and the cycle reports the
    failure."""
    btc = await env.propose()
    eth = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2510.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={"stop_loss": {"stop_price": D("2450.00"), "limit_price": D("2440.00")}},
    )
    for p in (btc, eth):
        assert p.status == "PROPOSED", p.problems
        await env.approve(p.intent_id)
    await env.cycle()  # both sent
    real = env.svc.execution._manage_intent
    failed: list[uuid.UUID] = []

    async def first_one_breaks(intent):  # type: ignore[no-untyped-def]
        if not failed or intent.id == failed[0]:
            failed.append(intent.id)
            raise RuntimeError("this position's data is broken")
        return await real(intent)

    monkeypatch.setattr(env.svc.execution, "_manage_intent", first_one_breaks)
    with pytest.raises(RuntimeError, match="data is broken"):
        await env.cycle()  # both fill; the one managed first fails
    async with env.db.connect() as conn:
        protected = {
            r.intent_id
            for r in await conn.execute(
                select(orders.c.intent_id).where(orders.c.purpose == "STOP_LOSS")
            )
        }
    assert protected == {btc.intent_id, eth.intent_id} - {failed[0]}


async def test_an_account_without_its_adapter_is_left_alone_and_records_no_cancel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker runs without the adapter an account needs (for example without the
    test-network keys). Its positions are not managed here, and a cancel asked for it records
    nothing: no cancel shown as requested when none could be sent. Found by review."""
    p = await env.propose(
        limit_price=D("82700.00"),
        management_plan={
            "stop_loss": {"stop_price": D("81000.00"), "limit_price": D("80900.00")},
            "cancel_unfilled_after_seconds": 60,
        },
    )
    await env.approve(p.intent_id)
    await env.cycle(2)
    async with env.db.connect() as conn:
        order_id = (await conn.execute(select(orders.c.id))).scalar_one()

    def no_adapter(account):  # type: ignore[no-untyped-def]
        raise ConflictError("the Binance test-network adapter is not available in this process")

    monkeypatch.setattr(env.svc.brokers, "for_account", no_adapter)
    env.clock.advance(seconds=61)  # the plan's time limit has passed
    with pytest.raises(ConflictError, match="not available"):
        await env.svc.execution.run_cycle()  # its orders cannot be synced: reported
    with pytest.raises(ConflictError, match="not available"):
        await env.svc.execution.request_cancel(order_id, reason="operator cancel")
    async with env.db.connect() as conn:
        status = await conn.scalar(select(orders.c.status))
        requested = await conn.scalar(
            select(func.count())
            .select_from(audit_events)
            .where(audit_events.c.action == "trade.cancel_requested")
        )
    assert status == "ACKNOWLEDGED" and requested == 0
    assert "managed" not in env.svc.execution.step_failures  # its positions were left alone
    monkeypatch.delattr(env.svc.brokers, "for_account")  # the adapter is back
    await env.cycle()
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "CANCELLED"


async def test_a_step_that_keeps_failing_raises_its_own_incident(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Job health can miss a failing cycle (a run that is not the leader returns normally), so
    the cycle itself raises a CRITICAL incident after three failures of a step in a row, and
    closes it when the step works again. Found by review."""

    async def broken() -> int:
        raise RuntimeError("protection step broke")

    monkeypatch.setattr(env.svc.execution, "manage_positions", broken)
    for expected in ({}, {}, {"trading.cycle_step_failing:managed": "CRITICAL"}):
        with pytest.raises(RuntimeError, match="protection step broke"):
            await env.svc.execution.run_cycle()
        assert await _open_alerts(env, "trading.cycle_step_failing:") == expected
    monkeypatch.delattr(env.svc.execution, "manage_positions")
    await env.svc.execution.run_cycle()
    assert await _open_alerts(env, "trading.cycle_step_failing:") == {}


async def test_an_order_cancelled_while_reconciliation_runs_is_no_mismatch(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciliation reads the venue's open orders, then the stop's cancel settles an entry
    before reconciliation reads our records. Asked again, the venue says it is cancelled, so it
    is no mismatch and raises no 'Reconciliation failed'. Found by review."""
    from cosmic.audit.service import Actor
    from cosmic.trading import killswitch

    p = await env.propose(limit_price=D("82700.00"))
    await env.approve(p.intent_id)
    await env.cycle(2)
    async with env.db.transaction() as conn:  # the stop, without its own immediate cancels
        await killswitch.activate(
            conn,
            actor=Actor.user(env.owner.user_id),
            principal=env.principal(),
            reason="test stop",
            now=env.clock.now(),
        )
    real_fills = env.svc.paper.get_fills
    cancelled: list[int] = []

    async def fills_meanwhile(account_ref, since):  # type: ignore[no-untyped-def]
        if since is None and not cancelled:  # reconciliation's own read, after open orders
            cancelled.append(await env.svc.execution.cancel_entries_under_stop())
        return await real_fills(account_ref, since)

    monkeypatch.setattr(env.svc.paper, "get_fills", fills_meanwhile)
    result = await env.svc.reconciler.reconcile(env.account_id, trigger="emergency_stop")
    async with env.db.connect() as conn:
        assert (await conn.scalar(select(orders.c.status))) == "CANCELLED"
    assert cancelled == [1] and result.passed, result.mismatches
    assert await _open_alerts(env, "accounting.reconciliation_failed:") == {}


async def test_one_exits_failure_does_not_hold_back_the_others(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two approved exits; releasing the first one fails. The second still goes out, and the
    cycle reports the failure. Found by review."""
    from cosmic.audit.service import Actor
    from cosmic.market import data as md
    from cosmic.trading.exits import propose_close

    btc = await env.propose()
    eth = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2510.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={"stop_loss": {"stop_price": D("2450.00"), "limit_price": D("2440.00")}},
    )
    for p in (btc, eth):
        await env.approve(p.intent_id)
    await env.cycle(2)  # both filled, each with its stop
    for key in ("SPOT:BTC/USD@COINBASE", "SPOT:ETH/USD@COINBASE"):
        async with env.db.transaction() as conn:
            exit_ = await propose_close(
                conn,
                account=await get_account(conn, account_id=env.account_id),
                inst=await md.instrument_by_key(conn, key),
                reason="close for the test",
                actor=Actor.user(env.owner.user_id),
                now=env.clock.now(),
            )
        assert exit_.status == "PROPOSED", exit_.problems
        await env.approve(exit_.intent_id)
    real = env.svc.execution._release_exit

    async def hold(order_id):  # type: ignore[no-untyped-def]
        return 0  # not yet: both exits wait, as when their stops' cancels are unconfirmed

    monkeypatch.setattr(env.svc.execution, "_release_exit", hold)
    await env.cycle()  # both exits authorised, neither released
    failed: list[uuid.UUID] = []

    async def first_one_breaks(order_id):  # type: ignore[no-untyped-def]
        if not failed or order_id == failed[0]:
            failed.append(order_id)
            raise RuntimeError("this exit's data is broken")
        return await real(order_id)

    monkeypatch.setattr(env.svc.execution, "_release_exit", first_one_breaks)
    with pytest.raises(RuntimeError, match="data is broken"):
        await env.svc.execution.release_exits()  # the cycle's step: both exits in one pass
    async with env.db.connect() as conn:
        exits = {
            r.id: r.status
            for r in await conn.execute(
                select(orders.c.id, orders.c.status).where(orders.c.purpose == "EXIT")
            )
        }
    assert len(exits) == 2 and exits.pop(failed[0]) == "AUTHORIZED"
    assert list(exits.values()) != ["AUTHORIZED"]  # the other one was sent


async def test_one_orders_failing_sync_does_not_stop_the_others(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Syncing one order fails (for example its account's adapter is missing in the worker);
    the other orders are still synced, so their fills are recorded and protected."""
    btc = await env.propose()
    eth = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2510.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={"stop_loss": {"stop_price": D("2450.00"), "limit_price": D("2440.00")}},
    )
    for p in (btc, eth):
        await env.approve(p.intent_id)
    await env.cycle()  # both sent and acknowledged
    real = env.svc.execution.sync_order
    broken: list[uuid.UUID] = []

    async def first_one_breaks(order_id, *, source=Source.BROKER):  # type: ignore[no-untyped-def]
        if not broken or order_id == broken[0]:
            broken.append(order_id)
            raise ConflictError("the adapter is not available in this process")
        return await real(order_id, source=source)

    monkeypatch.setattr(env.svc.execution, "sync_order", first_one_breaks)
    with pytest.raises(ConflictError, match="not available"):
        await env.cycle()  # both fill at the venue; only one can be synced
    async with env.db.connect() as conn:
        entries = {
            r.id: r.status
            for r in await conn.execute(
                select(orders.c.id, orders.c.status).where(orders.c.purpose == "ENTRY")
            )
        }
    assert entries.pop(broken[0]) == "ACKNOWLEDGED"
    assert list(entries.values()) == ["FILLED"]
