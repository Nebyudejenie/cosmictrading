"""Run and keep the rule competition (ADR-031).

Loads the auto-trader's markets with the costs it would pay there, runs ``compete.compete``
in a thread (CPU-bound, so the worker's event loop keeps its heartbeat), and appends the
result to ``auto_competitions``. A run is repeated once a day, or at once when the code
changed; earlier runs stay as they were.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import NotFoundError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import canonical_hash_lenient, to_jsonable_lenient
from cosmic.core.money import BPS
from cosmic.db.engine import Database
from cosmic.db.schema_research import auto_competitions
from cosmic.market import data as md
from cosmic.research.library import auto, compete, expect, hypotheses
from cosmic.research.library.catalog import SPECS, Spec
from cosmic.research.library.engine import Market
from cosmic.trading import venue_terms
from cosmic.trading.policy import active_policy

# (the instrument the auto-trader trades, the series its signal comes from)
MARKETS: tuple[tuple[str, str], ...] = (
    ("CFD:BTC/USD@HFM", "SPOT:BTC/USDT@BINANCE"),
    ("CFD:XAU/USD@HFM", "SPOT:PAXG/USDT@BINANCE"),
)
SPEC_POOL: tuple[Spec, ...] = SPECS  # every rule of the library competes
IDEA_POOL: tuple[Spec, ...] = hypotheses.IDEAS  # and this system's ideas, judged forward only
HOURLY_BARS = 24 * 400
DAILY_BARS = 5_000
RERUN_AFTER = timedelta(hours=20)
SPREAD_WINDOW = timedelta(hours=24)
ASSUMED_SPREAD = Decimal("0.001")  # 0.1 %: ASSUMPTION where no venue quotes are stored


def _code_version() -> str:
    """Hash of the code a verdict depends on: a change is a new, separately recorded run."""
    digest = hashlib.sha256()
    here = Path(__file__).parent
    root = here.parent.parent  # src/cosmic
    files = [
        here / name
        for name in (
            "compete.py",
            "compete_store.py",
            "expect.py",
            "catalog.py",
            "engine.py",
            "ta.py",
            "hypotheses.py",
            "auto.py",
        )
    ]
    files += [root / "trading" / "venue_terms.py", root / "market" / "indicators.py"]
    for f in files:
        digest.update(f.name.encode() + b"\0" + f.read_bytes())
    return digest.hexdigest()[:16]


CODE_VERSION = _code_version()


@dataclass(frozen=True)
class Run:
    id: Any
    created_at: datetime
    code_version: str
    forward_from: datetime
    data_until: datetime
    trials: int
    result: dict[str, Any]

    @property
    def entries(self) -> list[dict[str, Any]]:
        return list(self.result.get("entries") or [])

    def tiers(self) -> dict[str, int]:
        out = dict.fromkeys(compete.TIERS, 0)
        for e in self.entries:
            out[e["tier"]] = out.get(e["tier"], 0) + 1
        return out

    def earned(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e.get("earned")]

    def ideas(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e.get("idea")]

    def forward_leaders(self, limit: int = 12) -> list[dict[str, Any]]:
        """Everything with forward trades, best forward result first (the forward test is
        free of hindsight, so ranking it is fair)."""
        traded = [e for e in self.entries if (e.get("forward") or {}).get("trades")]
        traded.sort(key=lambda e: -(e["forward"]["total_r"] or 0.0))
        return traded[:limit]

    def find(self, rule: str, timeframe: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["rule"] == rule and e["timeframe"] == timeframe]


def verdict_line(run: Run | None, rule: str) -> str:
    """One line for Telegram and the auto-trader page."""
    if run is None:
        return "Rule competition: the first run comes within a few hours."
    tiers = run.tiers()
    mine = run.find(rule, "1h")
    yours = (
        f" Your rule on hourly bars: {', '.join(sorted({e['tier'] for e in mine}))}."
        if mine
        else ""
    )
    return (
        f"Rule competition ({run.created_at:%d %b}): {len(run.earned())} earned the auto-trader, "
        f"{tiers['QUALIFIED']} qualified, {tiers['PROMISING']} promising of {run.trials} tested."
        + yours
    )


async def latest_run(conn: AsyncConnection) -> Run | None:
    row = (
        await conn.execute(
            select(auto_competitions).order_by(desc(auto_competitions.c.created_at)).limit(1)
        )
    ).first()
    if row is None:
        return None
    return Run(
        row.id,
        row.created_at,
        row.code_version,
        row.forward_from,
        row.data_until,
        row.trials,
        row.result,
    )


def _market(key: str, bars: list[md.Bar], *, dates: bool = False) -> Market:
    return Market(
        key,
        tuple(b.open_time.date() if dates else b.open_time for b in bars),
        tuple(float(b.open) for b in bars),
        tuple(float(b.high) for b in bars),
        tuple(float(b.low) for b in bars),
        tuple(float(b.close) for b in bars),
        tuple(float(b.volume) for b in bars),
    )


async def _costs(
    conn: AsyncConnection, inst: md.InstrumentView, now: datetime
) -> tuple[float, float, bool]:
    """(fee per fill, half spread, measured?) as the auto-trader would pay on ``inst``."""
    _, policy = await active_policy(conn)
    fee = policy.fee_bps_for(inst.venue_key) / BPS
    spread = await md.median_spread(conn, inst.id, now, SPREAD_WINDOW)
    measured = spread is not None
    return float(fee), float(spread if spread is not None else ASSUMED_SPREAD) / 2, measured


def _swap(inst: md.InstrumentView, price: float) -> tuple[float, int | None, dict[str, Any]]:
    """(long swap per night, triple weekday, how it was set) from the venue's report."""
    sw = venue_terms.swap_per_night(inst.metadata, price)
    if sw is None:
        return 0.0, None, {"swap_known": False}
    triple = sw.triple_weekday if inst.base in venue_terms.METALS else None
    return (
        sw.long,
        triple,
        {
            "swap_known": True,
            "swap_pct": round(sw.long * 100, 6),
            "swap_triple_day": sw.triple_day if triple is not None else None,
        },
    )


