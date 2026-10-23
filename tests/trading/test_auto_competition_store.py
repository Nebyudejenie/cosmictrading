"""The rule competition in the worker and on the pages (ADR-031): one append-only run a day
from the auto-trader's markets with their costs, shown on /trader/competition, on /trader and
in Telegram /trader."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.research.library import compete, compete_store
from cosmic.research.library.catalog import BY_KEY
from cosmic.trading import mandates
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading.test_auto_expect_board import history, walk
from tests.trading.test_auto_trader_money import grant_money, last_hour
from tests.trading_env import BTC, Env, build_env

RULES = (
    "turtle_20_10",
    "keltner_breakout",
    "halving_18m",
)  # a breakout, a channel, a calendar rule


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    monkeypatch.setattr(compete_store, "MARKETS", ((BTC, BTC),))
    monkeypatch.setattr(compete_store, "SPEC_POOL", tuple(BY_KEY[k] for k in RULES))
    e = await build_env(settings, db)
    await history(e, walk(60 * 24), last_open=last_hour(e))
    day = e.clock.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    await history(e, walk(900, seed=9), last_open=day, timeframe="1d")
    return e


async def test_one_run_a_day_kept_as_it_was(env: Env) -> None:
    out = await compete_store.run_if_due(env.db, env.clock.now())
    assert out["status"] == "COMPLETED" and out["trials"] >= 4
    again = await compete_store.run_if_due(env.db, env.clock.now() + timedelta(hours=19))
    assert again == {"status": "CURRENT", "run": out["run"]}
    async with env.db.connect() as conn:
        run = await compete_store.latest_run(conn)
    assert run is not None and str(run.id) == out["run"]
    assert run.code_version == compete_store.CODE_VERSION
    assert run.forward_from == compete.FORWARD_FROM
    ideas = {(e["rule"], e["timeframe"]) for e in run.entries if e["idea"]}
    assert ideas == {
        ("idea_turtle_trend", "1h"),
        ("idea_turtle_busy", "1h"),
        ("idea_turtle_trend_busy", "1h"),
        ("idea_keltner_trend", "1h"),
        ("idea_turtle_trend", "1d"),
        ("idea_keltner_trend", "1d"),
    }
    assert all(e["tier"] in ("IDEA", "N/A") for e in run.entries if e["idea"])
    assert {(e["rule"], e["timeframe"]) for e in run.entries if not e["idea"]} == {
        (k, tf) for k in RULES for tf in ("1h", "1d")
    }
    (cal,) = [e for e in run.entries if e["rule"] == "halving_18m" and e["timeframe"] == "1d"]
    assert cal["error"] is None  # daily rules see calendar dates
    costs = run.result["costs"][BTC]
    assert costs["spread_measured"] and costs["spread_pct"] > 0
    assert sum(run.tiers().values()) == len(run.entries)
    with pytest.raises(Exception, match=r"append-only|permission denied"):
        async with env.db.transaction() as conn:
            await conn.execute(text("UPDATE auto_competitions SET trials = 0"))
    later = await compete_store.run_if_due(env.db, env.clock.now() + timedelta(hours=21))
    assert later["status"] == "COMPLETED" and later["run"] != out["run"]  # a new row, not a change


async def test_the_pages_and_telegram_show_the_verdict(env: Env) -> None:
    await grant_money(env, "5000")
    await compete_store.run_if_due(env.db, env.clock.now())
    async with env.db.connect() as conn:
        run = await compete_store.latest_run(conn)
    line = compete_store.verdict_line(run, "turtle_20_10")
    assert line.startswith("Rule competition (") and "Your rule on hourly bars:" in line
    assert compete_store.verdict_line(None, "turtle_20_10").endswith("within a few hours.")
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/trader/competition")).text
        assert "Rule competition" in page and "Leaderboard" in page
        assert "Turtle system 1 (20/10 breakout)" in page and "How the competition works" in page
        assert "The forward test: the race on new prices" in page
        assert "Ideas from the research" in page and "with the daily trend up" in page
        assert 'class="spark"' in page or "—" in page
        assert "Your auto-trader&#39;s rule" in page or "Your auto-trader's rule" in page
        trader = (await c.get("/trader")).text
        assert line in trader.replace("&#39;", "'") and 'href="/trader/competition"' in trader
