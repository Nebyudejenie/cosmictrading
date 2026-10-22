"""The Gold page (ADR-032): price, trend and volatility, what one ounce needs, why the
auto-trader's rule lost or won, the benchmark of buying every month, and Telegram /gold."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_market import instruments
from cosmic.reporting import gold
from cosmic.trading import mandates
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading.test_auto_expect_board import history, walk
from tests.trading.test_auto_trader_money import last_hour
from tests.trading_env import BTC, Env, build_env


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    """BTC stands in for gold: the test registry has no HFM gold."""
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    monkeypatch.setattr(gold, "GOLD_MARKETS", ((BTC, BTC),))
    e = await build_env(settings, db)
    await history(e, walk(60 * 24, seed=4), last_open=last_hour(e))
    day = e.clock.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    await history(e, walk(900, seed=8), last_open=day, timeframe="1d")
    return e


async def test_the_gold_board_explains_and_compares(env: Env) -> None:
    async with env.db.connect() as conn:
        g = await gold.build(conn, env.clock.now())
    assert g.traded == BTC and g.price is not None and g.regime is not None
    assert g.change_1d is not None and g.change_1y is not None and g.from_high_pct is not None
    assert g.atr_pct is not None and g.atr_usual_pct is not None and g.session
    assert g.one_lot_value is not None and set(g.minimum) == set(mandates.CARE_LEVELS)
    assert g.chart is not None and not g.chart.in_trade
    d = g.diagnosis
    assert d is not None and d.trades > 0 and d.findings[0].startswith("Before costs the rule's")
    assert sum(b.trades for b in d.sessions) == d.trades
    assert [b.years for b in g.benchmarks] == [1]  # 900 days hold one full year, not three
    assert g.costs["spread_measured"]
    lines = gold.telegram_lines(g)
    assert lines[0].startswith("GOLD ") and lines[-1].startswith("SIMULATED research")
    assert any(line.startswith("Why the rule lost or won: Before costs") for line in lines)


async def test_the_gold_page_reads_plainly(env: Env) -> None:
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/gold")).text
        home = (await c.get("/")).text
    for heading in (
        "The last year",
        "Can you trade gold with your money?",
        "Where the auto-trader would buy",
        "Why the auto-trader lost on gold",
        "How to win on gold",
        "Gold over the long run",
        "Your gold trades",
    ):
        assert heading in page, heading
    assert page.count('class="chart chart-price"') == 3  # the year, the radar, the benchmark
    assert "HISTORICAL SIMULATION" in page and "Not advice" in page
    assert 'href="/gold"' in home  # in the menu


async def test_the_broker_swap_is_counted_and_shown(env: Env) -> None:
    """With the bridge's swap report (version 3), the research charges it every night held
    (bitcoin trades every day) and the page names it."""
    swap = {
        "swap_long": "-2500",  # 25.00 USD a coin a night: big enough to show
        "swap_short": "0.0",
        "swap_mode": 1,
        "swap_rollover3days": 3,
        "point": "0.01",
        "contract_size": "1.0",
    }
    async with env.db.transaction() as conn:
        await conn.execute(
            instruments.update().where(instruments.c.canonical_key == BTC).values(metadata=swap)
        )
    async with env.db.connect() as conn:
        g = await gold.build(conn, env.clock.now())
    d = g.diagnosis
    assert d is not None and d.swap_r < 0 and g.costs["swap"] is not None
    assert any(f.startswith("Of those costs, the overnight swap took") for f in d.findings)
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/gold")).text
    assert "Of which overnight swap" in page and "a night for a buyer (MetaTrader 5)" in page
    assert "none at weekends" not in page  # bitcoin trades and pays every night


def test_gold_lines_without_gold() -> None:
    assert gold.telegram_lines(gold.GoldBoard()) == ["Gold is not configured here."]
