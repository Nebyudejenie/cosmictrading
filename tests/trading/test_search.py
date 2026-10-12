"""Full-text research search (DATA-10)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_market import claims
from cosmic.research.search import search
from tests.trading_env import Env, build_env


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def _claims(env: Env) -> None:
    async with env.db.transaction() as conn:
        for statement, status in (
            ("RSI on BTC hourly closes is 71, above the overbought line", "VERIFIED"),
            ("Momentum is likely to continue because volume is rising", "INFERRED"),
            ("ETH broke out of its range on strong volume", "CONTRADICTED"),
        ):
            await conn.execute(
                claims.insert().values(
                    id=uuid7(),
                    statement=statement,
                    claim_type="QUALITATIVE",
                    status=status,
                    created_by="test",
                    created_at=env.clock.now(),
                )
            )


async def test_search_claims_and_proposals_by_permission(env: Env) -> None:
    await _claims(env)
    await env.propose()  # thesis: "Trend continuation after a higher low on the hourly chart."
    async with env.db.connect() as conn:
        owner = await search(conn, "hourly", include_proposals=True)
        viewer = await search(conn, "hourly", include_proposals=False)
        phrase = await search(conn, '"higher low"', include_proposals=True)
        excluded = await search(conn, "volume -ETH", include_proposals=True)
    assert {h.kind for h in owner} == {"CLAIM", "PROPOSAL"}
    assert {h.kind for h in viewer} == {"CLAIM"}  # proposals need READ_ACCOUNT
    assert [h.kind for h in phrase] == ["PROPOSAL"] and "«higher» «low»" in phrase[0].snippet
    assert [h.status for h in excluded] == ["INFERRED"]  # the ETH claim is excluded
    claim = next(h for h in owner if h.kind == "CLAIM")
    assert claim.status == "VERIFIED" and "«hourly»" in claim.snippet


async def test_hostile_or_empty_queries_are_harmless(env: Env) -> None:
    await _claims(env)
    async with env.db.connect() as conn:
        for q in ("", "   ", "'; DROP TABLE claims; --", "a & | ! ( :*", "x" * 5000):
            assert isinstance(await search(conn, q, include_proposals=True), list)
        assert await conn.scalar(text("SELECT count(*) FROM claims")) == 3


async def test_claim_search_uses_the_fulltext_index(env: Env) -> None:
    await _claims(env)
    async with env.db.connect() as conn:
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            r[0]
            for r in await conn.execute(
                text(
                    "EXPLAIN SELECT id FROM claims, websearch_to_tsquery('english', 'rsi') q "
                    "WHERE to_tsvector('english', statement) @@ q"
                )
            )
        )
    assert "claims_fts" in plan, plan
