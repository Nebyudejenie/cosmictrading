"""One sync from a MetaTrader 5 bridge (ADR-028): store its snapshot, ingest its prices and
status like any other source, record command results and hand out new commands.

A bridge whose account is not a demo account never receives a command (and an incident says
so): the demo-only rule is enforced on the server as well as in the bridge.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.clock import Clock
from cosmic.core.jsonutil import to_jsonable
from cosmic.db.engine import Database
from cosmic.db.schema_market import instruments, quotes, sources, venues
from cosmic.db.schema_trading import bridge_commands, bridge_state
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.sources.connectors.bridge import BridgeMarketConnector, BridgeStatusConnector
from cosmic.sources.ingest import IngestService
from cosmic.trading.brokers.mt5_bridge import QUOTE_SOURCE, STATUS_SOURCE, VENUE

# Pushed every second; stored no more often than this (each store is a recorded fetch run).
EVERY = {QUOTE_SOURCE: timedelta(seconds=15), STATUS_SOURCE: timedelta(seconds=30)}
RULES_EVERY = timedelta(minutes=10)
MAX_COMMANDS = 20


class BridgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: uuid.UUID
    status: Literal["DONE", "FAILED"]
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=500)


class BridgeSync(BaseModel):
    """What the bridge sends every second. Values from MT5 are passed through as reported."""

    model_config = ConfigDict(extra="forbid")
    bridge: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    version: str = Field(max_length=40)
    sent_at_ms: int
    # Broker server time minus UTC (MT5 stamps in server time); None until the bridge trusts it.
    server_utc_offset_ms: int | None = Field(default=None, ge=-14 * 3_600_000, le=14 * 3_600_000)
    terminal: dict[str, Any]
    account: dict[str, Any]
    symbols: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ticks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    positions: list[dict[str, Any]] = Field(default_factory=list)
    orders: list[dict[str, Any]] = Field(default_factory=list)
    history_orders: list[dict[str, Any]] = Field(default_factory=list)
    deals: list[dict[str, Any]] = Field(default_factory=list)
    results: list[BridgeResult] = Field(default_factory=list)


async def _due(db: Database, key: str, every: timedelta, now: datetime) -> bool:
    async with db.connect() as conn:
        last = await conn.scalar(select(sources.c.last_success_at).where(sources.c.key == key))
    return last is None or now - last >= every


async def _prices_due(db: Database, now: datetime) -> bool:
    """By the last stored HFM quote (refreshing the rules also marks the source as fetched)."""
    async with db.connect() as conn:
        last = await conn.scalar(
            select(func.max(quotes.c.ingested_at))
            .join(instruments, instruments.c.id == quotes.c.instrument_id)
            .join(venues, venues.c.id == instruments.c.venue_id)
            .where(venues.c.key == VENUE)
        )
    return last is None or now - last >= EVERY[QUOTE_SOURCE]


async def _rules_due(db: Database, now: datetime) -> bool:
    async with db.connect() as conn:
        last = await conn.scalar(
            select(func.max(instruments.c.metadata_fetched_at))
            .join(venues, venues.c.id == instruments.c.venue_id)
            .where(venues.c.key == VENUE)
        )
    return last is None or now - last >= RULES_EVERY


async def apply_sync(db: Database, clock: Clock, sync: BridgeSync) -> list[dict[str, Any]]:
    now = clock.now()
    demo = sync.account.get("trade_mode") == "DEMO"
    async with db.transaction() as conn:
        snapshot = to_jsonable(sync.model_dump(exclude={"results"}))
        await conn.execute(
            pg_insert(bridge_state)
            .values(bridge=sync.bridge, received_at=now, snapshot=snapshot)
            .on_conflict_do_update(
                index_elements=[bridge_state.c.bridge],
                set_={"received_at": now, "snapshot": snapshot},
            )
        )
        for r in sync.results:
            await conn.execute(
                update(bridge_commands)
                .where(
                    bridge_commands.c.id == r.command_id,
                    bridge_commands.c.bridge == sync.bridge,
                    bridge_commands.c.status.in_(["PENDING", "SENT"]),
                )
                .values(
                    status=r.status, result=to_jsonable(r.result), error=r.error, finished_at=now
                )
            )
        key = f"trading.bridge_not_demo:{sync.bridge}"
        if demo:
            await resolve_by_key(conn, dedup_key=key, now=now, resolution="demo account again")
        else:
            await raise_incident(
                conn,
                dedup_key=key,
                category=Category.SECURITY,
                alert=Alert(
                    title=f"Bridge {sync.bridge} reports a non-demo account",
                    severity="CRITICAL",
                    what_happened="The MetaTrader 5 terminal behind the bridge is logged in to "
                    f"an account whose mode is {sync.account.get('trade_mode')!r}.",
                    verified="The bridge's account report.",
                    uncertain="Whether the terminal was switched on purpose.",
                    risk_now="Real money could be at stake; the rule is demo only.",
                    action_taken="No command is sent to this bridge; the bridge also refuses "
                    "to trade on a non-demo account.",
                    owner_action="Log the terminal back in to the HFM demo account.",
                ),
                now=now,
                detail={"bridge": sync.bridge},
            )
    offset = sync.server_utc_offset_ms
    ingest = IngestService(
        db,
        clock,
        {QUOTE_SOURCE: BridgeMarketConnector(QUOTE_SOURCE, sync.ticks, sync.symbols, offset)},
        {STATUS_SOURCE: BridgeStatusConnector(STATUS_SOURCE, sync.terminal, sync.account)},
    )
    # Decide first: storing the rules also marks the quote source as fetched.
    rules = bool(sync.symbols) and await _rules_due(db, now)
    prices = (
        bool(sync.ticks)
        and offset is not None  # tick times cannot be placed in UTC yet: store nothing
        and await _prices_due(db, now)
    )
    status = await _due(db, STATUS_SOURCE, EVERY[STATUS_SOURCE], now)
    if rules:
        await ingest.sync_instruments(QUOTE_SOURCE)
    if prices:
        await ingest.ingest_quotes(QUOTE_SOURCE)
    if status:
        await ingest.ingest_venue_status(STATUS_SOURCE)
    return await _claim(db, sync.bridge, now) if demo else []


async def _claim(db: Database, bridge: str, now: datetime) -> list[dict[str, Any]]:
    async with db.transaction() as conn:
        await conn.execute(
            update(bridge_commands)
            .where(
                bridge_commands.c.bridge == bridge,
                bridge_commands.c.status == "PENDING",
                bridge_commands.c.expires_at < now,
            )
            .values(status="EXPIRED", finished_at=now)
        )
        rows = list(
            await conn.execute(
                select(bridge_commands)
                .where(bridge_commands.c.bridge == bridge, bridge_commands.c.status == "PENDING")
                .order_by(bridge_commands.c.created_at)
                .limit(MAX_COMMANDS)
                .with_for_update(skip_locked=True)
            )
        )
        if rows:
            await conn.execute(
                update(bridge_commands)
                .where(bridge_commands.c.id.in_([r.id for r in rows]))
                .values(status="SENT", sent_at=now)
            )
    return [
        {
            "id": str(r.id),
            "kind": r.kind,
            "payload": r.payload,
            "expires_at_ms": int(r.expires_at.timestamp() * 1000),
        }
        for r in rows
    ]
