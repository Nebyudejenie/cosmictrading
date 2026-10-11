"""Provider state, usage ledger, quota and cache (DB-backed so the portal can show it)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.ai.policy import active_attestation
from cosmic.ai.registry import ProviderConfig
from cosmic.core.ids import uuid7
from cosmic.core.redaction import scrub_text
from cosmic.db.schema_ai import ai_cache, ai_provider_state, ai_usage

BREAKER_THRESHOLD = 3
BREAKER_OPEN = timedelta(minutes=5)
DEFAULT_RATE_LIMIT = timedelta(seconds=60)


async def _upsert(conn: AsyncConnection, provider_key: str, now: datetime, **values: Any) -> None:
    insert_values = {"circuit": "CLOSED", **values}
    stmt = pg_insert(ai_provider_state).values(
        provider_key=provider_key, updated_at=now, **insert_values
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[ai_provider_state.c.provider_key], set_={"updated_at": now, **values}
        )
    )


async def record_success(conn: AsyncConnection, provider_key: str, now: datetime) -> None:
    await _upsert(
        conn,
        provider_key,
        now,
        circuit="CLOSED",
        consecutive_failures=0,
        opened_until=None,
        last_success_at=now,
    )


async def record_failure(
    conn: AsyncConnection, provider_key: str, now: datetime, error: str
) -> str:
    row = (
        await conn.execute(
            select(ai_provider_state)
            .where(ai_provider_state.c.provider_key == provider_key)
            .with_for_update()
        )
    ).first()
    failures = (row.consecutive_failures if row else 0) + 1
    circuit = "OPEN" if failures >= BREAKER_THRESHOLD else "CLOSED"
    await _upsert(
        conn,
        provider_key,
        now,
        consecutive_failures=failures,
        circuit=circuit,
        opened_until=now + BREAKER_OPEN if circuit == "OPEN" else None,
        last_error_at=now,
        last_error=scrub_text(error)[:500],
    )
    return circuit


async def record_rate_limited(
    conn: AsyncConnection, provider_key: str, now: datetime, retry_after: float | None
) -> None:
    wait = timedelta(seconds=retry_after) if retry_after and retry_after > 0 else DEFAULT_RATE_LIMIT
    await _upsert(
        conn,
        provider_key,
        now,
        rate_limited_until=now + min(wait, timedelta(hours=24)),
        last_error_at=now,
        last_error="rate limited",
    )


async def disable(conn: AsyncConnection, provider_key: str, now: datetime, reason: str) -> None:
    await _upsert(
        conn,
        provider_key,
        now,
        disabled_reason=reason[:300],
        last_error_at=now,
        last_error=reason[:300],
    )


async def clear_disable(conn: AsyncConnection, provider_key: str, now: datetime) -> None:
    await _upsert(conn, provider_key, now, disabled_reason=None)


async def log_usage(
    conn: AsyncConnection,
    *,
    provider_key: str,
    model_id: str,
    task_type: str,
    role_key: str | None,
    request_hash: str,
    status: str,
    now: datetime,
    http_status: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
    reported_cost: Decimal | None = None,
    error: str | None = None,
) -> uuid.UUID:
    usage_id = uuid7()
    await conn.execute(
        ai_usage.insert().values(
            id=usage_id,
            provider_key=provider_key,
            model_id=model_id,
            task_type=task_type,
            role_key=role_key,
            request_hash=request_hash,
            status=status,
            http_status=http_status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            reported_cost=reported_cost,
            error=None if error is None else scrub_text(error)[:500],
            created_at=now,
        )
    )
    return usage_id


async def quota_available(
    conn: AsyncConnection, p: ProviderConfig, now: datetime
) -> tuple[bool, str]:
    att = await active_attestation(conn, p.key, now) if p.requires_attestation else None
    rpm = att.rpm_limit if att is not None and att.rpm_limit is not None else p.default_rpm
    rpd = att.rpd_limit if att is not None and att.rpd_limit is not None else p.default_rpd
    minute = await conn.scalar(
        select(func.count())
        .select_from(ai_usage)
        .where(ai_usage.c.provider_key == p.key, ai_usage.c.created_at > now - timedelta(minutes=1))
    )
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day = await conn.scalar(
        select(func.count())
        .select_from(ai_usage)
        .where(ai_usage.c.provider_key == p.key, ai_usage.c.created_at >= day_start)
    )
    if rpm <= 0 or rpd <= 0:
        return False, "no quota configured"
    if int(minute or 0) >= rpm:
        return False, f"local per-minute budget used ({rpm})"
    if int(day or 0) >= rpd:
        return False, f"local daily budget used ({rpd})"
    return True, ""


async def cache_get(conn: AsyncConnection, key: str, now: datetime) -> Any:
    return (
        await conn.execute(
            select(ai_cache).where(ai_cache.c.cache_key == key, ai_cache.c.expires_at > now)
        )
    ).first()


async def cache_put(
    conn: AsyncConnection,
    *,
    key: str,
    task_type: str,
    response: dict[str, Any],
    provider_key: str,
    model_id: str,
    now: datetime,
    ttl_seconds: int,
) -> None:
    values = {
        "task_type": task_type,
        "response": response,
        "provider_key": provider_key,
        "model_id": model_id,
        "created_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds),
    }
    await conn.execute(
        pg_insert(ai_cache)
        .values(cache_key=key, **values)
        .on_conflict_do_update(index_elements=[ai_cache.c.cache_key], set_=values)
    )
