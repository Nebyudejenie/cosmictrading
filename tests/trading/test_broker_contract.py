"""Broker adapter contract suite (V6 §1814). Parametrised over registered adapters; a live
adapter must pass it against its venue sandbox before registration. Only 'paper' exists."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
    SubmitRequest,
    VenueOrderStatus,
)
from tests.trading_env import BTC, Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


def req(coid: str, **kw: object) -> SubmitRequest:
    base: dict[str, object] = {
        "client_order_id": coid,
        "instrument_key": BTC,
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": D("0.001"),
        "limit_price": D("83000.00"),
        "stop_price": None,
        "time_in_force": "GTC",
        "expires_at": None,
    }
    base.update(kw)
    return SubmitRequest(**base)  # type: ignore[arg-type]


async def test_capabilities_declared(env: Env) -> None:
    caps = env.svc.paper.capabilities()
    assert caps.is_simulated and not caps.is_live and not caps.bracket_or_oco
    assert caps.idempotent_client_ids and caps.partial_fills
    assert "ASSUMPTION" in caps.fees


async def test_submit_idempotent_by_client_order_id(env: Env) -> None:
    b = env.svc.paper
    r1 = await b.submit_order(env.account_id, req("c1"))
    r2 = await b.submit_order(env.account_id, req("c1"))
    assert r1.accepted and r2.accepted and r1.venue_order_id == r2.venue_order_id
    assert len(await b.list_open_orders(env.account_id)) == 1


async def test_rejections_are_results_not_errors(env: Env) -> None:
    b = env.svc.paper
    assert not (
        await b.submit_order(env.account_id, req("c2", quantity=D("0.0010000001")))
    ).accepted
    assert not (
        await b.submit_order(env.account_id, req("c3", quantity=D("1")))
    ).accepted  # insufficient funds
    assert not (await b.submit_order(env.account_id, req("c4", order_type="MARKET"))).accepted
    o = await b.get_order(env.account_id, "c3")
    assert o.status is VenueOrderStatus.REJECTED and "insufficient" in (o.reject_reason or "")


async def test_get_order_not_found_is_explicit(env: Env) -> None:
    with pytest.raises(BrokerOrderNotFoundError):
        await env.svc.paper.get_order(env.account_id, "never-sent")


async def test_cancel_and_balances(env: Env) -> None:
    b = env.svc.paper
    await b.submit_order(env.account_id, req("c5", limit_price=D("80000.00")))
    usd = {x.asset: x for x in await b.get_balances(env.account_id)}["USD"]
    assert usd.available < usd.total  # funds on hold
    await b.cancel_order(env.account_id, "c5")
    assert (await b.get_order(env.account_id, "c5")).status is VenueOrderStatus.CANCELLED
    usd = {x.asset: x for x in await b.get_balances(env.account_id)}["USD"]
    assert usd.available == usd.total


async def test_fill_only_on_fresh_quote(env: Env) -> None:
    b = env.svc.paper
    await b.submit_order(env.account_id, req("c6"))
    env.clock.advance(minutes=10)  # quote now stale
    assert await b.tick() == 0
    env.clock.advance(seconds=1)
    await env.set_market("82986.50", "82986.51")
    assert await b.tick() == 1
    fills = await b.get_fills(env.account_id, since=None)
    assert fills[0].client_order_id == "c6" and fills[0].fee > 0


async def test_fault_modes(env: Env) -> None:
    b = env.svc.paper
    await b.inject_fault("UNAVAILABLE")
    with pytest.raises(BrokerUnavailableError):
        await b.submit_order(env.account_id, req("c7"))
    with pytest.raises(BrokerOrderNotFoundError):
        await b.get_order(env.account_id, "c7")  # definitively not accepted
    await b.inject_fault("TIMEOUT_BEFORE_ACCEPT")
    with pytest.raises(BrokerAmbiguousError):
        await b.submit_order(env.account_id, req("c8"))
    await b.inject_fault("TIMEOUT_AFTER_ACCEPT")
    with pytest.raises(BrokerAmbiguousError):
        await b.submit_order(env.account_id, req("c9"))
    assert (
        await b.get_order(env.account_id, "c9")
    ).status is VenueOrderStatus.OPEN  # it did reach the venue


async def test_fault_injection_refused_outside_dev(env: Env) -> None:
    from cosmic.core.errors import ConflictError
    from cosmic.trading.brokers.paper import PaperBroker

    prod_like = env.settings.model_copy(update={"paper_fault_injection": False})
    with pytest.raises(ConflictError):
        await PaperBroker(env.db, env.clock, prod_like).inject_fault("REJECT_NEXT")
