"""HFM's own gold candles (S61): live from the bridge's reports, rebuilt from stored prices, and
MT5's own through bridge version 4. No other venue's prices."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import text, update

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema_trading import bridge_state
from cosmic.gold import analysis, sim
from cosmic.market import hfm_candles
from cosmic.trading.bridge_fast import BridgeFast, apply_fast
from cosmic.trading.bridge_sync import BridgeSync, apply_sync
from cosmic.trading.brokers.base import BrokerUnavailableError
from tests.mt5_emulator import TIMEFRAME_D1, TIMEFRAME_H1, MT5Emulator
from tests.trading.test_hfm import bridge_running, build, open_account
from tests.trading.test_hfm_test_trade import _snapshot_until, _weekday


def test_hfm_server_time_is_new_york_plus_seven_hours_all_year() -> None:
    summer = int(datetime(2026, 7, 1, 0, 0, tzinfo=UTC).timestamp())  # 00:00 on HFM's clock
    winter = int(datetime(2026, 11, 4, 0, 0, tzinfo=UTC).timestamp())
    assert hfm_candles.server_to_utc(summer) == datetime(2026, 6, 30, 21, 0, tzinfo=UTC)  # UTC+3
    assert hfm_candles.server_to_utc(winter) == datetime(2026, 11, 3, 22, 0, tzinfo=UTC)  # UTC+2


def test_a_daily_candle_is_hfms_trading_day() -> None:
    # 2026-11-04 21:30 UTC is 16:30 in New York: still the day that began at 17:00 the day before.
    t = datetime(2026, 11, 4, 21, 30, tzinfo=UTC)
    assert hfm_candles.open_of(t, "1d") == datetime(2026, 11, 3, 22, 0, tzinfo=UTC)
    assert hfm_candles.open_of(t + timedelta(minutes=31), "1d") == datetime(
        2026, 11, 4, 22, 0, tzinfo=UTC
    )
    assert hfm_candles.open_of(t, "1h") == datetime(2026, 11, 4, 21, 0, tzinfo=UTC)


async def _bars(db: Database, tf: str) -> list[hfm_candles.Candle]:
    async with db.connect() as conn:
        return await hfm_candles.candles(conn, tf, 5000, complete_only=False)


async def test_the_fast_lanes_price_completes_a_candle_without_waiting_for_a_report(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S87 (the owner's rule of 2026-11-12: the data is delivered at once): HFM's first price
    of a new minute, sent on the fast lane a few times a second, completes the candle before it
    and is the price the robot reads; the report, every two to three seconds, is not waited
    for. Only beside a fresh report of the demo account."""
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path) as bridge:
        await open_account(h)
        await _weekday(h)  # Wednesday 14:00:00 UTC, HFM at 4,100.00 / 4,100.30
    clock = h.env.clock  # the bridge's loops are stopped: each request below is sent by hand

    def at(seconds: int) -> None:
        clock.advance(seconds=seconds)
        emu.now_ms = int(clock.now().timestamp() * 1000)

    async def report() -> None:
        payload = {**bridge.snapshot(), "results": []}
        await apply_sync(db, clock, BridgeSync.model_validate(json.loads(json.dumps(payload))))

    async def fast() -> None:
        asked = BridgeFast.model_validate(json.loads(json.dumps(bridge.fast_request())))
        assert await apply_fast(db, clock, asked) == []

    async def newest() -> datetime | None:
        async with db.connect() as conn:
            return await hfm_candles.newest_complete(conn, "1m")

    at(59)
    await report()  # 14:00:59: the minute's candle is still forming
    assert await newest() != datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
    at(2)
    emu.move("XAUUSD", "4103.00", "4103.30")  # 14:01:01, HFM's first price of the new minute
    emu.now_ms = int(clock.now().timestamp() * 1000)
    await fast()  # no report since
    assert await newest() == datetime(2026, 10, 14, 14, 0, tzinfo=UTC)  # complete at once
    async with db.connect() as conn:
        price = await sim.price_now(conn, clock.now())
        report_bid = await conn.scalar(
            text("SELECT snapshot->'ticks'->'XAUUSD'->>'bid' FROM bridge_state")
        )
    assert price is not None and price.bid == D("4103.00") and report_bid == "4100.0"
    # Without a fresh report the lane's prices are not used: the report says whose they are.
    at(20)
    emu.move("XAUUSD", "4107.00", "4107.30")
    emu.now_ms = int(clock.now().timestamp() * 1000)
    await fast()
    async with db.connect() as conn:
        assert await sim.price_now(conn, clock.now()) is None  # the bridge counts as silent
        forming = await hfm_candles.candles(conn, "1m", 1, complete_only=False)
    assert forming[-1].high == D("4103.00")  # 4,107 never entered a candle


