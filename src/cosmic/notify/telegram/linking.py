"""Link a portal user to an immutable Telegram user ID with a one-time code."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, ActorType, record
from cosmic.core.errors import ConflictError
from cosmic.core.ids import random_code, uuid7
from cosmic.db.schema import users
from cosmic.db.schema_execution import telegram_link_codes
from cosmic.identity.capabilities import capabilities_for_roles
from cosmic.identity.service import Principal

CODE_TTL = timedelta(minutes=10)


def _hash(code: str) -> bytes:
    return hashlib.sha256(code.strip().upper().encode("ascii", "ignore")).digest()


MAX_TTL = timedelta(minutes=60)


async def create_link_code(conn: AsyncConnection, *, principal: Principal, now: datetime) -> str:
    return await create_link_code_for(
        conn, user_id=principal.user_id, actor=principal.actor, now=now, ttl=CODE_TTL
    )


async def create_link_code_for(
    conn: AsyncConnection,
    *,
    user_id: uuid.UUID,
    actor: Actor,
    now: datetime,
    ttl: timedelta = CODE_TTL,
) -> str:
    """One-time code; the Telegram account that sends it is linked to ``user_id``.
    Used by the portal (the user themself) and by the server admin CLI (audited as CLI)."""
    if not timedelta(minutes=1) <= ttl <= MAX_TTL:
        raise ConflictError("link code lifetime must be between 1 and 60 minutes")
    code = random_code(10)
    await conn.execute(
        telegram_link_codes.insert().values(
            id=uuid7(),
            code_hash=_hash(code),
            user_id=user_id,
            created_at=now,
            expires_at=now + ttl,
        )
    )
    await record(
        conn,
        action="telegram.link_code_created",
        actor=actor,
        occurred_at=now,
        entity_type="user",
        entity_id=user_id,
        payload={"ttl_minutes": int(ttl.total_seconds() // 60)},
    )
    return code


async def link(
    conn: AsyncConnection, *, code: str, telegram_user_id: int, now: datetime
) -> uuid.UUID:
    row = (
        await conn.execute(
            select(telegram_link_codes)
            .where(telegram_link_codes.c.code_hash == _hash(code))
            .with_for_update()
        )
    ).first()
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise ConflictError("invalid or expired link code; create a new one in the portal")
    taken = (
        await conn.execute(select(users.c.id).where(users.c.telegram_user_id == telegram_user_id))
    ).first()
    if taken is not None and taken.id != row.user_id:
        raise ConflictError("this Telegram account is linked to another user")
    target = (
        await conn.execute(select(users.c.telegram_user_id).where(users.c.id == row.user_id))
    ).one()
    if target.telegram_user_id is not None and target.telegram_user_id != telegram_user_id:
        raise ConflictError(
            "this user is already linked to a different Telegram account; unlink first"
        )
    await conn.execute(
        update(telegram_link_codes).where(telegram_link_codes.c.id == row.id).values(used_at=now)
    )
    await conn.execute(
        update(users)
        .where(users.c.id == row.user_id)
        .values(telegram_user_id=telegram_user_id, updated_at=now)
    )
    await record(
        conn,
        action="telegram.linked",
        actor=Actor(ActorType.TELEGRAM, str(row.user_id)),
        occurred_at=now,
        entity_type="user",
        entity_id=row.user_id,
    )
    return uuid.UUID(str(row.user_id))


async def unlink(conn: AsyncConnection, *, principal: Principal, now: datetime) -> None:
    await conn.execute(
        update(users)
        .where(users.c.id == principal.user_id)
        .values(telegram_user_id=None, updated_at=now)
    )
    await record(
        conn,
        action="telegram.unlinked",
        actor=principal.actor,
        occurred_at=now,
        entity_type="user",
        entity_id=principal.user_id,
    )


async def principal_for_telegram(conn: AsyncConnection, telegram_user_id: int) -> Principal | None:
    row = (
        await conn.execute(
            select(users).where(
                users.c.telegram_user_id == telegram_user_id, users.c.is_active.is_(True)
            )
        )
    ).first()
    if row is None:
        return None
    return Principal(
        user_id=row.id,
        username=row.username,
        display_name=row.display_name,
        roles=tuple(row.roles),
        capabilities=capabilities_for_roles(row.roles),
        session_id=None,
        csrf_token=None,
        mfa_enabled=bool(row.mfa_enabled),
        mfa_verified_at=None,
        telegram_user_id=telegram_user_id,
        channel="TELEGRAM",
    )
