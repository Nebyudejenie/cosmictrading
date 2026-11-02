"""Rule health (S48): a rule in use whose record since it was chosen is rarer than 1 run in 20 of
its history is told once; it stays watched below 1 in 10 and ends when its record is usual again;
a real-money account running it is named and told; nothing changes by itself."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, update

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import incidents, notifications
from cosmic.market.jobs import build_ingest_service
from cosmic.notify.telegram.handlers import TelegramHandlers, Update
from cosmic.notify.telegram.linking import create_link_code
from cosmic.reporting import autotrader, edge, road, rule_watch
from cosmic.reporting import rehearsal as page
from cosmic.trading import mandates
from cosmic.worker.context import WorkerContext
from cosmic.worker.jobs import build_registry
from tests.helpers import principal_for
from tests.market_mock import MarketMock
from tests.trading.test_auto_trader_daily import BTC, grant
from tests.trading.test_edge import _gold, _store_gold
from tests.trading.test_rehearsal import set_binance_fee, start
from tests.trading_env import Env, build_env

KEY = f"{rule_watch.PREFIX}turtle_55_20"
TG_ID = 555_000_448
PAPER = rule_watch.User("rehearsal-binance-100 [SIMULATED · PAPER]", False)
REAL = rule_watch.User("real-binance [LIVE · REAL MONEY]", True)


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


def _record(as_bad: float | None, trades: int = 6, total: float = -11.4) -> edge.Forward:
    return edge.Forward("turtle_55_20", trades, 0, total, None, as_bad)


async def _incident(env: Env) -> Any:
    async with env.db.connect() as conn:
        return (
            await conn.execute(
                select(incidents.c.status, incidents.c.title, incidents.c.detail)
                .where(incidents.c.dedup_key == KEY)
                .order_by(incidents.c.opened_at.desc())
                .limit(1)
            )
        ).first()


async def _told(env: Env, *, real: bool = False) -> int:
    async with env.db.connect() as conn:
        rows = await conn.execute(
            select(notifications.c.dedup_key).where(
                notifications.c.payload["alert"]["title"].astext.contains("rule Turtle"),
                notifications.c.channel == "PORTAL",
            )
        )
        keys = [r.dedup_key or "" for r in rows]
    return sum(1 for k in keys if (":real:" in k) == real)


async def _tell(env: Env, f: edge.Forward, users: list[rule_watch.User] | None = None) -> None:
    env.clock.advance(hours=24)
    async with env.db.transaction() as conn:
        await rule_watch.tell(
            conn, env.clock.now(), [f], {"turtle_55_20": [PAPER] if users is None else users}
        )


async def test_told_once_watched_below_1_in_10_and_ended_when_usual(env: Env) -> None:
    await _tell(env, _record(0.03))
    first = await _incident(env)
    assert first.status == "OPEN" and await _told(env) == 1
    assert first.title.endswith("is doing worse than nearly all of its history")
    await _tell(env, _record(0.02))  # a day later, still unusual: not told again
    await _tell(env, _record(0.05))  # exactly 1 in 20: no longer unusual, still watched
    watched = await _incident(env)
    assert watched.status == "OPEN" and await _told(env) == 1
    happened = watched.detail["alert"]["what_happened"]
    assert "(watched)" in watched.detail["alert"]["title"] and "only 5.00 % of runs" in happened
    assert "rehearsal-binance-100 [SIMULATED · PAPER]" in happened
    assert "None to money" in watched.detail["alert"]["risk_now"]
    await _tell(env, _record(0.10))  # exactly 1 in 10: usual again
    assert (await _incident(env)).status == "RESOLVED"
    await _tell(env, _record(0.0499))  # a new bad patch: told again, never rounded up to 5 %
    again = await _incident(env)
    assert again.status == "OPEN" and await _told(env) == 2
    assert "only 4.99 % of runs" in again.detail["alert"]["what_happened"]
    await _tell(env, _record(0.01), users=[])  # nobody runs it now: still watched, not ended
    assert (await _incident(env)).status == "OPEN"
    await _tell(env, _record(0.4), users=[])  # usual again, used or not: ended
    assert (await _incident(env)).status == "RESOLVED"


async def test_an_acknowledged_alert_is_not_sent_again(env: Env) -> None:
    await _tell(env, _record(0.02))
    async with env.db.transaction() as conn:
        await conn.execute(
            update(incidents).where(incidents.c.dedup_key == KEY).values(status="ACKNOWLEDGED")
        )
    await _tell(env, _record(0.01))
    assert (await _incident(env)).status == "ACKNOWLEDGED" and await _told(env) == 1


async def test_a_real_money_account_running_the_rule_is_named_and_told(env: Env) -> None:
    await _tell(env, _record(0.02), users=[PAPER, REAL])
    alert = (await _incident(env)).detail["alert"]
    assert alert["account"] == REAL.ref
    assert "Real money: real-binance [LIVE · REAL MONEY] runs this rule" in alert["risk_now"]
    assert "every real-money proposal still waits for your code" in alert["action_taken"]
    assert "None to money" not in alert["risk_now"]
    assert await _told(env, real=True) == 1
    await _tell(env, _record(0.01), users=[PAPER, REAL])  # the same account: told once
    assert await _told(env, real=True) == 1


async def test_the_worker_judges_the_gold_rules_in_use_and_the_road_and_telegram_say_so(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_binance_fee(env)
    await start(env)  # the Binance rehearsal on Turtle 55/20
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    await grant(env, timeframe="1h")  # an hourly bitcoin auto-trader: not judged on gold
    async with env.db.connect() as conn:
        in_use = await rule_watch.rules_in_use(conn, env.clock.now())
    assert in_use == {"turtle_55_20": [PAPER]}
    seen: dict[str, Any] = {}

    async def records(_db: Any, _now: Any, rules: list[str]) -> list[edge.Forward] | None:
        seen["rules"] = rules
        return None if seen.get("no prices") else [_record(0.004, 8, -15.2)]

    monkeypatch.setattr(rule_watch, "records", records)
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="test")
    job = build_registry(ctx).handlers["rehearsal.rule_watch"]
    seen["no prices"] = True
    await job(ctx, {})  # no prices yet: nothing judged
    assert await _incident(env) is None
    seen["no prices"] = False
    await job(ctx, {})
    assert seen["rules"] == ["turtle_55_20"] and (await _incident(env)).status == "OPEN"
    async with env.db.connect() as conn:
        way = await road.build(conn, env.settings, env.clock.now())
    assert any(
        s.startswith("Rule Turtle system 2 (55/20 breakout) is doing worse than nearly all")
        for s in way.steps
    )
    async with env.db.transaction() as conn:
        code = await create_link_code(
            conn, principal=principal_for(env.owner, env.clock), now=env.clock.now()
        )
    h = TelegramHandlers(env.db, env.clock, env.settings, env.svc)
    await h.handle(Update(TG_ID, TG_ID, "private", text=f"/link {code}"))
    answer = (await h.handle(Update(TG_ID, TG_ID, "private", text="/rehearsal")))[0].text
    assert "Since the rules were chosen (24 Oct 2026), replayed on Binance gold:" in answer
    assert (
        "- Turtle system 2 (55/20 breakout): 8 closed, -15.20 (SIMULATED); UNUSUAL: only 0.40 % "
        "of its history's runs did this badly or worse" in answer
    )

    async def broken(_db: Any, _now: Any, _rules: list[str]) -> list[edge.Forward]:
        raise RuntimeError("a replay failed")

    monkeypatch.setattr(rule_watch, "records", broken)
    answer = (await h.handle(Update(TG_ID, TG_ID, "private", text="/rehearsal")))[0].text
    assert "could not be worked out now" in answer and "money now" in answer  # still answers
    async with env.db.transaction() as conn:  # the owner looked
        await conn.execute(
            update(incidents).where(incidents.c.dedup_key == KEY).values(status="ACKNOWLEDGED")
        )
    async with env.db.connect() as conn:
        way = await road.build(conn, env.settings, env.clock.now())
    assert not any("is doing worse" in s for s in way.steps)


async def test_the_records_are_the_pages_own_on_stored_gold(env: Env) -> None:
    env.clock.set(datetime(2027, 3, 1, 12, tzinfo=UTC))
    ctx = WorkerContext(settings=env.settings, db=env.db, clock=env.clock, worker_id="test")
    await build_ingest_service(ctx, transport=MarketMock().transport()).sync_instruments(
        "binance_public"
    )
    await _store_gold(env, _gold(1300))
    rules = list(mandates.RULES_BY_TIMEFRAME[mandates.DAILY])
    found = await rule_watch.records(env.db, env.clock.now(), rules)
    async with env.db.connect() as conn:
        b = await page.gold_bench(conn, env.clock.now())
    assert isinstance(b, autotrader.Bench) and found is not None
    assert found == edge.build(b, rules, env.clock.now()).forward
    assert await rule_watch.records(env.db, env.clock.now(), []) == []
