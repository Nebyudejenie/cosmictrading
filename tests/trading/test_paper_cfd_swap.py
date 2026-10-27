"""The paper venue charges a CFD's overnight swap at the exit, as HFM books it (S38): the HFM
Cent terms rehearsal must not look better than a real Cent account would."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_execution import paper_fills, paper_orders
from cosmic.db.schema_market import instruments
from cosmic.trading import venue_terms
from tests.trading_env import Env, build_env

D = Decimal
CENT = "CFD:XAU/USD@HFM_CENT"
GOLD_SWAP = {  # as the bridge reports XAUUSD: points per lot a night, Wednesday triple
    "swap_mode": 1,
    "swap_long": "-78.56",
    "swap_short": "0.0",
    "swap_rollover3days": 3,
    "point": "0.01",
    "contract_size": "100.0",
}
MONDAY = datetime(2026, 10, 19, 22, 0, tzinfo=UTC)  # 18:00 in New York: after Monday's rollover


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def bought(
    env: Env, at: datetime, metadata: dict[str, object] | None, key: str = CENT
) -> SimpleNamespace:
    """A filled paper buy of 0.02 of ``key``, and the sell row that exits it."""
    async with env.db.transaction() as conn:
        await conn.execute(
            update(instruments).where(instruments.c.canonical_key == key).values(metadata=metadata)
        )
        oid = uuid7()
        await conn.execute(
            paper_orders.insert().values(
                id=oid,
                account_ref=env.account_id,
                client_order_id=f"c-{oid.hex[:12]}",
                venue_order_id=f"v-{oid.hex[:12]}",
                instrument_key=key,
                side="BUY",
                order_type="LIMIT",
                quantity=D("0.02"),
                limit_price=D(4000),
                time_in_force="GTC",
                status="FILLED",
                filled_qty=D("0.02"),
                created_at=at,
                updated_at=at,
            )
        )
        await conn.execute(
            paper_fills.insert().values(
                id=uuid7(),
                paper_order_id=oid,
                venue_fill_id=f"pf-{oid.hex[:12]}",
                quantity=D("0.02"),
                price=D(4000),
                fee=D(0),
                fee_asset="USD",
                liquidity="TAKER",
                filled_at=at,
            )
        )
    return SimpleNamespace(account_ref=env.account_id, instrument_key=key, side="SELL")


async def test_a_cfd_exit_pays_the_nights_held(env: Env) -> None:
    row = await bought(env, MONDAY, GOLD_SWAP)
    sold = MONDAY + timedelta(days=2, hours=1)  # Wednesday 21:00 UTC: Tuesday, then Wednesday ×3
    async with env.db.connect() as conn:
        cost = await env.svc.paper._swap_cost(conn, row, D("0.02"), D(4000), sold)
    sw = venue_terms.swap_per_night(GOLD_SWAP, 4000.0)
    assert sw is not None and venue_terms.nights(MONDAY, sold, sw.triple_weekday) == 4
    expected = D("0.02") * D(4000) * D(str(-sw.long)) * 4  # −0.79 USD an ounce a night
    assert cost == expected.quantize(D("0.00000001")) and cost > 0


async def test_no_swap_without_terms_on_a_spot_market_or_for_a_credit(env: Env) -> None:
    row = await bought(env, MONDAY, None)  # an older bridge reported no swap
    sold = MONDAY + timedelta(days=2)
    async with env.db.connect() as conn:
        assert await env.svc.paper._swap_cost(conn, row, D("0.02"), D(4000), sold) == 0
    credit = {**GOLD_SWAP, "swap_long": "12.0"}
    row = await bought(env, MONDAY, credit)
    async with env.db.connect() as conn:
        assert await env.svc.paper._swap_cost(conn, row, D("0.02"), D(4000), sold) == 0
    # a spot market never pays a swap, even with swap terms and a filled buy (the CFD guard)
    spot = await bought(env, MONDAY, GOLD_SWAP, key="SPOT:BTC/USD@COINBASE")
    async with env.db.connect() as conn:
        assert await env.svc.paper._swap_cost(conn, spot, D("0.01"), D(83_000), sold) == 0
