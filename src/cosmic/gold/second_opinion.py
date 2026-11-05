"""The AI's second opinion, as the gold engine uses it (S68): it may only stop a new trade.

The opinion is written by ``cosmic.ai.gold_opinion`` (never imported here: the engine reads the
stored row). A fresh opinion that recommends WAIT, or the other direction, stops a BUY or SELL
the rules wanted, with the AI's own reason; it never starts, sizes or closes a trade. Without a
fresh opinion (no free AI configured, or none for ``FRESH``) the rules trade alone, as the owner
chose on 2026-11-04."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema_gold import ai_gold_opinions

FRESH = timedelta(minutes=15)


async def latest(conn: AsyncConnection) -> Any | None:
    return (
        await conn.execute(select(ai_gold_opinions).order_by(desc(ai_gold_opinions.c.at)).limit(1))
    ).first()


async def veto(conn: AsyncConnection, side: str, now: datetime) -> str | None:
    """Why the AI stops this ``side`` (BUY or SELL) now, or None."""
    row = await latest(conn)
    if row is None or now - row.at > FRESH or row.recommendation == side:
        return None
    return (
        f"WAIT — the AI's second opinion ({row.model_id}, {row.at:%H:%M} UTC) says "
        f"{row.recommendation}: {row.why}"
    )
