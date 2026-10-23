"""Should I take this trade? (reporting/decision.py, ADR-033): six signs on every proposal to
buy, from the competition's verdict, the trend, the session, the spread, the cost in R and the
size; shown on the approval page and with the Telegram card."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_research import auto_competitions
from cosmic.reporting import decision
from cosmic.research.library import auto, compete
from cosmic.trading import mandates
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading.test_auto_trader import breakout, hourly_bars
from tests.trading.test_auto_trader_money import grant_money, last_hour
from tests.trading_env import BTC, Env, build_env

D = Decimal
NAMES = ["Evidence", "Trend", "Time", "Cost now", "Cost of this trade", "Size"]


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    return await build_env(settings, db)


async def proposal(env: Env) -> tuple[Any, dict[str, Any]]:
    await grant_money(env, "5000", mode="ASK")
    await hourly_bars(env, breakout(), last_open=last_hour(env))
    assert await auto.run(env.db, env.clock) == [f"{BTC}:ASKED"]
    async with env.db.connect() as conn:
        intent = (await conn.execute(select(trade_intents))).one()
        packet = await conn.scalar(text("SELECT packet FROM approval_packets"))
    return intent, packet


async def test_every_proposal_to_buy_gets_six_signs(env: Env) -> None:
    _, packet = await proposal(env)
    async with env.db.connect() as conn:
        g = await decision.guide(conn, packet, env.clock.now())
    assert [s.name for s in g.signs] == NAMES
    by = {s.name: s for s in g.signs}
    assert by["Evidence"].mark == "UNKNOWN"  # no competition has run in this test
    assert "no proven edge yet" in g.summary
    assert by["Size"].mark == "GOOD" and "loses at most about" in by["Size"].text
    assert by["Cost of this trade"].mark in ("GOOD", "CAUTION", "AGAINST")
    assert "R (" in by["Cost of this trade"].text
    assert by["Time"].text.endswith(".") and by["Trend"].mark == "UNKNOWN"  # no daily bars
    lines = decision.telegram_lines(g)
    assert lines[0] == "Decision guide (not advice):" and len(lines) == 8


async def test_a_rejected_rule_is_evidence_against(env: Env) -> None:
    _, packet = await proposal(env)
    entry = {
        "rule": "turtle_20_10",
        "name": "Turtle",
        "market": BTC,
        "timeframe": "1h",
        "tier": "REJECTED",
        "earned": False,
        "idea": False,
        "val": {"trades": 40, "mean_r": -0.21},
        "forward": {"trades": 0, "total_r": 0.0},
    }
    async with env.db.transaction() as conn:
        await conn.execute(
            auto_competitions.insert().values(
                id=uuid7(),
                created_at=env.clock.now(),
                code_version="v",
                forward_from=compete.FORWARD_FROM,
                data_until=env.clock.now(),
                trials=1,
                result={"entries": [entry]},
                result_hash="h",
            )
        )
    async with env.db.connect() as conn:
        g = await decision.guide(conn, packet, env.clock.now())
    evidence = g.signs[0]
    assert evidence.mark == "AGAINST" and "-0.21 R a trade" in evidence.text


def test_the_cost_and_size_signs_follow_their_thresholds() -> None:
    def packet(stop: str, pct: str, fee_bps: str = "0") -> dict[str, Any]:
        return {
            "order": {"side": "BUY", "limit_price": "100"},
            "management_plan": {"stop_loss": {"stop_price": stop}},
            "costs": {"fee_bps": fee_bps, "currency": "USD"},
            "risk": {"estimated_max_loss": "30", "estimated_max_loss_pct_of_equity": pct},
        }

    cheap = decision._cost_of_trade(packet("90", "0.5"), D("0.0002"))  # 0.12 of 10.02 risk
    assert cheap.mark == "GOOD"
    dear = decision._cost_of_trade(packet("99", "0.5", fee_bps="60"), D("0.001"))
    assert dear.mark == "AGAINST" and "break even" in dear.text
    assert decision._cost_of_trade(packet("101", "0.5"), None).mark == "UNKNOWN"  # no real stop
    assert [decision._size(packet("90", p)).mark for p in ("0.8", "1.5", "3")] == [
        "GOOD",
        "CAUTION",
        "AGAINST",
    ]


async def test_an_exit_needs_no_guide(env: Env) -> None:
    _, packet = await proposal(env)
    sell = {**packet, "order": {**packet["order"], "side": "SELL"}}
    async with env.db.connect() as conn:
        assert (await decision.guide(conn, sell, env.clock.now())).signs == []


async def test_the_approval_page_shows_the_guide(env: Env) -> None:
    intent, _ = await proposal(env)
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get(f"/approvals/{intent.id}")).text
    assert "Should I take this trade?" in page and "not advice" in page
    assert all(name in page for name in NAMES)
