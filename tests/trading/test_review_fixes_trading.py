"""Regressions from the review of 2026-10-23 (mandates, the decision guide, prices, pages):
each test pins a confirmed finding so it cannot come back."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_research import auto_competitions
from cosmic.market import data as md
from cosmic.reporting import decision
from cosmic.research.library import auto, compete
from cosmic.trading import mandates
from cosmic.trading.mandates import MandateTerms, entry_refusal
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading.test_auto_expect_board import history, walk
from tests.trading.test_auto_trader import breakout, hourly_bars
from tests.trading.test_auto_trader_money import grant_money, last_hour
from tests.trading_env import BTC, Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    return await build_env(settings, db)


async def ask_proposal(env: Env) -> tuple[Any, dict[str, Any], mandates.Mandate]:
    await grant_money(env, "5000", mode="ASK")
    await hourly_bars(env, breakout(), last_open=last_hour(env))
    assert await auto.run(env.db, env.clock) == [f"{BTC}:ASKED"]
    async with env.db.connect() as conn:
        intent = (
            await conn.execute(
                select(trade_intents).where(
                    trade_intents.c.details["mandate_id"].astext.is_not(None)
                )
            )
        ).one()
        packet = await conn.scalar(
            text("SELECT packet FROM approval_packets WHERE intent_id = :i"), {"i": intent.id}
        )
        (m,) = await mandates.active_mandates(conn, env.clock.now())
    return intent, packet, m


async def test_an_entry_not_filled_yet_counts_as_open(env: Env) -> None:
    """A second market cannot slip past the total loss stop while the first entry waits."""
    intent, _, m = await ask_proposal(env)
    async with env.db.connect() as conn:
        waiting = await mandates.usage(conn, m, env.clock.now())
        itself = await mandates.usage(conn, m, env.clock.now(), exclude=intent.id)
    assert (waiting.open_positions, itself.open_positions) == (1, 0)
    assert waiting.open_value == intent.quantity * intent.limit_price and itself.open_value == 0
    nearly = mandates.Usage(0, waiting.open_positions, D(0), D(0), D(-410))  # 410 lost of 500
    assert "past your limit of 500.00" in str(entry_refusal(m, nearly, D(50), D(1000)))
    assert entry_refusal(m, mandates.Usage(0, 0, D(0), D(0), D(-410)), D(50), D(1000)) is None


async def test_an_entry_waiting_beside_a_manual_position_still_counts(env: Env) -> None:
    """Counting a waiting entry by what it has not filled yet, not by the instrument: a manual
    position in the same market must not hide it from the money cap."""
    manual = await env.propose()
    await env.approve(manual.intent_id)
    await env.cycle(2)  # a manual BTC position is held
    intent, _, m = await ask_proposal(env)
    async with env.db.connect() as conn:
        u = await mandates.usage(conn, m, env.clock.now())
        alone = await mandates.usage(conn, m, env.clock.now(), exclude=intent.id)
    assert u.open_positions == alone.open_positions == 1  # one market, counted once
    assert u.open_value - alone.open_value == intent.quantity * intent.limit_price


async def test_the_audit_records_the_total_limit_that_was_stored(env: Env) -> None:
    terms = MandateTerms(
        account_alias="paper-main",
        strategy_key="turtle_20_10",
        instruments=(BTC,),
        max_loss_per_trade=D(30),
        max_trades_per_day=5,
        max_open_positions=2,
        daily_loss_limit=D(60),
        days=30,
    )
    async with env.db.transaction() as conn:
        await mandates.grant(
            conn, principal=env.principal(), terms=terms, now=env.clock.now(), settings=env.settings
        )
    async with env.db.connect() as conn:
        payload = await conn.scalar(
            select(audit_events.c.payload).where(audit_events.c.action == "mandate.granted")
        )
    assert D(payload["total_loss_limit"]) == D(300)


async def competition(env: Env, market: str, timeframe: str, tier: str, signal: str) -> None:
    entry = {
        "rule": "turtle_20_10",
        "name": "Turtle",
        "market": market,
        "timeframe": timeframe,
        "tier": tier,
        "earned": False,
        "idea": False,
        "val": {"trades": 40, "mean_r": 0.31},
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
                result={"entries": [entry], "costs": {market: {"signal": signal}}},
                result_hash="h",
            )
        )


async def test_the_evidence_is_for_this_market_and_these_bars_only(env: Env) -> None:
    _, packet, _ = await ask_proposal(env)
    await competition(env, "CFD:XAU/USD@HFM", "1h", "PROMISING", "SPOT:PAXG/USDT@BINANCE")
    async with env.db.connect() as conn:
        g = await decision.guide(conn, packet, env.clock.now(), "1h")
    assert g.signs[0].mark == "UNKNOWN" and "not tested on this market" in g.signs[0].text
    await competition(env, "CFD:BTC/USD@HFM", "1h", "PROMISING", BTC)  # same prices, other costs
    async with env.db.connect() as conn:
        other_costs = await decision.guide(conn, packet, env.clock.now(), "1h")
    assert other_costs.signs[0].mark == "UNKNOWN"  # never another market's verdict
    await competition(env, BTC, "1h", "PROMISING", BTC)
    async with env.db.connect() as conn:
        own = await decision.guide(conn, packet, env.clock.now(), "1h")
        daily = await decision.guide(conn, packet, env.clock.now(), "1d")
    assert own.signs[0].mark == "CAUTION" and "+0.31 R a trade" in own.signs[0].text
    assert daily.signs[0].mark == "UNKNOWN" and "daily prices" in daily.signs[0].text


async def test_a_stale_price_is_never_the_cost_now(env: Env) -> None:
    _, packet, _ = await ask_proposal(env)
    env.clock.advance(hours=3)  # the stored quotes are now stale
    async with env.db.connect() as conn:
        g = await decision.guide(conn, packet, env.clock.now(), "1h")
    cost_now = next(s for s in g.signs if s.name == "Cost now")
    assert cost_now.mark == "UNKNOWN" and cost_now.text.startswith("No fresh price (")
    cost = next(s for s in g.signs if s.name == "Cost of this trade")
    assert "without the spread (unknown)" in cost.text


async def test_a_closed_market_keeps_its_measured_spread(env: Env) -> None:
    """Over a weekend no quote falls in the last day: its last stored quotes are used."""
    async with env.db.connect() as conn:
        inst = await md.instrument_by_key(conn, BTC)
        recent = await md.median_spread(conn, inst.id, env.clock.now(), timedelta(hours=24))
        later = env.clock.now() + timedelta(days=2)
        kept = await md.median_spread(conn, inst.id, later, timedelta(hours=24))
        none = await md.median_spread(conn, inst.id, later, timedelta(hours=24), fallback=0)
    assert recent is not None and kept == recent and none is None
    async with env.db.connect() as conn:  # quotes older than a week are not "recent"
        stale = await md.median_spread(
            conn, inst.id, env.clock.now() + timedelta(days=8), timedelta(hours=24)
        )
    assert stale is None


async def test_the_whole_test_link_replays_the_mandates_own_markets(env: Env) -> None:
    await history(env, walk(40 * 24), last_open=last_hour(env))
    await grant_money(env, "5000")
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/trader")).text
        assert (
            f"instruments={BTC.replace(':', '%3A').replace('/', '%2F').replace('@', '%40')}" in page
        )
        setup = (await c.get("/trader/setup", params={"money": "5000", "instruments": BTC})).text
    assert f'name="instruments" value="{BTC}" checked' in setup.replace("\\n", "")
