"""Persist and read the strategy library: evaluation runs, per-rule results, activations.

Research series and trading instruments are separate on purpose: each asset is evaluated on
its longest clean daily history, and a switched-on rule proposes paper trades on the
instrument the paper venue can fill.

| Asset | Research series (daily)          | Paper trading instrument   |
|-------|----------------------------------|----------------------------|
| BTC   | Bitstamp BTC/USD (since 2011)    | Coinbase BTC/USD           |
| Gold  | Binance PAXG/USDT (since 2020)   | Coinbase PAXG/USD          |

Every run is evaluated under each cost scenario; a rule's tier for a trading instrument is the
tier under that venue's scenario.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import canonical_hash, canonical_hash_lenient, to_jsonable_lenient
from cosmic.db.engine import Database
from cosmic.db.schema_market import instruments
from cosmic.db.schema_research import (
    library_runs,
    strategies,
    strategy_activations,
    strategy_evaluations,
    strategy_versions,
)
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.market import data as md
from cosmic.research.library.catalog import BY_KEY, SPECS, Spec
from cosmic.research.library.engine import (
    BINANCE_LIKE,
    COINBASE_LIKE,
    Costs,
    Market,
    align,
)
from cosmic.research.library.evaluate import (
    LibraryReport,
    SeriesReport,
    evaluate_panel,
    evaluate_single,
    judge,
)

MIN_BARS = 400
MAX_ACTIVE = 10
ACTIVATABLE = ("QUALIFIED", "WATCH")


@dataclass(frozen=True)
class ResearchAsset:
    asset: str
    research_key: str  # the series rules are judged on (recorded evaluations)
    trade_keys: tuple[str, ...]  # where proposals go, preferred first
    signal_key: str = ""  # the daily bars live signals use; the research series if empty

    @property
    def signal_series(self) -> str:
        return self.signal_key or self.research_key


# ADR-027: judged on the long stored histories, applied where the paper account trades. Bitstamp
# is retired, so BTC signals come from Binance's own daily bars; Coinbase keys stay only so
# existing activations still resolve (Coinbase instruments are no longer tradable).
ASSETS: tuple[ResearchAsset, ...] = (
    ResearchAsset(
        "BTC",
        "SPOT:BTC/USD@BITSTAMP",
        ("SPOT:BTC/USDT@BINANCE", "SPOT:BTC/USD@COINBASE"),
        "SPOT:BTC/USDT@BINANCE",
    ),
    ResearchAsset(
        "GOLD", "SPOT:PAXG/USDT@BINANCE", ("SPOT:PAXG/USDT@BINANCE", "SPOT:PAXG/USD@COINBASE")
    ),
)
SCENARIOS: tuple[Costs, ...] = (BINANCE_LIKE, COINBASE_LIKE)
VENUE_SCENARIO = {"COINBASE": COINBASE_LIKE.name, "BINANCE": BINANCE_LIKE.name}
PANEL_SERIES = "+".join(a.research_key for a in ASSETS)


def _code_version() -> str:
    """Hash of the library's source: a code change is a new, separately recorded version."""
    digest = hashlib.sha256()
    here = Path(__file__).parent
    for name in ("catalog.py", "engine.py", "evaluate.py", "stats.py", "ta.py"):
        digest.update(name.encode() + b"\0" + (here / name).read_bytes())
    return digest.hexdigest()[:16]


CODE_VERSION = _code_version()


def asset_for_trade(instrument_key: str) -> ResearchAsset:
    for a in ASSETS:
        if instrument_key in a.trade_keys:
            return a
    raise ValidationFailedError(f"{instrument_key} is not a library trading instrument")


def series_for(spec: Spec, asset: ResearchAsset) -> str:
    return PANEL_SERIES if spec.kind == "PANEL" else asset.research_key


