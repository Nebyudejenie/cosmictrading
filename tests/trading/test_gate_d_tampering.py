"""Gate D's adversarial cases shown on their own (S115): an approved trade whose terms are changed
afterwards (its side, its instrument, its quantity, its price, its account), and an order whose
pre-send risk check cannot run or cannot be recorded. The database refuses the change, the order
goes out with exactly the approved terms, and nothing is sent without its risk check."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_execution import orders, paper_orders, trade_intents
from cosmic.db.schema_market import instruments
from cosmic.db.schema_trading import risk_check_runs
from cosmic.trading.execution import entry
from tests.trading_env import BTC, Env, build_env


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def _intent(env: Env, intent_id: Any) -> Any:
    async with env.db.connect() as conn:
        return (
            await conn.execute(select(trade_intents).where(trade_intents.c.id == intent_id))
        ).one()


async def test_an_approved_trades_terms_cannot_be_changed_and_it_goes_out_as_approved(
    env: Env,
) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    intent = await _intent(env, p.intent_id)
    async with env.db.connect() as conn:
        other = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key != BTC).limit(1)
        )
    changes: dict[str, dict[str, Any]] = {
        "a changed side": {"side": "SELL" if intent.side == "BUY" else "BUY"},
        "another symbol": {"instrument_id": other or uuid7()},
        "a changed quantity": {"quantity": intent.quantity * 10},
        "an excessive price": {"limit_price": intent.limit_price * 2},
        "another account": {"account_id": uuid7()},
        "another order type": {"order_type": "MARKET"},
    }
    for why, values in changes.items():
        with pytest.raises(DBAPIError, match="material terms are immutable"):
            async with env.db.transaction() as conn:
                await conn.execute(
                    update(trade_intents).where(trade_intents.c.id == p.intent_id).values(**values)
                )
        assert await _intent(env, p.intent_id) == intent, why
    await env.cycle()
    async with env.db.connect() as conn:
        sent = (
            await conn.execute(
                select(paper_orders).where(
                    paper_orders.c.client_order_id == f"ct-{intent.trade_code}-E"
                )
            )
        ).one()
    assert (sent.instrument_key, sent.side, sent.quantity, sent.limit_price, sent.order_type) == (
        BTC,
        intent.side,
        intent.quantity,
        intent.limit_price,
        intent.order_type,
    )
    assert sent.account_ref == intent.account_id


@pytest.mark.parametrize("broken", ["evaluate", "record_risk_run"])
async def test_no_order_is_sent_when_its_pre_send_risk_check_cannot_run_or_be_recorded(
    env: Env, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)

    def fails(*args: object, **kwargs: object) -> Any:
        raise RuntimeError(f"{broken} is down")

    monkeypatch.setattr(entry, broken, fails)
    with pytest.raises(RuntimeError, match="is down"):
        await env.svc.execution.execute_intent(p.intent_id)
    assert await env.count(paper_orders) == 0 and await env.count(orders) == 0
    assert (await _intent(env, p.intent_id)).status == "APPROVED"  # waiting, nothing sent
    monkeypatch.undo()
    await env.cycle()  # once the check runs again, it runs before the order and is recorded
    assert await env.count(orders, orders.c.purpose == "ENTRY") == 1
    async with env.db.connect() as conn:
        stages = [
            r.stage
            for r in await conn.execute(
                select(risk_check_runs.c.stage)
                .where(risk_check_runs.c.intent_id == p.intent_id)
                .order_by(risk_check_runs.c.created_at)
            )
        ]
    assert stages[-1] == "PRE_SUBMISSION"
