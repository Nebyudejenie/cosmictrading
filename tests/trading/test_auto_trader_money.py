"""The auto-trader from money (ADR-030 addendum): limits from an amount and a care level, the
money limit, ASK mode, heads-ups and the auto-trader page's read model."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_execution import approvals, trade_intents, trading_mandates
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.reporting import autotrader
from cosmic.research.library import auto
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import Market
from cosmic.trading import mandates
from cosmic.trading.mandates import (
    Mandate,
    MandateTerms,
    Usage,
    entry_refusal,
    terms_from_money,
)
from tests.trading.test_auto_trader import breakout, hourly_bars
from tests.trading_env import BTC, Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    return await build_env(settings, db)


async def grant_money(env: Env, money: str, care: str = "NORMAL", mode: str = "AUTO") -> uuid.UUID:
    terms = terms_from_money(
        account_alias="paper-main",
        money=D(money),
        care=care,
        mode=mode,  # type: ignore[arg-type]
        instruments=(BTC,),
    )
    async with env.db.transaction() as conn:
        return await mandates.grant(
            conn,
            principal=env.principal(),
            terms=terms,
            now=env.clock.now(),
            settings=env.settings,
            replace=True,
        )


async def bars(env: Env, rows: list[tuple[float, float, float]], *, last_open: Any) -> None:
    """Completed 1-hour bars (close, high, low) ending with the bar opened at ``last_open``."""
    async with env.db.transaction() as conn:
        inst = (
            await conn.execute(select(instruments.c.id).where(instruments.c.canonical_key == BTC))
        ).scalar_one()
        src = (
            await conn.execute(
                select(sources.c.id).where(sources.c.key == "coinbase_exchange_public")
            )
        ).scalar_one()
        run = uuid7()
        now = env.clock.now()
        await conn.execute(
            fetch_runs.insert().values(
                id=run,
                source_id=src,
                kind="test_bars",
                started_at=now,
                finished_at=now,
                status="SUCCESS",
            )
        )
        first = last_open - timedelta(hours=len(rows) - 1)
        for i, (c, h, lo) in enumerate(rows):
            await conn.execute(
                text(
                    "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, "
                    "low, close, volume, is_complete, fetch_run_id, ingested_at, updated_at) "
                    "VALUES (:i, :s, '1h', :t, :c, :h, :l, :c, 1, true, :r, :n, :n)"
                ),
                {
                    "i": inst,
                    "s": src,
                    "t": first + timedelta(hours=i),
                    "c": D(str(c)),
                    "h": D(str(h)),
                    "l": D(str(lo)),
                    "r": run,
                    "n": now,
                },
            )


def last_hour(env: Env) -> Any:
    return env.clock.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


async def kinds(env: Env) -> list[str]:
    async with env.db.connect() as conn:
        return [r.kind for r in await conn.execute(select(notifications.c.kind))]


# ---- pure rules ---------------------------------------------------------------------------------


def test_money_and_care_set_every_limit() -> None:
    t = terms_from_money(
        account_alias="hfm-demo", money=D(5000), care="NORMAL", mode="ASK", instruments=(BTC,)
    )
    assert (t.max_loss_per_trade, t.daily_loss_limit) == (D("50.00"), D("150.00"))
    assert (t.max_trades_per_day, t.max_open_positions, t.days) == (5, 2, 30)
    assert (t.capital, t.risk_pct, t.mode) == (D(5000), D(1), "ASK")
    assert t.total_loss_limit == t.total_limit == D("500.00")  # ten trades' worth
    careful = terms_from_money(
        account_alias="xx", money=D(5000), care="CAREFUL", mode="AUTO", instruments=(BTC,)
    )
    assert careful.max_loss_per_trade == D("25.00")
    with pytest.raises(ConflictError, match="too little money"):
        terms_from_money(
            account_alias="xx", money=D(150), care="CAREFUL", mode="AUTO", instruments=(BTC,)
        )
    with pytest.raises(ConflictError, match="unknown care level"):
        terms_from_money(
            account_alias="xx", money=D(5000), care="WILD", mode="AUTO", instruments=(BTC,)
        )


def test_open_positions_never_exceed_the_money() -> None:
    start = datetime(2026, 10, 22, 0, tzinfo=UTC)
    m = Mandate(
        uuid.uuid4(),
        uuid.uuid4(),
        "turtle_20_10",
        "1h",
        (BTC,),
        D(50),
        5,
        2,
        D(150),
        "ACTIVE",
        uuid.uuid4(),
        start,
        start + timedelta(days=1),
        None,
        None,
        D(5000),
        D(1),
        "AUTO",
    )
    held = Usage(0, 1, D(0), D(4000))
    assert "worth more than your 5,000.00" in str(entry_refusal(m, held, D(10), D(1500)))
    assert entry_refusal(m, held, D(10), D(900)) is None
    legacy = replace(m, capital=None)  # a mandate granted without money keeps the old rules
    assert entry_refusal(legacy, held, D(10), D(1_000_000)) is None


def test_the_total_loss_stop_counts_the_worst_case_of_open_trades() -> None:
    """Addendum 3: an entry needs room for a full loss on it and on every open trade."""
    start = datetime(2026, 10, 22, 0, tzinfo=UTC)
    m = Mandate(
        uuid.uuid4(),
        uuid.uuid4(),
        "turtle_20_10",
        "1h",
        (BTC,),
        D(50),
        5,
        2,
        D(150),
        "ACTIVE",
        uuid.uuid4(),
        start,
        start + timedelta(days=1),
        None,
        None,
        D(5000),
        D(1),
        "AUTO",
        D(500),
    )
    one_open = Usage(0, 1, D(0), D(0), D(-400))  # lost 400: 400 + 2 × 50 = 500 fits
    assert entry_refusal(m, one_open, D(50), D(1000)) is None
    worse = Usage(0, 1, D(0), D(0), D("-400.01"))
    assert "past your limit of 500.00 (lost so far 400.01)" in str(
        entry_refusal(m, worse, D(50), D(1000))
    )
    legacy = replace(m, total_loss_limit=None)  # granted before 0013: ten trades' worth
    assert legacy.total_limit == D(500)
    ahead = Usage(0, 1, D(0), D(0), D(1000))  # profits give room
    assert entry_refusal(m, ahead, D(50), D(1000)) is None


def test_watch_reports_the_next_breakout_level() -> None:
    closes = [100.0 + (i % 3) for i in range(40)]
    market = Market(
        "X",
        tuple(range(40)),
        tuple(closes),
        tuple(c + 2 for c in closes),
        tuple(c - 2 for c in closes),
        tuple(closes),
        tuple(1.0 for _ in closes),
    )
    level, close, atr = auto.watch(BY_KEY["turtle_20_10"], market)  # type: ignore[misc]
    assert level == max(market.high[-20:]) and close == closes[-1] and atr > 0


# ---- with money, automatically ------------------------------------------------------------------


async def test_a_trade_with_money_shows_on_the_board_from_open_to_closed(env: Env) -> None:
    await grant_money(env, "5000")
    last = last_hour(env)
    await hourly_bars(env, breakout(), last_open=last)
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    async with env.db.connect() as conn:
        packet = await conn.scalar(text("SELECT packet FROM approval_packets"))
        m = (await mandates.active_mandates(conn, env.clock.now()))[0]
    assert D(str(packet["risk"]["estimated_max_loss"])) <= D("50.00")
    assert D(str(packet["order"]["max_notional"])) <= D(5000)
    assert m.capital == D(5000) and m.mode == "AUTO"
    await env.cycle(2)
    async with env.db.connect() as conn:
        board = await autotrader.build(conn, env.clock.now())
    assert board.account_ref == "paper-main [SIMULATED · PAPER]" and board.mandate is not None
    assert len(board.open) == 1 and board.open[0].auto and board.open[0].loss_if_stopped
    assert board.watch and board.watch[0].state == "IN A TRADE"
    env.clock.advance(hours=2)
    await env.set_market("83000.00", "83000.50")
    await hourly_bars(env, [*breakout(), 83_150.0, 82_000.0], last_open=last + timedelta(hours=2))
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    await env.cycle(2)
    async with env.db.connect() as conn:
        board = await autotrader.build(conn, env.clock.now())
        lines = autotrader.telegram_lines(board, env.clock.now())
    assert not board.open and len(board.closed) == 1
    t = board.closed[0]
    assert t.auto and t.market == "BTC/USD" and t.how == "exit"
    assert board.totals.closed == 1 and board.totals.total == t.pnl
    assert board.curve_values == [float(t.pnl)]
    assert lines[0].startswith("Auto-trader ON on paper-main [SIMULATED · PAPER]")
    assert any(line.startswith("Result: ") for line in lines)
    closed = [k for k in await kinds(env) if k == "trade_closed"]
    assert closed, "the owner hears how the trade ended"


# ---- ask first ------------------------------------------------------------------------------------


async def test_ask_mode_proposes_and_waits_for_the_owner(env: Env) -> None:
    await grant_money(env, "5000", mode="ASK")
    await hourly_bars(env, breakout(), last_open=last_hour(env))
    assert await auto.run(env.db, env.clock) == [f"{BTC}:ASKED"]
    async with env.db.connect() as conn:
        intent = (await conn.execute(select(trade_intents))).one()
        asked = await conn.scalar(
            select(notifications.c.payload).where(notifications.c.kind == "approval_requested")
        )
        assert await conn.scalar(select(approvals.c.id)) is None  # nothing approved by itself
        board = await autotrader.build(conn, env.clock.now())
    assert intent.status == "PROPOSED" and intent.plain_summary.startswith("TIME TO TRADE")
    assert (
        asked["time_to_trade"] is True and asked["account_ref"] == "paper-main [SIMULATED · PAPER]"
    )
    assert [w.code for w in board.waiting] == [intent.trade_code]
    await env.approve(intent.id)  # the owner says: trade now
    await env.cycle(2)
    async with env.db.connect() as conn:
        status = await conn.scalar(select(trade_intents.c.status))
    assert status in ("EXECUTING", "EXECUTED")


# ---- heads-up and settings ------------------------------------------------------------------------


async def test_a_heads_up_comes_once_when_a_breakout_is_near(env: Env) -> None:
    await grant_money(env, "5000")
    # Closes swing between 82,850 and 82,950 (highs +20): the next breakout level is 82,970.
    rows = [(82_850.0 + (100 if i % 2 else 0), 0.0, 0.0) for i in range(70)]
    rows = [(c, c + 20, c - 20) for c, _, _ in rows]
    rows[-1] = (82_940.0, 82_960.0, 82_920.0)  # 30 below the level, within half an ATR
    await bars(env, rows, last_open=last_hour(env))
    assert await auto.run(env.db, env.clock) == []
    assert await auto.run(env.db, env.clock) == []
    async with env.db.connect() as conn:
        soon = [
            n.payload
            for n in await conn.execute(
                select(notifications).where(
                    notifications.c.kind == "auto_trade_soon", notifications.c.channel == "PORTAL"
                )
            )
        ]
    assert len(soon) == 1 and "below its breakout level" in soon[0]["text"]


async def test_the_total_loss_stop_switches_it_off_for_good(env: Env) -> None:
    """Addendum 3: after a losing trade leaves no room for another full loss within the total
    limit, and nothing is open, the next cycle ends the mandate STOPPED and says why."""
    terms = MandateTerms(
        account_alias="paper-main",
        strategy_key="turtle_20_10",
        instruments=(BTC,),
        max_loss_per_trade=D(50),
        max_trades_per_day=5,
        max_open_positions=2,
        daily_loss_limit=D(150),
        days=30,
        total_loss_limit=D("50.01"),  # room for exactly one full loss
    )
    async with env.db.transaction() as conn:
        mid = await mandates.grant(
            conn, principal=env.principal(), terms=terms, now=env.clock.now(), settings=env.settings
        )
    last = last_hour(env)
    await hourly_bars(env, breakout(), last_open=last)
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    await env.cycle(2)
    env.clock.advance(hours=2)
    await env.set_market("83000.00", "83000.50")
    await hourly_bars(env, [*breakout(), 83_150.0, 82_000.0], last_open=last + timedelta(hours=2))
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]  # the rule's exit
    await env.cycle(2)
    async with env.db.connect() as conn:
        m = await mandates.get_mandate(conn, mid)
        u = await mandates.usage(conn, m, env.clock.now())
    assert m.status == "ACTIVE" and u.realized_total < 0 and u.open_positions == 0
    await auto.run(env.db, env.clock)  # each cycle checks the stop first
    async with env.db.connect() as conn:
        m = await mandates.get_mandate(conn, mid)
        board = await autotrader.build(conn, env.clock.now())
        texts = [
            n.payload["text"]
            for n in await conn.execute(
                select(notifications).where(notifications.c.kind == "mandate_changed")
            )
        ]
    assert m.status == "STOPPED" and m.end_reason is not None
    assert "one more full loss (50.00) would pass your total loss limit of 50.01" in m.end_reason
    assert board.mandate is None and board.ended is not None and board.ended.id == mid
    assert autotrader.telegram_lines(board, env.clock.now())[1].startswith(
        "It stopped itself: it lost"
    )
    assert any("(stopped: it lost" in t for t in texts)
    assert any("stops for good after losing 50.01 in total" in t for t in texts)  # granted


def test_a_deep_drop_halves_the_size_until_it_recovers() -> None:
    """Addendum 4: half size from five losing trades' worth below the best result, full again
    below two and a half; in between it keeps the size it has."""
    r = D(50)
    assert mandates.sizing([], r) == mandates.Sizing(D(1), D(0), None)
    up_then_down = [D(100), *[D(-50)] * 4]  # best +100, then −200: a drop of 4 trades' worth
    assert not mandates.sizing(up_then_down, r).half
    s = mandates.sizing([*up_then_down, D(-50)], r)  # 5 trades' worth: half
    assert s.half and s.drop == D(250) and s.switched_after == 6
    still = mandates.sizing([*up_then_down, D(-50), D(100)], r)  # drop 150: still above 125
    assert still.half and still.switched_after == 6
    back = mandates.sizing([*up_then_down, D(-50), D(100), D(30)], r)  # drop 120 < 125
    assert not back.half and back.switched_after == 8


async def test_after_a_deep_drop_entries_are_sized_to_half(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await grant_money(env, "5000")  # 50 a trade
    half = mandates.Sizing(D("0.5"), D(260), 6)

    async def halved(conn: object, m: object) -> mandates.Sizing:
        return half

    monkeypatch.setattr(mandates, "sizing_now", halved)
    await hourly_bars(env, breakout(), last_open=last_hour(env))
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    async with env.db.connect() as conn:
        packet = await conn.scalar(text("SELECT packet FROM approval_packets"))
        summary = await conn.scalar(select(trade_intents.c.plain_summary))
    assert D(str(packet["risk"]["estimated_max_loss"])) <= D("25.00")
    assert "half size after a deep drop" in summary


async def test_the_size_change_is_told_once_and_shown(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mandates, "HALVE_AFTER", D("0.01"))  # any loss is "deep" here
    await grant_money(env, "5000")
    last = last_hour(env)
    await hourly_bars(env, breakout(), last_open=last)
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    await env.cycle(2)
    env.clock.advance(hours=2)
    await env.set_market("83000.00", "83000.50")
    await hourly_bars(env, [*breakout(), 83_150.0, 82_000.0], last_open=last + timedelta(hours=2))
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]  # the exit, at a loss
    await env.cycle(2)
    await auto.run(env.db, env.clock)
    await auto.run(env.db, env.clock)  # told once, not every cycle
    async with env.db.connect() as conn:
        board = await autotrader.build(conn, env.clock.now())
        texts = [
            n.payload["text"]
            for n in await conn.execute(
                select(notifications).where(
                    notifications.c.kind == "mandate_changed", notifications.c.channel == "PORTAL"
                )
            )
            if "HALF size" in n.payload["text"]
        ]
    assert board.sizing is not None and board.sizing.half
    assert len(texts) == 1 and "returns to full size when the drop is below" in texts[0]
    assert any(
        line.startswith("Trade size: HALF")
        for line in autotrader.telegram_lines(board, env.clock.now())
    )


async def test_new_settings_replace_the_old_ones_at_once(env: Env) -> None:
    first = await grant_money(env, "5000")
    second = await grant_money(env, "8000", care="CAREFUL")
    async with env.db.connect() as conn:
        rows = {r.id: r for r in await conn.execute(select(trading_mandates))}
    assert rows[first].status == "REVOKED" and rows[first].end_reason == "replaced by new settings"
    assert rows[second].status == "ACTIVE" and rows[second].max_loss_per_trade == D("40")
    with pytest.raises(ConflictError, match=r"holds 10,000\.00"):
        await grant_money(env, "20000")
