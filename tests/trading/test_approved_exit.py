"""Approved exits: closing a protected position replaces its stop and never oversells."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from sqlalchemy import select

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.core.errors import ValidationFailedError
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders, post_trade_reviews, trade_intents
from cosmic.db.schema_trading import positions
from cosmic.trading.proposals import ProposalResult, create_candidate
from tests.trading_env import Env, build_env, candidate


async def open_protected(env: Env) -> tuple[ProposalResult, D]:
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle()
    await env.cycle()  # entry FILLED, protective stop placed
    async with env.db.connect() as conn:
        rows = list(await conn.execute(select(orders).where(orders.c.intent_id == p.intent_id)))
    entry = next(o for o in rows if o.purpose == "ENTRY")
    stop = next(o for o in rows if o.purpose == "STOP_LOSS")
    assert entry.status == "FILLED" and stop.status == "ACKNOWLEDGED"
    return p, entry.filled_qty


def exit_candidate(qty: D, closes: tuple[object, ...], **kw: object) -> object:
    return candidate(
        side="SELL",
        effect="CLOSE",
        quantity=qty,
        limit_price=D("82900.00"),
        price_band_low=D("82000.00"),
        price_band_high=D("84000.00"),
        management_plan={},
        closes=closes,
        entry_conditions="Limit sell at or above 82,900.",
        **{"time_in_force": "IOC", **kw},
    )


async def propose_exit(env: Env, qty: D, closes: tuple[object, ...]) -> ProposalResult:
    async with env.db.transaction() as conn:
        return await create_candidate(
            conn,
            candidate=exit_candidate(qty, closes),  # type: ignore[arg-type]
            actor=Actor.user(env.owner.user_id),
            now=env.clock.now(),
        )


async def held(env: Env) -> D:
    async with env.db.connect() as conn:
        q = await conn.scalar(
            select(positions.c.quantity).where(positions.c.account_id == env.account_id)
        )
    return D(q or 0)


async def test_plain_sell_beside_open_entries_is_refused(settings: Settings, db: Database) -> None:
    """Selling a position that belongs to entries must name them (closes), so their stops are
    replaced instead of being left to oversell."""
    env = await build_env(settings, db)
    _, qty = await open_protected(env)
    with pytest.raises(ValidationFailedError, match="name them in closes"):
        await propose_exit(env, qty, ())


async def test_an_exit_that_replaces_stops_must_be_ioc(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    entry, qty = await open_protected(env)
    with pytest.raises(ValueError, match="must be IOC"):
        exit_candidate(qty, (entry.intent_id,), **{"time_in_force": "GTC"})


async def test_approved_exit_replaces_the_stop_and_closes_the_position(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)
    entry, qty = await open_protected(env)
    r = await propose_exit(env, qty, (entry.intent_id,))
    assert r.status == "PROPOSED", r.problems
    async with db.connect() as conn:
        packet_closes = (
            await conn.execute(
                select(trade_intents.c.details).where(trade_intents.c.id == r.intent_id)
            )
        ).scalar_one()["closes"]
    assert packet_closes == [str(entry.intent_id)]
    await env.approve(r.intent_id)
    await env.cycle()  # stop cancelled, then the exit is transmitted
    await env.cycle()  # the venue fills it
    async with db.connect() as conn:
        rows = list(await conn.execute(select(orders).where(orders.c.intent_id == entry.intent_id)))
        exit_intent = (
            await conn.execute(select(trade_intents).where(trade_intents.c.id == r.intent_id))
        ).one()
        reviews = list(await conn.execute(select(post_trade_reviews.c.intent_id)))
    by_purpose = {o.purpose: o for o in rows}
    assert by_purpose["STOP_LOSS"].status == "CANCELLED"
    assert by_purpose["EXIT"].status == "FILLED" and by_purpose["EXIT"].reduce_only
    assert by_purpose["EXIT"].filled_qty == qty
    assert exit_intent.status == "EXECUTED"
    assert await held(env) == 0
    assert [x.intent_id for x in reviews] == [entry.intent_id]  # entry reviewed as closed
    await env.cycle()
    async with db.connect() as conn:
        stops = list(
            await conn.execute(
                select(orders).where(
                    orders.c.intent_id == entry.intent_id, orders.c.purpose == "STOP_LOSS"
                )
            )
        )
    assert len(stops) == 1  # no new stop for a closed position


async def test_stop_filling_first_means_the_exit_is_never_sent(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)
    entry, qty = await open_protected(env)
    r = await propose_exit(env, qty, (entry.intent_id,))
    await env.approve(r.intent_id)
    await env.set_market(
        "80950.00", "80960.00"
    )  # triggers the stop (81,000) above its limit (80,900)
    await env.svc.paper.tick()  # the venue fills the stop before our exit runs
    await env.cycle()
    async with db.connect() as conn:
        exits = list(await conn.execute(select(orders).where(orders.c.purpose == "EXIT")))
        exit_intent = (
            await conn.execute(select(trade_intents).where(trade_intents.c.id == r.intent_id))
        ).one()
    assert all(o.status not in ("SUBMITTING", "SUBMITTED", "ACKNOWLEDGED", "FILLED") for o in exits)
    assert exit_intent.status in ("REQUIRES_REAPPROVAL", "BLOCKED", "EXECUTION_FAILED")
    assert await held(env) == 0  # sold once, by the stop: no oversell


async def test_closes_must_name_this_accounts_open_entries(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)
    entry, qty = await open_protected(env)
    with pytest.raises(ValidationFailedError):
        await propose_exit(env, qty, (env.account_id,))  # not an entry of this position
    with pytest.raises(ValidationFailedError):
        await propose_exit(env, qty * 2, (entry.intent_id,))  # more than those entries hold
    with pytest.raises(ValueError, match="only an exit"):
        candidate(closes=(entry.intent_id,))  # a BUY cannot close anything


async def test_two_exits_never_sell_more_than_is_held(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    entry, qty = await open_protected(env)
    a = await propose_exit(env, qty, (entry.intent_id,))
    b = await propose_exit(env, qty, (entry.intent_id,))
    assert a.status == b.status == "PROPOSED"
    await env.approve(a.intent_id)
    await env.approve(b.intent_id)
    await env.cycle()
    await env.cycle()
    async with db.connect() as conn:
        statuses = {
            r.id: r.status
            for r in await conn.execute(
                select(trade_intents.c.id, trade_intents.c.status).where(
                    trade_intents.c.id.in_([a.intent_id, b.intent_id])
                )
            )
        }
        exits = list(await conn.execute(select(orders).where(orders.c.purpose == "EXIT")))
    assert statuses[a.intent_id] == "EXECUTED"
    assert statuses[b.intent_id] in ("REQUIRES_REAPPROVAL", "BLOCKED")
    assert sum(o.filled_qty for o in exits) == qty  # sold exactly once
    assert await held(env) == 0


async def test_an_unfilled_exit_expires_and_the_stop_returns(
    settings: Settings, db: Database
) -> None:
    env = await build_env(settings, db)
    entry, qty = await open_protected(env)
    r = await propose_exit(env, qty, (entry.intent_id,))
    await env.approve(r.intent_id)
    await env.set_market("82050.00", "82060.00")  # inside the band, below the 82,900 limit
    await env.cycle()  # the stop is cancelled and the IOC exit sent
    await env.cycle()  # it does not fill: the venue expires it; protection comes back
    async with db.connect() as conn:
        rows = list(await conn.execute(select(orders).where(orders.c.intent_id == entry.intent_id)))
        exit_status = await conn.scalar(
            select(trade_intents.c.status).where(trade_intents.c.id == r.intent_id)
        )
    by = {}
    for o in rows:
        by.setdefault(o.purpose, []).append(o.status)
    assert by["EXIT"] == ["EXPIRED"] and exit_status == "EXECUTION_FAILED"
    assert "CANCELLED" in by["STOP_LOSS"] and "ACKNOWLEDGED" in by["STOP_LOSS"]  # a new stop
    assert await held(env) == qty  # nothing was sold