async def test_live_candles_come_from_hfms_reports(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)  # Wednesday 14:00 UTC, HFM at 4,100.00 / 4,100.30
        emu.move("XAUUSD", "4102.00", "4102.30")
        emu.now_ms = int(h.env.clock.now().timestamp() * 1000)
        await _snapshot_until(h, lambda s: str(s["ticks"]["XAUUSD"]["bid"]).startswith("4102"))
        h.env.clock.advance(minutes=1)
        emu.move("XAUUSD", "4099.00", "4099.30")
        emu.now_ms = int(h.env.clock.now().timestamp() * 1000)
        await _snapshot_until(h, lambda s: str(s["ticks"]["XAUUSD"]["bid"]).startswith("4099"))
        for _ in range(200):  # the candle is stored just after the report
            if any(c.open_time.minute == 1 for c in await _bars(db, "1m")):
                break
            await asyncio.sleep(0.02)
    m1 = [c for c in await _bars(db, "1m") if c.open_time >= datetime(2026, 10, 14, tzinfo=UTC)]
    first, second = m1[-2], m1[-1]
    assert first.open_time == datetime(2026, 10, 14, 14, 0, tzinfo=UTC) and first.complete
    assert (first.open, first.high, first.close) == (D("4100.00"), D("4102.00"), D("4102.00"))
    assert second.close == D("4099.00") and not second.complete  # still forming
    (h1,) = [c for c in await _bars(db, "1h") if c.open_time >= datetime(2026, 10, 14, tzinfo=UTC)]
    assert (h1.open, h1.high, h1.low, h1.close) == (
        D("4100.00"),
        D("4102.00"),
        D("4099.00"),
        D("4099.00"),
    )
    d1 = (await _bars(db, "1d"))[-1]
    assert d1.open_time == datetime(2026, 10, 13, 21, 0, tzinfo=UTC)  # 17:00 New York (EDT)


async def test_a_closed_markets_frozen_price_makes_no_candle(
    settings: Settings, db: Database
) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)  # Saturday
    async with db.transaction() as conn:
        from cosmic.sources.ingest import ensure_registry

        await ensure_registry(conn, now)
        tick = {
            "bid": "4100.0",
            "ask": "4100.3",
            "time_msc": int((now - timedelta(hours=19)).timestamp() * 1000),
        }
        assert await hfm_candles.record(conn, now, tick, 0) is False
    assert await _bars(db, "1m") == []


