"""Operations → Demo auto-trader: grant a mandate with a fresh code, see it, stop it."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trading_mandates
from cosmic.notify.telegram.runner import notification_text
from cosmic.trading import mandates
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.trading_env import BTC, build_env

REF = "paper-main [SIMULATED · PAPER]"
D = Decimal


async def test_owner_starts_and_stops_the_auto_trader(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    env = await build_env(settings, db)
    app = create_app(settings, db=db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/ops")).text
        assert "Demo auto-trader" in page and f">{REF}</option>" in page
        assert "Turtle system 1 (20/10 breakout)" in page
        r = await c.post(
            "/ops/auto-trader",
            data={
                "csrf_token": csrf_from(page),
                "account": "paper-main",
                "rule": "turtle_20_10",
                "instruments": BTC,
                "max_loss_per_trade": "30",
                "max_trades_per_day": "5",
                "max_open_positions": "2",
                "daily_loss_limit": "60",
                "days": "30",
            },
        )
        assert r.status_code == 303, r.text
        page = (await c.get("/ops")).text
        assert "Stop the auto-trader on" in page and "loss at the stop ≤ 30.00" in page
        async with db.connect() as conn:
            m = (await conn.execute(select(trading_mandates))).one()
        r = await c.post(f"/ops/auto-trader/{m.id}/revoke", data={"csrf_token": csrf_from(page)})
        assert r.status_code == 303, r.text
    async with db.connect() as conn:
        assert (await conn.execute(select(trading_mandates.c.status))).scalar_one() == "REVOKED"


def test_auto_trader_messages_say_what_happened_and_where() -> None:
    trade = notification_text(
        "auto_trade",
        {
            "trade_code": "T-1",
            "account_ref": "hfm-demo [SIMULATED · DEMO]",
            "action": "BUY 0.02 BTC at a limit of 81900",
            "max_loss": "23.10",
            "rule": "Turtle system 1 (20/10 breakout)",
        },
    )
    assert trade.startswith("AUTO TRADE T-1 on hfm-demo [SIMULATED · DEMO]: BUY 0.02 BTC")
    assert "at most 23.10" in trade and "forward test" in trade
    skipped = notification_text(
        "auto_trade_skipped",
        {
            "account_ref": "hfm-demo [SIMULATED · DEMO]",
            "action": "buy CFD:XAU/USD@HFM",
            "reason": "the smallest trade (1) would risk about 44.10",
        },
    )
    assert skipped == (
        "Auto-trader skipped on hfm-demo [SIMULATED · DEMO]: buy CFD:XAU/USD@HFM. "
        "Why: the smallest trade (1) would risk about 44.10"
    )
    assert notification_text("mandate_changed", {"text": "Auto-trader ON"}) == "Auto-trader ON"


async def test_owner_starts_it_with_money_and_sees_it(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-trader page: off, a plan from money, started in ASK mode, then stopped."""
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    env = await build_env(settings, db)
    app = create_app(settings, db=db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/trader")).text
        assert "Your auto-trader" in page and "OFF" in page and "Start the auto-trader" in page
        assert 'http-equiv="refresh"' in page  # it keeps itself up to date
        setup = (await c.get("/trader/setup?money=5000&care=NORMAL&mode=ASK")).text
        assert "Most one trade can lose" in setup and "50.00" in setup and "150.00" in setup
        r = await c.post(
            "/trader/start",
            data={
                "csrf_token": csrf_from(setup),
                "money": "5000",
                "care": "NORMAL",
                "mode": "ASK",
                "instruments": BTC,
            },
        )
        assert r.status_code == 303, r.text
        page = (await c.get("/trader")).text
        assert "ASKS YOU FIRST" in page and "5,000.00" in page and "TIME TO TRADE" in page
        assert "Your auto-trader" in (await c.get("/")).text  # the home page shows it too
        r = await c.post("/trader/stop", data={"csrf_token": csrf_from(page)})
        assert r.status_code == 303, r.text
        assert "OFF" in (await c.get("/trader")).text
    async with db.connect() as conn:
        m = (await conn.execute(select(trading_mandates))).one()
    assert (m.capital, m.risk_pct, m.mode, m.status) == (D(5000), D(1), "ASK", "REVOKED")
