"""The bridge's fast lane (S87; the owner's rule of 2026-11-12: an order leaves for HFM within
one second of its prediction).

The bridge's report (``bridge_sync``) carries the whole account, and measured in production it
arrives every two to three seconds: HFM's price reached the robot that late, and an order waited
for the next report to be picked up. The fast lane is a second, small request the bridge keeps
open all the time:

- it brings HFM's newest prices, a few times a second. They are kept apart from the report
  (``bridge_fast``) and put into HFM's forming candles, so a 1-minute candle is complete, and
  the robot decides, a fraction of a second after the minute turns;
- it brings the results of the orders the lane ran;
- the server holds it for up to ``hold_ms`` and answers the moment an order waits.

Only orders travel on it (``FAST_KINDS``), and only while the bridge's last full report is
fresh and shows the demo account the server trades: the report's own rule
(``mt5_bridge.report_problem``), read here from the stored report. Sign-ins, candles and
everything else still go with the report. A bridge that never asks (before version 8) simply
does not use it.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.logs import get_logger
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema_trading import bridge_commands, bridge_fast, bridge_state
from cosmic.market import hfm_candles
from cosmic.trading.bridge_sync import FAST_KINDS, BridgeResult, _claim
from cosmic.trading.brokers.mt5_bridge import BRIDGES, opened_on, report_problem

log = get_logger(__name__)
FAST_VERSION = 8  # the first bridge with the fast lane
HOLD_MS = 250  # how long the bridge is told to let the server hold a request
HOLD_MAX_MS = 1000
POLL_SECONDS = 0.02  # how often a held request looks for a waiting order
OFFER = {"path": "/bridge/v1/fast", "hold_ms": HOLD_MS}  # told to the bridge with each report


class BridgeFast(BaseModel):
    """What the bridge sends on its fast lane. Prices are passed through as MT5 reports them."""

    model_config = ConfigDict(extra="forbid")
    bridge: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    version: str = Field(max_length=40)
    sent_at_ms: int
    server_utc_offset_ms: int | None = Field(default=None, ge=-14 * 3_600_000, le=14 * 3_600_000)
    ticks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    results: list[BridgeResult] = Field(default_factory=list)
    hold_ms: int = Field(default=0, ge=0, le=HOLD_MAX_MS)
    # the bridge's own measurements: its clock against the server's, the round trip, its report
    info: dict[str, Any] = Field(default_factory=dict)


async def _may_trade(db: Database, bridge: str, now: datetime) -> bool:
    """Whether an order may go to this bridge now: its last full report is fresh and shows the
    demo account opened on its adapter. Fail closed: no report, no orders on this lane."""
    spec = BRIDGES.get(bridge)
    if spec is None:
        return False
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(
                    bridge_state.c.received_at, bridge_state.c.snapshot["account"].label("account")
                ).where(bridge_state.c.bridge == bridge)
            )
        ).first()
        if row is None:
            return False
        expected = await opened_on(conn, spec.adapter)
    report = {"account": row.account or {}}  # all the rule reads of a report
    return report_problem(report, row.received_at, now, expected) is None


async def _wait_for_order(db: Database, bridge: str, hold_ms: int) -> bool:
    """Whether an order waits for this bridge: now, or within ``hold_ms``. One connection for
    the whole hold, asked every ``POLL_SECONDS`` (each statement sees what is committed)."""
    waiting = (
        select(bridge_commands.c.id)
        .where(
            bridge_commands.c.bridge == bridge,
            bridge_commands.c.status == "PENDING",
            bridge_commands.c.kind.in_(sorted(FAST_KINDS)),
        )
        .limit(1)
    )
    deadline = time.monotonic() + hold_ms / 1000
    async with db.connect() as conn:
        while True:
            if await conn.scalar(waiting) is not None:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(POLL_SECONDS)


async def apply_fast(
    db: Database, clock: Clock, fast: BridgeFast, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Store the prices and the results, then hand out the orders waiting for this bridge: at
    once, or the moment one arrives within ``hold_ms``."""
    now = clock.now()
    spec = BRIDGES.get(fast.bridge)
    if spec is None:  # the read-only terminal, or an unknown bridge: no orders, no lane
        return []
    tick = (fast.ticks or {}).get(hfm_candles.SYMBOL)
    async with db.transaction() as conn:
        before = await conn.scalar(
            select(bridge_fast.c.ticks).where(bridge_fast.c.bridge == fast.bridge).with_for_update()
        )
        values = {
            "received_at": now,
            "ticks": to_jsonable(fast.ticks),
            "server_utc_offset_ms": fast.server_utc_offset_ms,
            "info": to_jsonable(fast.info),
        }
        await conn.execute(
            pg_insert(bridge_fast)
            .values(bridge=fast.bridge, **values)
            .on_conflict_do_update(index_elements=[bridge_fast.c.bridge], set_=values)
        )
        for r in fast.results:
            await conn.execute(
                update(bridge_commands)
                .where(
                    bridge_commands.c.id == r.command_id,
                    bridge_commands.c.bridge == fast.bridge,
                    bridge_commands.c.status.in_(["PENDING", "SENT"]),
                )
                .values(
                    status=r.status, result=to_jsonable(r.result), error=r.error, finished_at=now
                )
            )
    trading = await _may_trade(db, fast.bridge, now)
    new_tick = tick is not None and tick != (before or {}).get(hfm_candles.SYMBOL)
    if spec.primary and trading and new_tick:
        # HFM's own candles, from this price (S61): the same step as the report's, far more
        # often. Never able to stop the lane.
        try:
            async with db.transaction() as conn:
                await hfm_candles.record(conn, now, tick, fast.server_utc_offset_ms)
        except Exception as exc:
            log.warning("hfm_candles_failed", error=scrub_text(str(exc))[:300])
    if not trading:
        return []
    if not await _wait_for_order(db, fast.bridge, fast.hold_ms):
        return []
    return await _claim(db, fast.bridge, clock.now(), settings, trading=True, only=FAST_KINDS)