async def _hourly(
    conn: AsyncConnection, inst: md.InstrumentView, key: str, now: datetime
) -> Market | None:
    """The backtest's hourly bars before the forward date, chosen by date so they are the same
    on every run, then every bar since the forward date (the forward test's record)."""
    before = await md.complete_bars(
        conn, inst.id, "1h", limit=HOURLY_BARS, until=compete.FORWARD_FROM
    )
    hours = max(0, int((now - compete.FORWARD_FROM).total_seconds() // 3600)) + 48
    after = [
        b
        for b in await md.complete_bars(conn, inst.id, "1h", limit=hours)
        if b.open_time >= compete.FORWARD_FROM
    ]
    bars = before + after
    return _market(key, bars) if len(bars) >= auto.MIN_BARS else None


async def load_series(
    conn: AsyncConnection, now: datetime
) -> tuple[list[compete.Series], dict[str, Any]]:
    """Each market on hourly and daily bars, with its costs; and how the costs were set."""
    out: list[compete.Series] = []
    basis: dict[str, Any] = {}
    for traded, signal in MARKETS:
        try:
            series_inst = await md.instrument_by_key(conn, signal)
        except NotFoundError:
            continue
        try:
            inst = await md.instrument_by_key(conn, traded)
        except NotFoundError:  # the venue is not configured here: its signal market stands in
            inst, traded = series_inst, signal
        fee, half, measured = await _costs(conn, inst, now)
        basis[traded] = {
            "signal": signal,
            "fee_pct": round(fee * 100, 6),
            "spread_pct": round(half * 200, 6),
            "spread_measured": measured,
        }
        hourly = await _hourly(conn, series_inst, signal, now)
        daily = await md.complete_bars(conn, series_inst.id, "1d", limit=DAILY_BARS)
        if hourly is not None:
            last = hourly.close[-1]
        elif daily:
            last = float(daily[-1].close)
        else:
            continue
        swap, triple, swap_basis = _swap(inst, last)
        basis[traded].update(swap_basis)
        closed = inst.base in venue_terms.METALS  # gold CFDs do not trade at weekends
        if hourly is not None:
            out.append(compete.Series(traded, "1h", hourly, fee, half, None, swap, triple, closed))
        if daily:
            out.append(
                compete.Series(
                    traded,
                    "1d",
                    _market(signal, daily),
                    fee,
                    half,
                    _market(signal, daily, dates=True),
                    swap,
                    triple,
                    closed,
                )
            )
    return out, basis


async def run_if_due(db: Database, now: datetime) -> dict[str, Any]:
    async with db.connect() as conn:
        latest = await latest_run(conn)
        if (
            latest is not None
            and latest.code_version == CODE_VERSION
            and now - latest.created_at < RERUN_AFTER
        ):
            return {"status": "CURRENT", "run": str(latest.id)}
        series, basis = await load_series(conn, now)
    if not series:
        return {"status": "NO_DATA"}
    report = await asyncio.to_thread(compete.compete, series, SPEC_POOL, None, IDEA_POOL)
    if not report.windows:
        return {"status": "NO_DATA"}
    result = {**report.as_dict(), "costs": basis, "slippage_pct": expect.SLIPPAGE * 100}
    data_until = max((w.last or w.end) for w in report.windows.values())
    run_id = uuid7()
    async with db.transaction() as conn:
        await conn.execute(
            auto_competitions.insert().values(
                id=run_id,
                created_at=now,
                code_version=CODE_VERSION,
                forward_from=compete.FORWARD_FROM,
                data_until=data_until,
                trials=report.trials,
                result=to_jsonable_lenient(result),
                result_hash=canonical_hash_lenient(result),
            )
        )
    return {"status": "COMPLETED", "run": str(run_id), "trials": report.trials}
