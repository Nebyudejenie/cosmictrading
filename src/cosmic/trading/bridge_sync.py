"""One sync from a MetaTrader 5 bridge (ADR-028): store its snapshot, ingest its prices and
status like any other source, record command results and hand out new commands.

A bridge whose account is not a demo account never receives a trading command (and an incident
says so): the demo-only rule is enforced on the server as well as in the bridge. A terminal signed
in to no account (S110) cannot trade at all: it is not reported as a non-demo account, and only
when an account was opened on it is the owner told that it is signed out. Sign-in and
identity commands (S83, Broker Connections) go to a terminal whatever account it holds; a
sign-in's password is added only to the answer that hands it out, never stored with it. The
read-only terminal (the REAL account) only reports and signs in.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, desc, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.logs import get_logger
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema_market import instruments, quotes, sources, venues
from cosmic.db.schema_trading import bridge_commands, bridge_fast, bridge_state
from cosmic.market import hfm_candles
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident, resolve_by_key
from cosmic.sources.connectors.bridge import (
    BridgeMarketConnector,
    BridgeStatusConnector,
    CentTermsConnector,
)
from cosmic.sources.ingest import IngestService
from cosmic.trading import connections
from cosmic.trading.brokers.mt5_bridge import (
    BRIDGE_REAL,
    BRIDGES,
    CONNECTION_COMMANDS,
    QUOTE,
    QUOTE_SOURCE,
    STATUS_SOURCE,
    VENUE,
    opened_on,
)

log = get_logger(__name__)
CENT_SOURCE = "hfm_cent_terms"  # HFM's prices with the Cent account's terms (S38)
# Pushed every second; stored no more often than this (each store is a recorded fetch run).
EVERY = {QUOTE_SOURCE: timedelta(seconds=15), STATUS_SOURCE: timedelta(seconds=30)}
RULES_EVERY = timedelta(minutes=10)
MAX_COMMANDS = 20
# The fast lane (S87, ``bridge_fast``): the kinds that travel on it (orders only), and how
# recently it must have been heard for the report to leave those to it.
FAST_KINDS = frozenset({"SUBMIT", "CANCEL"})
LANE_UP = timedelta(seconds=1)


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
    # Bridge 8 (S87): how long its report takes (the snapshot, the request, the slowest MT5
    # call), in milliseconds; kept with the report, only ever read.
    timing: dict[str, Any] = Field(default_factory=dict)
    # Bridge 8: this run of the bridge (its start, in ms) and how many commands it had finished
    # when the account was read. A command's result carries its own number (``seq``): only a
    # report that has reached it shows what the command did (``mt5_bridge.reflects``).
    commands_done: list[int] | None = Field(default=None, min_length=2, max_length=2)


async def _due(db: Database, key: str, every: timedelta, now: datetime) -> bool:
    async with db.connect() as conn:
        last = await conn.scalar(select(sources.c.last_success_at).where(sources.c.key == key))
    return last is None or now - last >= every


def last_stored_statement() -> Select[Any]:
    """When HFM's last quote was stored: of each of HFM's instruments, its newest quote (one
    entry of ``quotes_latest``), and of those the one stored last. Asked at every report; it
    used to read every HFM quote ever stored (S92). A bridge's prices only move forward in
    time, so an instrument's newest quote is also the one stored last."""
    newest = (
        select(quotes.c.ingested_at)
        .where(quotes.c.instrument_id == instruments.c.id)
        .order_by(desc(quotes.c.source_ts))
        .limit(1)
        .correlate(instruments)
        .scalar_subquery()
    )
    return (
        select(func.max(newest))
        .select_from(instruments.join(venues, venues.c.id == instruments.c.venue_id))
        .where(venues.c.key == VENUE)
    )


async def _prices_due(db: Database, now: datetime) -> bool:
    """By the last stored HFM quote (refreshing the rules also marks the source as fetched)."""
    async with db.connect() as conn:
        last = await conn.scalar(last_stored_statement())
    return last is None or now - last >= EVERY[QUOTE_SOURCE]