async def load_market(conn: AsyncConnection, key: str) -> Market | None:
    """Completed daily bars of one instrument as floats (analytics only, ADR-007)."""
    try:
        inst = await md.instrument_by_key(conn, key)
    except NotFoundError:
        return None
    bars = await md.complete_bars(conn, inst.id, "1d", limit=20_000)
    if len(bars) < MIN_BARS:
        return None
    return Market(
        key,
        tuple(b.open_time.date() for b in bars),
        tuple(float(b.open) for b in bars),
        tuple(float(b.high) for b in bars),
        tuple(float(b.low) for b in bars),
        tuple(float(b.close) for b in bars),
        tuple(float(b.volume) for b in bars),
    )


async def ensure_spec(conn: AsyncConnection, spec: Spec, now: datetime) -> uuid.UUID:
    description = f"{spec.rule} Source: {spec.source}." + (
        f" Note: {spec.note}" if spec.note else ""
    )
    await conn.execute(
        pg_insert(strategies)
        .values(id=uuid7(), key=spec.key, name=spec.name, description=description, created_at=now)
        .on_conflict_do_nothing(index_elements=[strategies.c.key])
    )
    sid = (
        await conn.execute(select(strategies.c.id).where(strategies.c.key == spec.key))
    ).scalar_one()
    digest = canonical_hash(
        {
            "key": spec.key,
            "params": {k: str(v) for k, v in spec.params.items()},
            "code": CODE_VERSION,
        }
    )
    existing = (
        await conn.execute(select(strategy_versions.c.id).where(strategy_versions.c.hash == digest))
    ).first()
    if existing is not None:
        return uuid.UUID(str(existing.id))
    n = int(
        await conn.scalar(
            select(func.count())
            .select_from(strategy_versions)
            .where(strategy_versions.c.strategy_id == sid)
        )
        or 0
    )
    vid = uuid7()
    await conn.execute(
        strategy_versions.insert().values(
            id=vid,
            strategy_id=sid,
            version=n + 1,
            params=to_jsonable_lenient(spec.params),
            code_ref=f"cosmic.research.library.catalog:{spec.key}@{CODE_VERSION}",
            hash=digest,
            status="RESEARCH",
            created_at=now,
        )
    )
    return vid


def evaluate_all(markets: dict[str, Market], costs: Costs) -> LibraryReport:
    reports: list[SeriesReport] = [evaluate_single(markets[a.research_key], costs) for a in ASSETS]
    reports.append(evaluate_panel(align([markets[a.research_key] for a in ASSETS]), costs))
    return judge(reports)


async def run_library(
    db: Database, now: datetime, scenarios: tuple[Costs, ...] = SCENARIOS
) -> dict[str, Any]:
    """Evaluate all 100 rules on every research series under the given cost scenarios.

    CPU-bound evaluation runs in a thread so the worker's event loop keeps its heartbeat.
    """
    async with db.connect() as conn:
        markets: dict[str, Market] = {}
        missing = []
        for a in ASSETS:
            m = await load_market(conn, a.research_key)
            if m is None:
                missing.append(a.research_key)
            else:
                markets[a.research_key] = m
    if missing:
        return {"status": "INSUFFICIENT_DATA", "missing": missing, "min_bars": MIN_BARS}
    out: dict[str, Any] = {"status": "COMPLETED", "runs": {}}
    for costs in scenarios:
        report = await asyncio.to_thread(evaluate_all, markets, costs)
        async with db.transaction() as conn:
            run_id = await _store(conn, report, costs, markets, now)
        out["runs"][costs.name] = str(run_id)
    return out


