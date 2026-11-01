"""The gap guard (S42): a stop-limit the price has fallen below cannot fill. The system says so
once, marks the protection DEGRADED and puts an exit before the owner; it never widens the stop
by itself (V6 §1822), and stands down when the price comes back."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, notifications
from cosmic.db.schema_execution import orders, trade_intents
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.trading.accounts import get_account
from cosmic.trading.execution.protection import gap_key
from cosmic.trading.exits import propose_close
from tests.trading_env import BTC, Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def _held_with_its_stop(env: Env) -> Any:
    """A filled BTC buy at 83,000 on paper, protected by its stop-limit (stop 81,000, limit
    80,900)."""
    env.clock.advance(seconds=1)
    await env.set_market("82990.00", "83000.00")
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(3)
    async with env.db.connect() as conn:
        stop = (await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))).one()
    assert (stop.stop_price, stop.limit_price) == (D("81000.00"), D("80900.00"))
    return p


async def _state(env: Env, intent_id: Any) -> dict[str, Any]:
    async with env.db.connect() as conn:
        incident = (
            await conn.execute(select(incidents).where(incidents.c.dedup_key == gap_key(intent_id)))
        ).first()
        exits = list(
            await conn.execute(
                select(trade_intents.c.id, trade_intents.c.trade_code, trade_intents.c.status)
                .where(trade_intents.c.side == "SELL")
                .order_by(trade_intents.c.created_at)
            )
        )
        asked = [
            str(r.payload)
            for r in await conn.execute(
                select(notifications.c.payload).where(
                    notifications.c.kind == "approval_requested",
                    notifications.c.channel == "PORTAL",
                )
            )
        ]
        return {
            "incident": incident,
            "protection": await conn.scalar(select(positions.c.protection_state)),
            "held": await conn.scalar(select(positions.c.quantity)),
            "exits": exits,
            "asked": asked,
            "stops": [
                r.status
                for r in await conn.execute(
                    select(orders.c.status)
                    .where(orders.c.purpose == "STOP_LOSS")
                    .order_by(orders.c.created_at)
                )
            ],
        }


async def test_a_gap_below_the_stops_limit_is_told_once_with_an_exit_to_approve(env: Env) -> None:
    p = await _held_with_its_stop(env)
    before = await _state(env, p.intent_id)
    assert before["incident"] is None and before["protection"] == "ACTIVE"
    env.clock.advance(seconds=30)
    await env.set_market("80700.00", "80710.00")  # through the stop and below its limit
    await env.cycle(1)
    gapped = await _state(env, p.intent_id)
    assert gapped["stops"] == ["ACKNOWLEDGED"]  # it cannot fill: the bid is below its limit
    assert gapped["protection"] == "DEGRADED"
    incident = gapped["incident"]
    assert incident is not None and incident.severity == "WARNING"  # paper money
    assert "fell through the stop" in incident.title
    [exit_] = gapped["exits"]
    assert exit_.status == "PROPOSED"  # an exit for the owner to approve, never sold by itself
    assert any(exit_.trade_code in a for a in gapped["asked"])
    env.clock.advance(seconds=30)
    await env.set_market("80650.00", "80660.00")
    await env.cycle(1)  # still below: no second alert, no second exit
    again = await _state(env, p.intent_id)
    assert len(again["exits"]) == 1
    assert again["incident"].occurrences == incident.occurrences
    await env.approve(exit_.id)  # the owner sells: the stop is cancelled first
    await env.cycle(4)
    sold = await _state(env, p.intent_id)
    assert sold["held"] == 0 and sold["stops"][0] == "CANCELLED"
    assert sold["incident"].status == "RESOLVED"  # the position was closed


async def test_when_the_price_comes_back_the_stop_fills_and_the_alert_ends(env: Env) -> None:
    """A triggered stop-limit fills once the price is back at its limit (on Binance as in the
    paper simulator): the alert ends, and so does the exit nobody needs any more."""
    p = await _held_with_its_stop(env)
    env.clock.advance(seconds=30)
    await env.set_market("80700.00", "80710.00")
    await env.cycle(1)
    assert (await _state(env, p.intent_id))["protection"] == "DEGRADED"
    env.clock.advance(seconds=30)
    await env.set_market("80950.00", "80960.00")  # back above the stop's limit
    await env.cycle(2)
    back = await _state(env, p.intent_id)
    assert back["stops"] == ["FILLED"] and back["held"] == 0
    assert back["incident"].status == "RESOLVED"
    assert back["exits"][0].status == "BLOCKED"  # nothing left to sell


async def test_a_venue_whose_stop_sells_at_market_is_left_alone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HFM's MT5 stop-loss closes at market once hit: a gap fills worse, but it fills."""
    p = await _held_with_its_stop(env)
    caps = env.svc.paper.capabilities()
    monkeypatch.setattr(
        env.svc.paper, "capabilities", lambda: replace(caps, stop_fills_at_market=True)
    )
    env.clock.advance(seconds=30)
    await env.set_market("80700.00", "80710.00")
    await env.cycle(1)
    state = await _state(env, p.intent_id)
    assert state["incident"] is None and state["exits"] == []


async def test_an_exit_already_waiting_is_the_one_to_decide_on(env: Env) -> None:
    """The owner asked to sell before the gap: the guard tells, and points at that exit."""
    p = await _held_with_its_stop(env)
    async with env.db.transaction() as conn:
        account = await get_account(conn, alias="paper-main")
        inst = await md.instrument_by_key(conn, BTC)
        own = await propose_close(
            conn,
            account=account,
            inst=inst,
            reason="I want out before the weekend",
            actor=Actor.user(env.owner.user_id),
            now=env.clock.now(),
        )
    env.clock.advance(seconds=30)
    await env.set_market("80700.00", "80710.00")
    await env.cycle(1)
    state = await _state(env, p.intent_id)
    assert [e.id for e in state["exits"]] == [own.intent_id]  # no second exit
    assert state["incident"].detail["exit"] == own.trade_code
    assert state["protection"] == "DEGRADED"


async def test_cancelling_a_gapped_stop_is_not_the_price_coming_back(env: Env) -> None:
    p = await _held_with_its_stop(env)
    env.clock.advance(seconds=30)
    await env.set_market("80700.00", "80710.00")
    await env.cycle(1)
    async with env.db.connect() as conn:
        stop_id = await conn.scalar(select(orders.c.id).where(orders.c.purpose == "STOP_LOSS"))
    await env.svc.execution.request_cancel(stop_id, reason="the owner cancels the stop")
    await env.cycle(2)  # cancelled, and placed again by the approved plan: gapped at once
    state = await _state(env, p.intent_id)
    assert state["incident"].status == "OPEN" and state["protection"] == "DEGRADED"
    assert len(state["exits"]) == 1  # the first exit is still the one waiting


async def test_a_rejected_exit_is_proposed_again_after_an_hour_while_the_gap_lasts(
    env: Env,
) -> None:
    p = await _held_with_its_stop(env)
    env.clock.advance(seconds=30)
    await env.set_market("80700.00", "80710.00")
    await env.cycle(1)
    first = (await _state(env, p.intent_id))["exits"][0]
    await env.approve(first.id, decision="REJECT")
    env.clock.advance(minutes=30)
    await env.set_market("80690.00", "80700.00")
    await env.cycle(1)
    assert len((await _state(env, p.intent_id))["exits"]) == 1  # not within the hour
    env.clock.advance(minutes=31)
    await env.set_market("80680.00", "80690.00")
    await env.cycle(1)
    exits = (await _state(env, p.intent_id))["exits"]
    assert len(exits) == 2 and exits[-1].status == "PROPOSED"
