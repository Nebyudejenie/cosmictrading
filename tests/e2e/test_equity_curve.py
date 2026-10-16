"""Portfolio's equity curve per account (directive Phase 2): drawn from the 5-minute samples,
thinned for the chart, honest when there is too little to draw."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_trading import equity_snapshots
from cosmic.trading.queries import equity_history
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading_env import build_env


async def test_portfolio_draws_each_accounts_equity_curve(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db)
    app = create_app(settings, db=db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        empty = (await c.get("/portfolio")).text
        assert "Not enough equity samples yet" in empty  # nothing recorded: said so
        now = env.clock.now()
        async with db.transaction() as conn:
            for i in range(600):  # about two days of 5-minute samples
                await conn.execute(
                    equity_snapshots.insert().values(
                        id=uuid7(),
                        account_id=env.account_id,
                        taken_at=now - timedelta(minutes=5 * (600 - i)),
                        equity=D(10_000) + D(i),
                        cash=D(10_000),
                        positions_value=D(i),
                        price_label="DELAYED",
                        is_simulated=True,
                    )
                )
        page = (await c.get("/portfolio")).text
    assert '<svg class="chart"' in page
    assert 'aria-label="paper-main equity in USD, last 7 days"' in page
    async with db.connect() as conn:
        points = await equity_history(conn, env.account_id, now - timedelta(days=7))
    assert len(points) == 240  # thinned for the chart
    assert points[-1][1] == D(10_599) and points[0][1] == D(10_000)  # both ends kept