async def _rules_due(db: Database, now: datetime) -> bool:
    async with db.connect() as conn:
        last = await conn.scalar(
            select(func.max(instruments.c.metadata_fetched_at))
            .join(venues, venues.c.id == instruments.c.venue_id)
            .where(venues.c.key == VENUE)
        )
    return last is None or now - last >= RULES_EVERY


async def apply_sync(
    db: Database, clock: Clock, sync: BridgeSync, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """``settings``: the server's, to hand a sign-in its password (none: sign-ins wait)."""
    now = clock.now()
    if sync.bridge == BRIDGE_REAL:
        return await _read_only(db, now, sync, settings)
    bridge = BRIDGES.get(sync.bridge)
    if bridge is None:
        raise ValueError(f"unknown bridge {sync.bridge}")
    # Commands go only to a demo account whose money the server understands (S55: a Cent
    # account's US cents would be read as dollars) and that is the account opened here on this
    # bridge's adapter (S56 review; S74); unclaimed, they end unsent.
    demo = sync.account.get("trade_mode") == "DEMO" and sync.account.get("currency") == QUOTE
    signed_out = not sync.account  # MT5 signed in to no account: it cannot trade at all (S110)
    async with db.connect() as conn:
        expected = await opened_on(conn, bridge.adapter)
    if demo:
        demo = expected is None or sync.account.get("fingerprint") == expected
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
        await _connection_results(conn, sync, now)
        key = f"trading.bridge_not_demo:{sync.bridge}"
        out_key = f"trading.bridge_signed_out:{sync.bridge}"
        if not signed_out:
            await resolve_by_key(conn, dedup_key=out_key, now=now, resolution="signed in again")
        if demo:
            await resolve_by_key(conn, dedup_key=key, now=now, resolution="demo account again")
        elif signed_out:
            # No account at all (a new terminal waiting for its first sign-in, or MT5 restarting):
            # nothing can be traded, so this is not the non-demo alarm. Said only where an account
            # was opened on this bridge, which then cannot trade until it is signed in again.
            await resolve_by_key(conn, dedup_key=key, now=now, resolution="no account signed in")
            if expected is not None:
                await raise_incident(
                    conn,
                    dedup_key=out_key,
                    category=Category.TRADING,
                    alert=Alert(
                        title=f"Bridge {sync.bridge}: MT5 is signed in to no account",
                        severity="WARNING",
                        what_happened="The MetaTrader 5 terminal behind the bridge reports no "
                        "account: it is signed out, or MT5 is starting.",
                        verified="The bridge's account report.",
                        uncertain="Whether MT5 signs in again by itself (it keeps its saved "
                        "login across restarts).",
                        risk_now="No new trade can be sent through this terminal. Open trades "
                        "keep the stop-loss and take-profit HFM holds.",
                        action_taken="No command but a sign-in is sent to this bridge.",
                        owner_action="If it lasts, sign the account in again in Admin → Broker "
                        "Connections.",
                    ),
                    now=now,
                    detail={"bridge": sync.bridge},
                )
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
    if not bridge.primary:
        # Another robot's own account (S74): HFM's prices, candles and status come from the
        # primary bridge only, so one market never has two feeds.
        leave = FAST_KINDS if await _lane_up(db, sync.bridge, now) else None
        return await _claim(db, sync.bridge, now, settings, trading=demo, leave=leave)
    offset = sync.server_utc_offset_ms
    if demo:  # HFM's own candles, from this report's price (S61); never stops the sync
        try:
            async with db.transaction() as conn:
                tick = (sync.ticks or {}).get(hfm_candles.SYMBOL)
                await hfm_candles.record(conn, now, tick, offset)
        except Exception as exc:
            log.warning("hfm_candles_failed", error=scrub_text(str(exc))[:300])
    ingest = IngestService(
        db,
        clock,
        {
            QUOTE_SOURCE: BridgeMarketConnector(QUOTE_SOURCE, sync.ticks, sync.symbols, offset),
            # the same prices with the Cent account's lot rules, for the paper rehearsal (S38)
            CENT_SOURCE: CentTermsConnector(CENT_SOURCE, sync.ticks, sync.symbols, offset),
        },
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
    try:  # the Cent mirror (S38): after HFM's own prices, and never able to stop this sync
        if rules:
            await ingest.sync_instruments(CENT_SOURCE)
        if prices:
            await ingest.ingest_quotes(CENT_SOURCE)
    except Exception as exc:
        log.warning("cent_mirror_failed", error=scrub_text(str(exc))[:300])
    # The heartbeat last: once it is stored, the prices of this sync are too.
    if status:
        await ingest.ingest_venue_status(STATUS_SOURCE)
    # While the bridge's fast lane is up, orders are left to it (S87): it takes one within
    # milliseconds, and an order then always travels the same way. The lane silent for a
    # second: the report carries them again, as before bridge 8.
    leave = FAST_KINDS if await _lane_up(db, sync.bridge, now) else None
    return await _claim(db, sync.bridge, now, settings, trading=demo, leave=leave)


async def _connection_results(conn: Any, sync: BridgeSync, now: datetime) -> None:
    """Sign-in and identity results (S83) onto their accounts; never able to stop a sync."""
    try:
        async with conn.begin_nested():
            await connections.on_results(conn, sync.bridge, list(sync.results), now)
    except Exception as exc:
        log.warning("connection_results_failed", error=scrub_text(str(exc))[:300])


async def _read_only(
    db: Database, now: datetime, sync: BridgeSync, settings: Settings | None
) -> list[dict[str, Any]]:
    """The read-only terminal (S83): the REAL account's report is kept for Broker Connections,
    and only sign-in and identity commands are handed out; nothing here can trade. An account
    signed in with its trading password is said at once (the bridge still refuses orders)."""
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
        await _connection_results(conn, sync, now)
        key = f"trading.real_terminal_can_trade:{sync.bridge}"
        if sync.account.get("trade_allowed") is True:
            await raise_incident(
                conn,
                dedup_key=key,
                category=Category.SECURITY,
                alert=Alert(
                    title="The REAL account is signed in with its trading password",
                    severity="CRITICAL",
                    what_happened="MT5 on the read-only terminal reports that HFM allows "
                    "trading on this sign-in (the master password, not the investor one).",
                    verified="The bridge's account report (account_info.trade_allowed).",
                    uncertain="Nothing about it: it is MT5's own report.",
                    risk_now="Low: the read-only bridge refuses every trading command and the "
                    "server never sends one to this terminal.",
                    action_taken="No trading command is sent to this terminal.",
                    owner_action="Admin → Broker Connections: replace the REAL account's "
                    "password with its investor (read-only) password from myHF.",
                ),
                now=now,
                detail={"bridge": sync.bridge},
            )
        else:
            await resolve_by_key(conn, dedup_key=key, now=now, resolution="read-only sign-in")
    return await _claim(db, sync.bridge, now, settings, trading=False)


async def _lane_up(db: Database, bridge: str, now: datetime) -> bool:
    """Whether the bridge's fast lane was heard within ``LANE_UP``."""
    async with db.connect() as conn:
        heard = await conn.scalar(
            select(bridge_fast.c.received_at).where(bridge_fast.c.bridge == bridge)
        )
    return heard is not None and now - heard <= LANE_UP


WAY_MS = 50  # the way to the bridge when it has not measured its round trip yet
# "Still waiting", with the status written in the statement's text and not passed as a value
# (S92). These statements are prepared once and run many times a second; only with the status
# in the text can the planner see that ``bridge_commands_pending`` (the waiting commands alone,
# nearly always none) answers them. As a value, its plan for "any status" read every command
# ever sent (production, 2026-11-16).
PENDING = bridge_commands.c.status == literal_column("'PENDING'")


def way_of(info: Mapping[str, Any] | None) -> int:
    """One way to the bridge, in milliseconds: half the round trip in ``info``, the bridge's
    own measurements (S87); ``WAY_MS`` while it has measured none."""
    rtt = (info or {}).get("rtt_ms")
    return WAY_MS if not isinstance(rtt, int) or rtt < 0 else rtt // 2


async def _way_ms(conn: Any, bridge: str) -> int:
    """One way to the bridge: from what it last sent on its fast lane. An order's remaining
    time is counted on the bridge from its arrival there."""
    return way_of(
        await conn.scalar(select(bridge_fast.c.info).where(bridge_fast.c.bridge == bridge))
    )


async def _claim(
    db: Database,
    bridge: str,
    now: datetime,
    settings: Settings | None,
    *,
    trading: bool,
    only: frozenset[str] | None = None,
    leave: frozenset[str] | None = None,
    way_ms: int | None = None,
) -> list[dict[str, Any]]:
    """The bridge's pending commands. ``trading`` False: only sign-in and identity commands
    (S83); the others wait (and expire unsent). A sign-in gets its password only here.
    ``only``: the kinds this request may carry (the fast lane's orders, S87); ``leave``: the
    kinds it leaves for the fast lane. ``way_ms``: one way to the bridge on the path this
    request came by (S91: the bridge has two, a few milliseconds and a few hundred apart);
    from what the bridge last sent when not given."""
    out: list[dict[str, Any]] = []
    async with db.transaction() as conn:
        gone = list(
            await conn.execute(
                update(bridge_commands)
                .where(
                    bridge_commands.c.bridge == bridge,
                    PENDING,
                    bridge_commands.c.expires_at < now,
                )
                .values(status="EXPIRED", finished_at=now)
                .returning(bridge_commands.c.id)
            )
        )
        if gone:
            await connections.expired(conn, [g.id for g in gone], now)
        wanted = select(bridge_commands).where(bridge_commands.c.bridge == bridge, PENDING)
        if not trading:
            wanted = wanted.where(bridge_commands.c.kind.in_(sorted(CONNECTION_COMMANDS)))
        if only is not None:
            wanted = wanted.where(bridge_commands.c.kind.in_(sorted(only)))
        if leave is not None:
            wanted = wanted.where(bridge_commands.c.kind.not_in(sorted(leave)))
        rows = list(
            await conn.execute(
                wanted.order_by(bridge_commands.c.created_at)
                .limit(MAX_COMMANDS)
                .with_for_update(skip_locked=True)
            )
        )
        sent = []
        for r in rows:
            payload = (
                await connections.hand_out(conn, settings, r)
                if r.kind in CONNECTION_COMMANDS
                else r.payload
            )
            if payload is not None and payload.get("ttl_ms") is not None:
                # S87: an order with a time limit: what is left of it now. The bridge counts
                # it from the order's arrival there, on its own clock.
                way = way_ms if way_ms is not None else await _way_ms(conn, bridge)
                left = int((r.expires_at - now).total_seconds() * 1000) - way
                payload = {**payload, "ttl_ms": max(left, 0)}
            if payload is None:  # a sign-in without a usable password: never sent
                await conn.execute(
                    update(bridge_commands)
                    .where(bridge_commands.c.id == r.id)
                    .values(
                        status="FAILED",
                        error="no saved password could be read for this sign-in",
                        finished_at=now,
                    )
                )
                continue
            sent.append(r.id)
            out.append(
                {
                    "id": str(r.id),
                    "kind": r.kind,
                    "payload": payload,
                    "expires_at_ms": int(r.expires_at.timestamp() * 1000),
                }
            )
        if sent:
            await conn.execute(
                update(bridge_commands)
                .where(bridge_commands.c.id.in_(sent))
                .values(status="SENT", sent_at=now)
            )
    return out
