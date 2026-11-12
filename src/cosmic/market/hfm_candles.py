"""HFM's own gold candles (S61): no other venue's prices.

The owner (2026-11-04): "Use HFM gold prices and HFM historical candles. Never use Binance gold
prices." Candles of HFM's XAUUSD come from three places, all from HFM:

- **live:** the bridge reports HFM's price every second; each report updates the forming 1-, 5-
  and 15-minute, hourly and daily candles (``record``). Volume counts the reports, not trades.
- **history:** the quotes stored since 2026-10-15 (one every ~15 s), rebuilt into candles once
  (``backfill``). They miss moves between samples: a high may be a little low, a low a little high.
- **the broker's own:** bridge version 4 sends MT5's candles (``store_rates``); they replace the
  rebuilt ones of the same period.

Prices are HFM's bid, as MT5 draws its charts. A daily candle is HFM's trading day, which
starts at 17:00 in New York (00:00 on HFM's server clock), as MT5's daily candles do. A candle is
complete once its period has ended; a complete candle is never changed by a live report.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema_market import bars, instruments, quotes, sources

INSTRUMENT = "CFD:XAU/USD@HFM"
SOURCE = "hfm_mt5_bridge"
SYMBOL = "XAUUSD"
# Timeframe → seconds; the daily candle follows HFM's trading day (``open_of``).
TIMEFRAMES: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
MT5_TIMEFRAMES = {"1m": "M1", "5m": "M5", "15m": "M15", "1h": "H1", "1d": "D1"}
NEW_YORK = ZoneInfo("America/New_York")
TRADING_DAY_START = 17  # HFM's trading day starts at 17:00 New York time
FRESH = timedelta(seconds=10)  # an older tick is a closed market's last price, not a new one
RUN_NAMESPACE = uuid.UUID("6a3f0c0e-6f1e-4c3b-9f1e-5c0d1d0a5c61")


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    complete: bool


def open_of(t: datetime, tf: str) -> datetime:
    """When the candle of ``tf`` that contains ``t`` opened (UTC)."""
    if tf == "1d":
        ny = t.astimezone(NEW_YORK)
        start = ny.replace(hour=TRADING_DAY_START, minute=0, second=0, microsecond=0)
        if ny < start:
            start -= timedelta(days=1)
        return start.astimezone(UTC)
    secs = TIMEFRAMES[tf]
    return datetime.fromtimestamp(int(t.timestamp()) // secs * secs, UTC)


def ends(open_time: datetime, tf: str) -> datetime:
    if tf == "1d":
        return open_of(open_time + timedelta(hours=26), "1d")  # the next trading day's start
    return open_time + timedelta(seconds=TIMEFRAMES[tf])


async def _ids(conn: AsyncConnection) -> tuple[Any, Any] | None:
    inst = await conn.scalar(
        select(instruments.c.id).where(instruments.c.canonical_key == INSTRUMENT)
    )
    src = await conn.scalar(select(sources.c.id).where(sources.c.key == SOURCE))
    return None if inst is None or src is None else (inst, src)


async def _run(conn: AsyncConnection, src: Any, day: str, now: datetime, kind: str) -> uuid.UUID:
    """One fetch run a day per kind (live, backfill, broker) that the candles point to."""
    run = uuid.uuid5(RUN_NAMESPACE, f"{kind}:{day}")
    await conn.execute(
        text(
            "INSERT INTO fetch_runs (id, source_id, kind, started_at, finished_at, status) "
            "VALUES (:id, :s, :k, :n, :n, 'SUCCESS') ON CONFLICT (id) DO NOTHING"
        ),
        {"id": run, "s": src, "k": f"hfm_candles_{kind}", "n": now},
    )
    return run


UPSERT_LIVE = text(
    "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, close, "
    "volume, is_complete, fetch_run_id, ingested_at, updated_at) "
    "VALUES (:i, :s, :tf, :t, :p, :p, :p, :p, 1, false, :r, :n, :n) "
    "ON CONFLICT (instrument_id, source_id, timeframe, open_time) DO UPDATE SET "
    "high = GREATEST(bars.high, EXCLUDED.high), low = LEAST(bars.low, EXCLUDED.low), "
    "close = EXCLUDED.close, volume = bars.volume + 1, updated_at = EXCLUDED.updated_at "
    "WHERE bars.is_complete = false"
)
CLOSE_EARLIER = text(
    "UPDATE bars SET is_complete = true, updated_at = :n WHERE instrument_id = :i AND "
    "source_id = :s AND timeframe = :tf AND open_time < :t AND is_complete = false"
)


async def record(
    conn: AsyncConnection, now: datetime, tick: Mapping[str, Any] | None, offset_ms: int | None
) -> bool:
    """Put HFM's latest price into the forming candles; True when it did. A tick older than
    ``FRESH`` (a closed market's last price) or without a known server clock is skipped."""
    if not tick or offset_ms is None or tick.get("bid") is None or tick.get("time_msc") is None:
        return False
    at = datetime.fromtimestamp((int(tick["time_msc"]) - int(offset_ms)) / 1000, UTC)
    if now - at > FRESH:
        return False
    ids = await _ids(conn)
    if ids is None:
        return False
    inst, src = ids
    price = Decimal(str(tick["bid"]))
    run = await _run(conn, src, f"{now:%Y-%m-%d}", now, "live")
    for tf in TIMEFRAMES:
        t = open_of(at, tf)
        params = {"i": inst, "s": src, "tf": tf, "t": t, "p": price, "r": run, "n": now}
        await conn.execute(UPSERT_LIVE, params)
        await conn.execute(CLOSE_EARLIER, params)
    return True


def aggregate(samples: Iterable[tuple[datetime, Decimal]], tf: str, now: datetime) -> list[Candle]:
    """Candles of ``tf`` from (time, price) samples in time order."""
    out: dict[datetime, list[Decimal]] = {}
    count: dict[datetime, int] = {}
    for at, price in samples:
        t = open_of(at, tf)
        c = out.get(t)
        if c is None:
            out[t] = [price, price, price, price]
            count[t] = 1
        else:
            c[1], c[2], c[3] = max(c[1], price), min(c[2], price), price
            count[t] += 1
    return [
        Candle(t, o, h, low, close, Decimal(count[t]), ends(t, tf) <= now)
        for t, (o, h, low, close) in sorted(out.items())
    ]


INSERT_KEEP = text(
    "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, close, "
    "volume, is_complete, fetch_run_id, ingested_at, updated_at) "
    "VALUES (:i, :s, :tf, :t, :o, :h, :l, :c, :v, :done, :r, :n, :n) "
    "ON CONFLICT (instrument_id, source_id, timeframe, open_time) DO NOTHING"
)


async def backfill(conn: AsyncConnection, now: datetime) -> dict[str, int]:
    """Candles rebuilt from every stored HFM quote, where none is stored yet; per timeframe, how
    many were added."""
    ids = await _ids(conn)
    if ids is None:
        return {}
    inst, src = ids
    rows = list(
        await conn.execute(
            select(quotes.c.source_ts, quotes.c.bid)
            .where(quotes.c.instrument_id == inst, quotes.c.bid.is_not(None))
            .order_by(quotes.c.source_ts)
        )
    )
    samples = [(r.source_ts, Decimal(str(r.bid))) for r in rows]
    run = await _run(conn, src, f"{now:%Y-%m-%d}", now, "backfill")
    added: dict[str, int] = {}
    for tf in TIMEFRAMES:
        n = 0
        for c in aggregate(samples, tf, now):
            if not c.complete:
                continue  # the forming candle is the live one's
            result = await conn.execute(
                INSERT_KEEP,
                {
                    "i": inst,
                    "s": src,
                    "tf": tf,
                    "t": c.open_time,
                    "o": c.open,
                    "h": c.high,
                    "l": c.low,
                    "c": c.close,
                    "v": c.volume,
                    "done": True,
                    "r": run,
                    "n": now,
                },
            )
            n += result.rowcount or 0
        added[tf] = n
    return added


UPSERT_BROKER = text(
    "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, close, "
    "volume, is_complete, fetch_run_id, ingested_at, updated_at) "
    "VALUES (:i, :s, :tf, :t, :o, :h, :l, :c, :v, true, :r, :n, :n) "
    "ON CONFLICT (instrument_id, source_id, timeframe, open_time) DO UPDATE SET "
    "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close, "
    "volume = EXCLUDED.volume, is_complete = true, fetch_run_id = EXCLUDED.fetch_run_id, "
    "updated_at = EXCLUDED.updated_at"
)


SERVER_AHEAD_OF_NEW_YORK = timedelta(hours=7)  # HFM's server clock: 17:00 New York = 00:00


def server_offset_ms(at: datetime) -> int:
    """HFM's server clock ahead of UTC at ``at``, as New York + 7 hours makes it (ms)."""
    ny = at.astimezone(NEW_YORK).utcoffset()
    assert ny is not None
    return int((ny + SERVER_AHEAD_OF_NEW_YORK).total_seconds() * 1000)


def server_to_utc(seconds: int) -> datetime:
    """An MT5 time on HFM's server clock (New York + 7 hours, so UTC+3 in the US summer and UTC+2
    in its winter) in UTC: right on both sides of a daylight-saving change, which today's offset
    alone would not be for older candles."""
    server = datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)
    return (server - SERVER_AHEAD_OF_NEW_YORK).replace(tzinfo=NEW_YORK).astimezone(UTC)


