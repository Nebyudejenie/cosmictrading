"""AT-13: AI provider outage or quota exhaustion does not disable hard risk or reconciliation."""

from __future__ import annotations

from pydantic import SecretStr

from cosmic.ai.gateway import AIGateway
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_execution import paper_orders
from tests.ai_fake import FakeAI
from tests.integration.test_ai_gateway import setup_routes, task
from tests.trading_env import build_env


async def test_at13_ai_outage_does_not_affect_safety(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    s = settings.model_copy(update={"groq_api_key": SecretStr("gsk_" + "z" * 40)})
    await setup_routes(db, s, env.clock, username="aiadmin")
    fake = FakeAI()
    for _ in range(10):
        fake.behaviours["groq"].append(("status", (503,)))
    gw = AIGateway(db, env.clock, s, transport=fake.transport(), retry_base_seconds=0)
    for i in range(3):
        assert (await gw.run(task(content=f"x{i}"))).status == "WAITING_FOR_AI"
    # Deterministic risk, approval, execution and reconciliation still work.
    p = await env.propose()
    assert p.status == "PROPOSED"
    blocked = await env.propose(quantity=__import__("decimal").Decimal("0.02"))
    assert blocked.status == "NON_EXECUTABLE"  # hard limits still enforced
    await env.approve(p.intent_id)
    await env.cycle(2)
    assert await env.count(paper_orders) == 2
    assert (await env.svc.reconciler.reconcile(env.account_id, trigger="ai-outage")).passed