async def _store(
    conn: AsyncConnection,
    report: LibraryReport,
    costs: Costs,
    markets: dict[str, Market],
    now: datetime,
) -> uuid.UUID:
    ids = {
        r.canonical_key: r.id
        for r in await conn.execute(
            select(instruments.c.id, instruments.c.canonical_key).where(
                instruments.c.canonical_key.in_(list(markets))
            )
        )
    }
    versions = {s.key: await ensure_spec(conn, s, now) for s in SPECS}
    rows = []
    series_meta = []
    for sr in report.series:
        bh = {k: v.as_dict() for k, v in sr.buy_hold.items()}
        tiers: dict[str, int] = {}
        for e in sr.evaluations:
            tiers[e.tier] = tiers.get(e.tier, 0) + 1
            rows.append(
                {
                    "id": uuid7(),
                    "strategy_version_id": versions[e.spec.key],
                    "series": sr.series,
                    "instrument_id": ids.get(sr.series),
                    "tier": e.tier,
                    "rank": e.rank,
                    "deflated_sharpe": Decimal(str(round(e.dsr, 8))),
                    "timing_sharpe": Decimal(str(round(e.timing_sr * 365**0.5, 8))),
                    "result": to_jsonable_lenient(e.as_dict()),
                    "created_at": now,
                }
            )
        series_meta.append(
            {
                "series": sr.series,
                "from": sr.dates[0],
                "to": sr.dates[1],
                "bars": sr.bars,
                "evaluated_from_bar": sr.windows.start,
                "validation_from_bar": sr.windows.split,
                "buy_and_hold": bh,
                "tiers": tiers,
                "var_timing_sharpe": report.var_sr.get(sr.series),
            }
        )
    content = [r["result"] for r in rows]
    run_id = uuid7()
    await conn.execute(
        library_runs.insert().values(
            id=run_id,
            started_at=now,
            finished_at=now,
            code_version=CODE_VERSION,
            trials=report.trials,
            costs={
                "name": costs.name,
                "fee_bps": costs.fee_bps,
                "half_spread_bps": costs.half_spread_bps,
                "slippage_bps": costs.slippage_bps,
                "basis": costs.basis,
            },
            series=to_jsonable_lenient(series_meta),
            summary={"disclaimer": report.disclaimer, "specs": len(SPECS)},
            result_hash=canonical_hash_lenient(content),
            created_at=now,
        )
    )
    for r in rows:
        r["library_run_id"] = run_id
    await conn.execute(strategy_evaluations.insert(), rows)
    await record(
        conn,
        action="research.library_evaluated",
        actor=Actor.worker("quant_analyst"),
        occurred_at=now,
        entity_type="library_run",
        entity_id=run_id,
        payload={
            "costs": costs.name,
            "trials": report.trials,
            "code_version": CODE_VERSION,
            "tiers": {m["series"]: m["tiers"] for m in series_meta},
        },
    )
    return run_id


async def latest_run(conn: AsyncConnection, costs_name: str) -> Any:
    return (
        await conn.execute(
            select(library_runs)
            .where(library_runs.c.costs["name"].astext == costs_name)
            .order_by(desc(library_runs.c.created_at))
            .limit(1)
        )
    ).first()


async def run_evaluations(conn: AsyncConnection, run_id: uuid.UUID) -> list[Any]:
    return list(
        await conn.execute(
            select(strategy_evaluations, strategies.c.key.label("strategy_key"))
            .join(
                strategy_versions,
                strategy_versions.c.id == strategy_evaluations.c.strategy_version_id,
            )
            .join(strategies, strategies.c.id == strategy_versions.c.strategy_id)
            .where(strategy_evaluations.c.library_run_id == run_id)
            .order_by(strategy_evaluations.c.series, strategy_evaluations.c.rank)
        )
    )


