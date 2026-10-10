"""Recording and verifying audit events.

Convention: call ``record()`` as the last statement of a transaction. The
database trigger serialises audit inserts until commit to keep the chain linear.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.logs import get_correlation_id
from cosmic.core.redaction import redact
from cosmic.db.schema import audit_events


class ActorType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    WORKER = "WORKER"
    TELEGRAM = "TELEGRAM"
    CLI = "CLI"


@dataclass(frozen=True)
class Actor:
    type: ActorType
    id: str | None

    @staticmethod
    def user(user_id: object) -> Actor:
        return Actor(ActorType.USER, str(user_id))

    @staticmethod
    def system(component: str) -> Actor:
        return Actor(ActorType.SYSTEM, component)

    @staticmethod
    def worker(role: str) -> Actor:
        return Actor(ActorType.WORKER, role)


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    checked: int
    first_bad_seq: int | None
    reason: str | None


async def record(
    conn: AsyncConnection,
    *,
    action: str,
    actor: Actor,
    occurred_at: datetime,
    entity_type: str | None = None,
    entity_id: object | None = None,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> int:
    """Insert one audit event; returns its sequence number."""
    safe_payload = redact(to_jsonable(payload or {}))
    result = await conn.execute(
        insert(audit_events)
        .values(
            id=uuid7(),
            occurred_at=occurred_at,
            actor_type=actor.type.value,
            actor_id=actor.id,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            correlation_id=correlation_id or get_correlation_id(),
            payload=safe_payload,
            # seq/prev_hash/hash are assigned by the database trigger; placeholders are overwritten.
            seq=0,
            prev_hash="",
            hash=str(uuid7()),
        )
        .returning(audit_events.c.seq)
    )
    return int(result.scalar_one())


async def verify_chain(conn: AsyncConnection) -> ChainVerification:
    row = (
        await conn.execute(text("SELECT ok, checked, first_bad_seq, reason FROM audit_verify()"))
    ).one()
    return ChainVerification(
        ok=bool(row.ok),
        checked=int(row.checked),
        first_bad_seq=None if row.first_bad_seq is None else int(row.first_bad_seq),
        reason=row.reason,
    )


async def head(conn: AsyncConnection) -> tuple[int, str] | None:
    row = (
        await conn.execute(
            select(audit_events.c.seq, audit_events.c.hash)
            .order_by(audit_events.c.seq.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else (int(row.seq), str(row.hash))


async def events_for(
    conn: AsyncConnection, entity_type: str, entity_id: object, limit: int = 200
) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(audit_events)
        .where(
            audit_events.c.entity_type == entity_type, audit_events.c.entity_id == str(entity_id)
        )
        .order_by(audit_events.c.seq)
        .limit(limit)
    )
    return [dict(r._mapping) for r in rows]