def utc_to_server(at: datetime) -> int:
    """A UTC instant in seconds on HFM's server clock (``server_to_utc`` the other way)."""
    on_clock = at.astimezone(NEW_YORK).replace(tzinfo=None) + SERVER_AHEAD_OF_NEW_YORK
    return int(on_clock.replace(tzinfo=UTC).timestamp())


# The newest row of the candles' own index, read backwards: the same cost however many years of
# candles are stored (it is asked ten times a second).
NEWEST_COMPLETE = text(
    "SELECT open_time FROM bars "
    "WHERE instrument_id = (SELECT id FROM instruments WHERE canonical_key = :k) "
    "AND source_id = (SELECT id FROM sources WHERE key = :s) "
    "AND timeframe = :tf AND is_complete ORDER BY open_time DESC LIMIT 1"
)


async def newest_complete(conn: AsyncConnection, tf: str) -> datetime | None:
    """When the newest complete candle of ``tf`` opened. It changes the moment a candle
    completes: what the robot's loop watches for between its every-second checks (S87)."""
    found: datetime | None = await conn.scalar(
        NEWEST_COMPLETE, {"k": INSTRUMENT, "s": SOURCE, "tf": tf}
    )
    return found


async def earliest(conn: AsyncConnection, tf: str) -> datetime | None:
    """When the oldest stored candle of ``tf`` opened."""
    ids = await _ids(conn)
    if ids is None:
        return None
    inst, src = ids
    found: datetime | None = await conn.scalar(
        select(func.min(bars.c.open_time)).where(
            bars.c.instrument_id == inst, bars.c.source_id == src, bars.c.timeframe == tf
        )
    )
    return found