async def evaluation_for(conn: AsyncConnection, spec: Spec, instrument_key: str) -> Any:
    """The latest evaluation that governs ``spec`` on a trading instrument (venue's costs)."""
    asset = asset_for_trade(instrument_key)
    inst = await md.instrument_by_key(conn, instrument_key)
    run = await latest_run(conn, VENUE_SCENARIO.get(inst.venue_key, COINBASE_LIKE.name))
    if run is None:
        return None
    return (
        await conn.execute(
            select(strategy_evaluations)
            .join(
                strategy_versions,
                strategy_versions.c.id == strategy_evaluations.c.strategy_version_id,
            )
            .join(strategies, strategies.c.id == strategy_versions.c.strategy_id)
            .where(
                strategy_evaluations.c.library_run_id == run.id,
                strategies.c.key == spec.key,
                strategy_evaluations.c.series == series_for(spec, asset),
            )
        )
    ).first()


async def activations(conn: AsyncConnection) -> dict[tuple[str, uuid.UUID], Any]:
    """Latest decision per (rule, instrument)."""
    latest: dict[tuple[str, uuid.UUID], Any] = {}
    for r in await conn.execute(
        select(strategy_activations).order_by(strategy_activations.c.occurred_at)
    ):
        latest[(r.strategy_key, r.instrument_id)] = r
    return latest


async def active(conn: AsyncConnection) -> list[Any]:
    return [r for r in (await activations(conn)).values() if r.action == "ACTIVATE"]


async def set_active(
    conn: AsyncConnection,
    *,
    principal: Principal,
    strategy_key: str,
    instrument_key: str,
    on: bool,
    reason: str,
    acknowledged: bool,
    settings: Settings,
    now: datetime,
) -> None:
    """Owner switches a rule's paper proposals on or off (ADMINISTER_SYSTEM, fresh MFA).

    Switching on needs the governing evaluation to be QUALIFIED or WATCH; a WATCH rule needs
    the owner to acknowledge that it is a forward test, not a proven edge.
    """
    require(principal, Capability.ADMINISTER_SYSTEM, now=now, settings=settings)
    spec = BY_KEY.get(strategy_key)
    if spec is None:
        raise NotFoundError("unknown strategy")
    if len(reason.strip()) < 5:
        raise ValidationFailedError("give a reason of at least 5 characters")
    asset = asset_for_trade(instrument_key)
    inst = await md.instrument_by_key(conn, instrument_key)
    current = (await activations(conn)).get((spec.key, inst.id))
    is_on = current is not None and current.action == "ACTIVATE"
    if on == is_on:
        raise ConflictError("already " + ("on" if on else "off"))
    evaluation = await evaluation_for(conn, spec, instrument_key)
    if on:
        if evaluation is None:
            raise ConflictError("no evaluation yet; run the library first")
        if evaluation.tier not in ACTIVATABLE:
            raise ConflictError(
                f"{spec.name} is {evaluation.tier} on {asset.asset}; only "
                "QUALIFIED or WATCH rules can propose paper trades"
            )
        if evaluation.tier == "WATCH" and not acknowledged:
            raise ValidationFailedError(
                "this rule did not pass the significance test; confirm that paper trading it is "
                "a forward test, not a proven edge"
            )
        if len(await active(conn)) >= MAX_ACTIVE:
            raise ConflictError(f"at most {MAX_ACTIVE} rules can be on at once")
    await conn.execute(
        strategy_activations.insert().values(
            id=uuid7(),
            strategy_key=spec.key,
            instrument_id=inst.id,
            action="ACTIVATE" if on else "DEACTIVATE",
            evaluation_id=evaluation.id if (on and evaluation is not None) else None,
            actor_type=principal.actor.type.value,
            actor_id=str(principal.actor.id),
            reason=reason.strip()[:300],
            occurred_at=now,
        )
    )
    await record(
        conn,
        action="research.strategy_activated" if on else "research.strategy_deactivated",
        actor=principal.actor,
        occurred_at=now,
        entity_type="strategy",
        entity_id=spec.key,
        payload={
            "instrument": instrument_key,
            "tier": evaluation.tier if evaluation is not None else None,
            "deflated_sharpe": evaluation.deflated_sharpe if evaluation is not None else None,
            "reason": reason.strip()[:300],
            "paper_only": True,
        },
    )
