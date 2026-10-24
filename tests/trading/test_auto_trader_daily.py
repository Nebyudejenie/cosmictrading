"""Daily rules and one auto-trader per account (ADR-030 addendum 5): a daily mandate decides
once a day, waits for a fresh price rather than lose the day's signal, and each account's
auto-trader is shown, changed and stopped on its own."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_execution import trade_intents, trading_mandates
from cosmic.db.schema_market import instruments, market_events
from cosmic.reporting import autotrader, decision
from cosmic.research.library import auto
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import Market
from cosmic.trading import mandates, venue_terms
from cosmic.trading.accounts import create_paper_account, get_account
from cosmic.trading.mandates import terms_from_money
from cosmic.trading.paper_accounts import open_paper_account
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.trading.test_auto_expect_board import Row, history, walk
from tests.trading_env import BTC, Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    return await build_env(settings, db)


async def grant(
    env: Env,
    *,
    timeframe: str = "1d",
    rule: str = "turtle_20_10",
    mode: str = "AUTO",
    alias: str = "paper-main",
    money: str = "5000",
    care: str = "NORMAL",
) -> uuid.UUID:
    terms = terms_from_money(
        account_alias=alias,
        money=D(money),
        care=care,
        mode=mode,  # type: ignore[arg-type]
        instruments=(BTC,),
        strategy_key=rule,
        timeframe=timeframe,  # type: ignore[arg-type]
    )
    async with env.db.transaction() as conn:
        return await mandates.grant(
            conn, principal=env.principal(), terms=terms, now=env.clock.now(), settings=env.settings
        )


def last_day(env: Env) -> datetime:
    """The daily bar completed at today's 00:00 UTC opened a day before."""
    return mandates.day_start(env.clock.now()) - timedelta(days=1)


def quiet_then_breakout(days: int = 450, base: float = 82_900.0) -> list[Row]:
    """Quiet days, then a close above the prior 20-day high on the last one."""
    rows: list[Row] = [
        (base, base + 20, base - 20, base + (10 if i % 2 else -10)) for i in range(days - 1)
    ]
    return [*rows, (base, base + 220, base - 20, base + 200)]


async def notices(env: Env, kind: str) -> list[str]:
    async with env.db.connect() as conn:
        rows = await conn.execute(
            select(notifications.c.payload).where(notifications.c.kind == kind)
        )
        return [str(r.payload.get("text") or r.payload.get("reason") or "") for r in rows]


# ---- which rule runs on which bars --------------------------------------------------------------


async def test_each_rule_runs_on_its_own_bars_and_a_daily_one_never_asks(env: Env) -> None:
    with pytest.raises(ConflictError, match="does not run on 1-hour bars"):
        await grant(env, timeframe="1h", rule="ema_200_trend")
    with pytest.raises(ConflictError, match="ask-first works with hourly rules only"):
        await grant(env, mode="ASK")
    mid = await grant(env, rule="ema_200_trend")
    async with env.db.connect() as conn:
        m = await mandates.get_mandate(conn, mid)
    assert (m.timeframe, m.bars, m.strategy_key) == ("1d", "daily bars", "ema_200_trend")
    assert m.expires_at - m.granted_at == timedelta(days=90)  # daily mandates last 90 days
    with pytest.raises(Exception, match="immutable"):
        async with env.db.transaction() as conn:
            await conn.execute(
                update(trading_mandates).where(trading_mandates.c.id == mid).values(timeframe="1h")
            )
    assert "on daily bars" in (await notices(env, "mandate_changed"))[0]


def test_only_a_breakout_rule_has_a_level_to_watch() -> None:
    n = 120
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    closes = [100.0 + (i % 7) for i in range(n)]
    market = Market(
        "X",
        tuple(t0 + timedelta(days=i) for i in range(n)),
        tuple(closes),
        tuple(c + 2 for c in closes),
        tuple(c - 2 for c in closes),
        tuple(closes),
        tuple(1.0 for _ in closes),
    )
    assert auto.is_breakout(BY_KEY["turtle_55_20"]) and not auto.is_breakout(
        BY_KEY["ema_200_trend"]
    )
    assert auto.watch(BY_KEY["ema_200_trend"], market) is None
    assert auto.watch(BY_KEY["turtle_20_10"], market) is not None


