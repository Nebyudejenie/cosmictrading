"""The AI's second opinion on HFM's gold (S68): read from HFM's own candles through the free-AI
gateway (a fake provider here; the real adapter code runs), stored, and used only to stop a
trade. Without a configured free AI the rules trade alone."""

from __future__ import annotations

import json
from decimal import Decimal as D

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from cosmic.ai import gold_opinion
from cosmic.ai.gateway import AIGateway
from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_gold import ai_gold_opinions
from cosmic.gold import second_opinion, sim
from cosmic.sources.ingest import ensure_registry
from cosmic.trading.policy import ensure_default_policy
from tests.ai_fake import FakeAI
from tests.helpers import make_user
from tests.integration.test_ai_gateway import GROQ_KEY, setup_routes
from tests.trading.test_gold_sim import WED, World

OPINION = {
    "direction": "UP",
    "recommendation": "WAIT",
    "confidence": "MEDIUM",
    "why": "The hourly trend is up but the daily candle sits at resistance; wait for a pullback.",
    "conditions": "Hourly trend up, RSI 71 (stretched), spread normal.",
    "expected_risk": "A 30-dollar stop; a reversal at resistance would reach it.",
    "timing": "London and New York are open: liquid, but a stretched move.",
}


@pytest.fixture
async def w(settings: Settings, db: Database) -> World:
    clock = FrozenClock(WED)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await ensure_default_policy(conn, clock.now())
    owner = await make_user(db, clock)
    world = World(db, clock, owner.user_id)
    await world.hfm("4040.00", "4040.30")
    return world


async def _opinions(db: Database) -> int:
    async with db.connect() as conn:
        return int(await conn.scalar(select(func.count()).select_from(ai_gold_opinions)) or 0)


async def test_without_a_free_ai_nothing_is_sent_and_the_rules_trade_alone(
    w: World, settings: Settings, db: Database
) -> None:
    fake = FakeAI()
    gw = AIGateway(db, w.clock, settings, transport=fake.transport(), retry_base_seconds=0)
    await w.market("BUY")
    result = await gold_opinion.opine(db, gw, w.clock.now())
    assert result is not None and result.status == "WAITING_FOR_AI" and fake.calls == []
    assert await _opinions(db) == 0
    acct = await w.account("100")
    assert await sim.decide(db, w.clock.now()) == [f"{acct}:BUY"]  # the rules alone
    await gw.close()


async def test_the_ai_reads_hfms_candles_and_can_only_stop_a_trade(
    w: World, settings: Settings, db: Database
) -> None:
    s = settings.model_copy(update={"groq_api_key": SecretStr(GROQ_KEY)})
    await setup_routes(db, s, w.clock, username="ai-owner")
    fake = FakeAI()
    fake.default["groq"] = OPINION
    gw = AIGateway(db, w.clock, s, transport=fake.transport(), retry_base_seconds=0)
    await w.market("BUY")
    result = await gold_opinion.opine(db, gw, w.clock.now())
    assert result is not None and result.status == "OK"
    assert await gold_opinion.opine(db, gw, w.clock.now()) is None  # once per 5 minutes
    (call,) = fake.calls
    sent = json.dumps(call["body"])
    assert "1h: candles" in sent and "1d: candles" in sent and "bid 4040.00" in sent
    assert "balance" not in sent.lower() and "hfm-demo" not in sent  # market data only
    async with db.connect() as conn:
        row = await second_opinion.latest(conn)
    assert (row.recommendation, row.direction, row.reading) == ("WAIT", "UP", "BUY")
    # The rules say BUY; the AI says WAIT: the trade waits, with the AI's own reason.
    acct = await w.account("100")
    assert await sim.decide(db, w.clock.now()) == [f"{acct}:WAIT"]
    decisions = await w.decisions(acct)
    assert "the AI's second opinion" in decisions[-1].reasons[-1]  # type: ignore[attr-defined]
    assert "wait for a pullback" in decisions[-1].reasons[-1]  # type: ignore[attr-defined]
    assert await w.positions(acct) == []
    # An agreeing opinion lets it through; the AI never sizes or starts anything itself.
    w.clock.advance(minutes=6)
    await w.hfm("4040.00", "4040.30")
    await w.market("BUY")
    fake.default["groq"] = {**OPINION, "recommendation": "BUY"}
    assert (await gold_opinion.opine(db, gw, w.clock.now())).status == "OK"  # type: ignore[union-attr]
    assert await sim.decide(db, w.clock.now()) == [f"{acct}:BUY"]
    (p,) = await w.positions(acct)
    assert p.ounces == D("0.06")  # the rules' own size: 2.00 at a 30.00 stop
    await gw.close()