async def test_history_rebuilt_from_stored_prices_never_overwrites(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
    before = await _bars(db, "1h")
    async with db.transaction() as conn:
        await conn.execute(text("DELETE FROM bars WHERE timeframe = '1m'"))
        added = await hfm_candles.backfill(conn, h.env.clock.now() + timedelta(hours=2))
    assert added["1m"] >= 1  # rebuilt from the stored HFM quotes
    async with db.transaction() as conn:
        again = await hfm_candles.backfill(conn, h.env.clock.now() + timedelta(hours=2))
    assert again["1m"] == 0  # never twice
    assert [c.open_time for c in await _bars(db, "1h")][: len(before)] == [
        c.open_time for c in before
    ]


async def test_mt5s_own_candles_through_bridge_version_4(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    # 30 trading days of HFM's daily candles, rising, in HFM's server time (00:00 each day).
    days = [datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=k) for k in range(42)]
    days = [d for d in days if d.weekday() < 5][:30]
    emu.candles[("XAUUSD", TIMEFRAME_D1)] = [
        {
            "time": int(d.timestamp()),
            "open": 3900.0 + k,
            "high": 3910.0 + k,
            "low": 3890.0 + k,
            "close": 3905.0 + k,
            "tick_volume": 1000,
        }
        for k, d in enumerate(days)
    ]
    emu.candles[("XAUUSD", TIMEFRAME_H1)] = []
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        rows = await h.adapter.rates("XAUUSD", "D1", 1000)
        assert len(rows) == 30 and rows[0][1:5] == ["3900.0", "3910.0", "3890.0", "3905.0"]
        async with db.transaction() as conn:
            stored = await hfm_candles.store_rates(conn, "1d", rows, h.env.clock.now())
    assert stored == 30
    d1 = await _bars(db, "1d")
    first = d1[0]
    # 2026-09-01 00:00 on HFM's clock is 2026-08-31 21:00 UTC (summer, UTC+3)
    assert first.open_time == datetime(2026, 8, 31, 21, 0, tzinfo=UTC) and first.complete
    assert (first.open, first.close) == (D("3900.0"), D("3905.0"))
    # With 21 days or more, the daily trend is used.
    broker = [c for c in d1 if c.volume == 1000]  # MT5's own (its tick volume)
    assert len(broker) == 30 and analysis.frame("1d", broker).trend == analysis.UP


async def test_an_older_bridge_is_told_to_update(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
    async with db.transaction() as conn:  # the VM still runs version 3
        await conn.execute(
            update(bridge_state).values(
                snapshot=text("jsonb_set(snapshot, '{version}', '\"3\"')"),
                received_at=h.env.clock.now(),
            )
        )
    with pytest.raises(BrokerUnavailableError, match="update the bridge program to version 4"):
        await h.adapter.rates("XAUUSD", "D1", 10)


async def test_hfms_older_candles_are_read_page_by_page_through_bridge_6(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    """S73, the test lab's history: MT5 holds HFM's older candles; bridge version 6 reads them a
    page at a time back to the first one MT5 has, the server stores them as candles, and the
    read-only answers are not kept twice. Nothing is traded."""
    from types import SimpleNamespace

    from sqlalchemy import delete, select

    from cosmic.db.schema_market import bars
    from cosmic.db.schema_trading import bridge_commands
    from cosmic.gold import jobs
    from tests.mt5_emulator import TIMEFRAME_M1
    from tests.trading.test_hfm import WEDNESDAY

    emu = MT5Emulator()
    emu.server_offset_ms = hfm_candles.server_offset_ms(WEDNESDAY)
    # 5,000 minutes of HFM's gold, ending an hour before the test's Wednesday (server time)
    last = hfm_candles.utc_to_server(WEDNESDAY - timedelta(hours=1))
    emu.candles[("XAUUSD", TIMEFRAME_M1)] = [
        {
            "time": last - 60 * (4999 - k),
            "open": 4000.0 + k / 100,
            "high": 4001.0 + k / 100,
            "low": 3999.0 + k / 100,
            "close": 4000.5 + k / 100,
            "tick_volume": 7,
        }
        for k in range(5000)
    ]
    for tf in (TIMEFRAME_D1, TIMEFRAME_H1):
        emu.candles[("XAUUSD", tf)] = []
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
        await _weekday(h)
        latest = await h.adapter.rates("XAUUSD", "M1", 1500)  # the hourly job's page
        async with db.transaction() as conn:
            # only MT5's minutes: none of the live ones the test's earlier clock made
            await conn.execute(delete(bars).where(bars.c.timeframe == "1m"))
            assert await hfm_candles.store_rates(conn, "1m", latest, h.env.clock.now()) == 1500
        jobs._deep_done.clear()
        ctx = SimpleNamespace(db=db, clock=h.env.clock)
        sent = len(emu.sent)  # the account's contract check traded; the history must not
        pages = []
        for _ in range(5):
            stored = await jobs.deep_history(h.adapter, ctx)  # type: ignore[arg-type]
            pages.append(stored.get("1m", 0))
            if "1m" in jobs._deep_done:
                break
        async with db.connect() as conn:
            first = await hfm_candles.earliest(conn, "1m")
            kept = [
                r.result
                for r in await conn.execute(
                    select(bridge_commands.c.result).where(bridge_commands.c.kind == "RATES")
                )
            ]
    assert pages == [2000, 1500, 0]  # 2,000 + 1,500 older, then MT5 has nothing older
    assert first == hfm_candles.server_to_utc(last - 60 * 4999)
    assert len(await _bars(db, "1m")) == 5000
    assert all(k == {"kept_as": "candles", "rates": k["rates"]} for k in kept)  # not kept twice
    assert len(emu.sent) == sent  # read-only: nothing sent to HFM


async def test_a_bridge_older_than_6_reads_no_older_candles(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await open_account(h)
    async with db.transaction() as conn:  # the VM still runs version 5
        await conn.execute(
            update(bridge_state).values(
                snapshot=text("jsonb_set(snapshot, '{version}', '\"5\"')"),
                received_at=h.env.clock.now(),
            )
        )
    with pytest.raises(BrokerUnavailableError, match="update the bridge program to version 6"):
        await h.adapter.rates("XAUUSD", "M1", 10, before=1_790_000_000)