# ---- deciding once a day --------------------------------------------------------------------------


async def test_a_daily_breakout_trades_once_on_its_bar(env: Env) -> None:
    await grant(env)
    await history(env, quiet_then_breakout(), last_open=last_day(env), timeframe="1d")
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    assert await auto.run(env.db, env.clock) == []  # the same bar is never handled twice
    async with env.db.connect() as conn:
        intent = (await conn.execute(select(trade_intents))).one()
    assert intent.details["timeframe"] == "1d" and decision.timeframe_of(intent) == "1d"
    assert "'Turtle system 1 (20/10 breakout)' broke out on daily bars" in intent.plain_summary
    assert "3×ATR(20) of the daily bars below entry" in intent.thesis
    assert "It runs on daily bars, as evaluated" in intent.thesis


async def test_renewing_never_trades_the_same_daily_signal_twice(env: Env) -> None:
    """A renewal is a new mandate; the bar's buy signal was acted on under the old one."""
    await grant(env)
    await history(env, quiet_then_breakout(), last_open=last_day(env), timeframe="1d")
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    env.clock.advance(seconds=1)
    terms = terms_from_money(
        account_alias="paper-main",
        money=D(5000),
        care="NORMAL",
        mode="AUTO",
        instruments=(BTC,),
        timeframe="1d",
    )
    async with env.db.transaction() as conn:  # the owner renews at once
        await mandates.grant(
            conn,
            principal=env.principal(),
            terms=terms,
            now=env.clock.now(),
            settings=env.settings,
            replace=True,
        )
    assert await auto.run(env.db, env.clock) == []  # the first buy is still on its way
    async with env.db.connect() as conn:
        account = await get_account(conn, alias="paper-main")
        inst = await conn.scalar(select(instruments.c.id).where(instruments.c.canonical_key == BTC))
        assert await auto._bought_on_bar(conn, account.id, inst, "turtle_20_10", last_day(env))
        assert await conn.scalar(select(func.count()).select_from(trade_intents)) == 1