async def store_rates(
    conn: AsyncConnection,
    tf: str,
    rates: Sequence[Sequence[Any]],
    now: datetime,
) -> int:
    """MT5's own candles (bridge version 4): [server time in s, open, high, low, close, tick
    volume] each. The closed ones replace any candle of the same period; the forming one is left
    to the live reports."""
    ids = await _ids(conn)
    if ids is None:
        return 0
    inst, src = ids
    run = await _run(conn, src, f"{now:%Y-%m-%d}", now, "broker")
    n = 0
    for r in rates:
        at = server_to_utc(int(r[0]))
        t = open_of(at, tf) if tf == "1d" else at
        if ends(t, tf) > now:
            continue
        await conn.execute(
            UPSERT_BROKER,
            {
                "i": inst,
                "s": src,
                "tf": tf,
                "t": t,
                "o": Decimal(str(r[1])),
                "h": Decimal(str(r[2])),
                "l": Decimal(str(r[3])),
                "c": Decimal(str(r[4])),
                "v": Decimal(str(r[5])),
                "r": run,
                "n": now,
            },
        )
        n += 1
    return n


async def candles(
    conn: AsyncConnection, tf: str, limit: int, *, complete_only: bool = True
) -> list[Candle]:
    """The latest ``limit`` candles of ``tf``, oldest first. One statement (S87: it is read on
    the way from a prediction to its order): the instrument and the source are looked up in
    it."""
    inst = select(instruments.c.id).where(instruments.c.canonical_key == INSTRUMENT)
    src = select(sources.c.id).where(sources.c.key == SOURCE)
    q = (
        select(
            bars.c.open_time,
            bars.c.open,
            bars.c.high,
            bars.c.low,
            bars.c.close,
            bars.c.volume,
            bars.c.is_complete,
        )
        .where(
            bars.c.instrument_id == inst.scalar_subquery(),
            bars.c.source_id == src.scalar_subquery(),
            bars.c.timeframe == tf,
        )
        .order_by(bars.c.open_time.desc())
        .limit(limit)
    )
    if complete_only:
        q = q.where(bars.c.is_complete)
    rows = list(await conn.execute(q))
    return [
        Candle(r.open_time, r.open, r.high, r.low, r.close, r.volume, r.is_complete)
        for r in reversed(rows)
    ]