async def test_daily_rules_do_not_run_on_markets_closed_at_weekends(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Friday signal would wait for a price until a newer bar replaced it (until S38)."""
    monkeypatch.setattr(venue_terms, "METALS", frozenset({"BTC"}))  # the test market as gold
    with pytest.raises(ConflictError, match="closes at weekends"):
        await grant(env)
    await grant(env, timeframe="1h")  # hourly rules handle it bar by bar, as before


async def test_a_100_dollar_account_is_sized_within_every_risk_limit(env: Env) -> None:
    """S36: at $100 and Bold, the rule's size would be worth more than R06 (50 % of equity in
    one market) and R07 (the cash) allow: it is made smaller instead of sized and refused."""
    async with env.db.transaction() as conn:
        main = await get_account(conn, alias="paper-main")
        acct = await open_paper_account(  # funds the paper venue as well as the ledger
            conn,
            broker=env.svc.paper,
            alias="paper-100",
            venue_key=main.venue_key,
            base_currency=main.base_currency,
            initial_capital=D(100),
            now=env.clock.now(),
            actor=Actor.system("test"),
        )
    await env.svc.reconciler.reconcile(acct, trigger="test")
    await grant(env, alias="paper-100", money="100", care="BOLD")
    await history(env, quiet_then_breakout(), last_open=last_day(env), timeframe="1d")
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    async with env.db.connect() as conn:
        packet = await conn.scalar(text("SELECT packet FROM approval_packets"))
    assert D(str(packet["order"]["max_notional"])) <= D(50)  # R06: half of the 100
    assert D(str(packet["risk"]["estimated_max_loss"])) <= D(2)  # Bold: 2 % of the 100


async def test_an_old_daily_series_is_never_acted_on(env: Env) -> None:
    await grant(env)
    old = last_day(env) - timedelta(days=2)
    await history(env, quiet_then_breakout(), last_open=old, timeframe="1d")
    assert await auto.run(env.db, env.clock) == []
    async with env.db.connect() as conn:
        assert await conn.scalar(select(market_events.c.id).limit(1)) is None


async def test_without_a_fresh_price_a_daily_signal_waits_instead_of_being_lost(
    env: Env,
) -> None:
    await grant(env)
    env.clock.advance(hours=3)  # the venue's last quote is now hours old
    await history(env, quiet_then_breakout(), last_open=last_day(env), timeframe="1d")
    assert await auto.run(env.db, env.clock) == []
    assert await auto.run(env.db, env.clock) == []
    waiting = [t for t in await notices(env, "auto_trade_soon") if "no fresh price" in t]
    assert len(waiting) == 1  # told once, and the signal is kept
    async with env.db.connect() as conn:
        assert await conn.scalar(select(market_events.c.id).limit(1)) is None
    await env.set_market("82900.00", "82900.50")
    await env.svc.reconciler.reconcile(env.account_id, trigger="test")  # hours later, as live
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]  # not lost


async def test_a_daily_exit_may_be_tried_again_each_hour(env: Env) -> None:
    mid = await grant(env)
    async with env.db.connect() as conn:
        m = await mandates.get_mandate(conn, mid)
    spec, bar = BY_KEY["turtle_20_10"], last_day(env)
    once = auto.signal_key(m, spec, BTC, bar)
    first = auto.signal_key(m, spec, BTC, bar, "2026101001")
    second = auto.signal_key(m, spec, BTC, bar, "2026101002")
    assert len({once, first, second}) == 3 and first.startswith(once)


# ---- what one trade risks, and what to expect, on daily bars --------------------------------------


async def test_what_one_trade_risks_follows_the_bars_and_the_venue_stop(env: Env) -> None:
    async with env.db.transaction() as conn:  # a lot big enough to show cents
        await conn.execute(
            update(instruments).where(instruments.c.canonical_key == BTC).values(min_qty=D("0.01"))
        )
    wide: list[Row] = [(82_900.0, 83_300.0, 82_500.0, 82_900.0)] * 450
    narrow: list[Row] = [(82_900.0, 82_920.0, 82_880.0, 82_900.0)] * 100
    await history(env, wide, last_open=last_day(env), timeframe="1d")
    hour = env.clock.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    await history(env, narrow, last_open=hour)
    now = env.clock.now()
    async with env.db.connect() as conn:
        _, hourly, _, _ = await autotrader.instrument_needs(conn, BTC, now, "1h")
        _, daily, _, _ = await autotrader.instrument_needs(conn, BTC, now, "1d")
        _, gap, _, _ = await autotrader.instrument_needs(conn, BTC, now, "1d", market_stop=False)
    assert hourly is not None and daily is not None and gap is not None
    assert daily > hourly  # a stop 3×ATR of daily bars is wider than of hourly ones
    assert gap > daily  # without a market stop, the stop-limit 0.5 % lower adds to the risk


async def test_what_to_expect_replays_a_daily_rule_over_years(env: Env) -> None:
    rows = walk(1_200, seed=5)
    await history(env, rows, last_open=last_day(env), timeframe="1d")
    now = env.clock.now()
    async with env.db.connect() as conn:
        account = await get_account(conn, alias="paper-main")
        e = await autotrader.expectation(
            conn,
            now,
            account,
            [BTC],
            per_trade=D(50),
            daily_stop=D(150),
            money=D(5000),
            total_stop=D(500),
            strategy_key="turtle_55_20",
            timeframe="1d",
        )
    assert e.summary is not None, e.note
    assert e.daily and e.unit == "days" and e.bar_word == "daily"
    assert e.period.startswith("the years ") and e.rule == "Turtle system 2 (55/20 breakout)"
    assert e.summary.days > 700  # years, not the last twelve months


# ---- the owner is told before a mandate ends ------------------------------------------------------


async def test_the_owner_is_reminded_before_a_mandate_ends(env: Env) -> None:
    await grant(env, timeframe="1h")  # 30 days
    env.clock.advance(days=23, hours=12)  # six and a half days left
    async with env.db.transaction() as conn:
        assert await mandates.remind_renewals(conn, env.clock.now()) == 1
    async with env.db.transaction() as conn:
        await mandates.remind_renewals(conn, env.clock.now())  # told once, not every cycle
    env.clock.advance(days=6)  # half a day left
    async with env.db.transaction() as conn:
        await mandates.remind_renewals(conn, env.clock.now())
    async with env.db.connect() as conn:
        keys = sorted(
            r.dedup_key
            for r in await conn.execute(
                select(notifications.c.dedup_key).where(
                    notifications.c.dedup_key.like("mandate:%:renew:%")
                )
            )
        )
    assert sorted({re.search(r":renew:(\d+)", k).group(1) for k in keys}) == ["1", "7"]  # type: ignore[union-attr]
    assert all(
        "renew it under" in t for t in await notices(env, "mandate_changed") if "ends on" in t
    )


# ---- one auto-trader per account ------------------------------------------------------------------


async def test_each_account_has_its_own_auto_trader(env: Env, settings: Settings) -> None:
    async with env.db.transaction() as conn:
        main = await get_account(conn, alias="paper-main")
        await create_paper_account(
            conn,
            alias="paper-two",
            venue_key=main.venue_key,
            base_currency=main.base_currency,
            initial_capital=D(10_000),
            now=env.clock.now(),
            actor=Actor.system("test"),
        )
    await grant(env, timeframe="1h")
    env.clock.advance(seconds=1)
    await grant(env, alias="paper-two", rule="turtle_55_20")
    env.clock.advance(seconds=1)
    async with env.db.connect() as conn:
        boards = await autotrader.boards(conn, env.clock.now())
    assert [b.account.alias for b in boards if b.account] == ["paper-main", "paper-two"]
    assert [b.bars for b in boards] == ["1-hour bars", "daily bars"]
    heads = [autotrader.telegram_lines(b, env.clock.now())[0] for b in boards]
    assert heads[1].endswith("Turtle system 2 (55/20 breakout) on daily bars")
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/trader")).text
        assert "2 auto-traders run" in page and 'href="/trader?account=paper-two"' in page
        page = (await c.get("/trader?account=paper-two")).text
        assert "Turtle system 2 (55/20 breakout) on daily bars" in page
        assert re.search(r'name="account" value="paper-two"', page)
        setup = (
            await c.get(
                "/trader/setup?account=paper-two&timeframe=1d&rule=ema_200_trend&money=5000"
            )
        ).text
        assert 'value="ema_200_trend" selected' in setup and 'value="1d" checked' in setup
        env.clock.advance(seconds=1)
        r = await c.post(  # new settings for paper-two only
            "/trader/start",
            data={
                "csrf_token": csrf_from(setup),
                "money": "5000",
                "care": "NORMAL",
                "mode": "AUTO",
                "account": "paper-two",
                "timeframe": "1d",
                "rule": "ema_200_trend",
                "instruments": BTC,
            },
        )
        assert r.status_code == 303, r.text
        r = await c.post(
            "/trader/stop", data={"csrf_token": csrf_from(page), "account": "paper-two"}
        )
        assert r.status_code == 303, r.text
        page = (await c.get("/trader?account=paper-two")).text  # off, and it says what still runs
        assert "1 auto-trader runs" in page and 'href="/trader?account=paper-main"' in page
        assert "Price above its 200-day average on daily bars" in page  # what it last ran
        assert "timeframe=1d&amp;rule=ema_200_trend" in page  # restart where it stopped
        assert 'href="/trader/competition?account=paper-two"' in page
        home = (await c.get("/")).text
        assert "Turtle system 1 (20/10 breakout) on 1-hour bars" in home
    async with env.db.connect() as conn:
        rows = list(
            await conn.execute(
                text(
                    "SELECT a.alias, m.strategy_key, m.timeframe, m.status FROM trading_mandates m "
                    "JOIN accounts a ON a.id = m.account_id ORDER BY m.granted_at"
                )
            )
        )
    assert [tuple(r) for r in rows] == [
        ("paper-main", "turtle_20_10", "1h", "ACTIVE"),  # never touched by paper-two's changes
        ("paper-two", "turtle_55_20", "1d", "REVOKED"),  # replaced by the new settings
        ("paper-two", "ema_200_trend", "1d", "REVOKED"),  # then stopped
    ]
