"""Claims with evidence links. Numbers are verified by code; AI statements are INFERRED."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.db.schema_market import claims, contradictions, evidence_links


async def add_claim(
    conn: AsyncConnection,
    *,
    mission_id: uuid.UUID | None,
    statement: str,
    status: str,
    created_by: str,
    now: datetime,
    metric: str | None = None,
    value: Decimal | None = None,
    unit: str | None = None,
    as_of: datetime | None = None,
    subject: tuple[str, str] | None = None,
    method: str | None = None,
    evidence: list[tuple[str, str, datetime | None]] | None = None,
) -> uuid.UUID:
    cid = uuid7()
    await conn.execute(
        claims.insert().values(
            id=cid,
            mission_id=mission_id,
            subject_type=subject[0] if subject else None,
            subject_id=subject[1] if subject else None,
            statement=statement[:1000],
            claim_type="NUMERIC" if value is not None else "QUALITATIVE",
            metric=metric,
            value=value,
            unit=unit,
            as_of=as_of,
            status=status,
            verification_method=method,
            verified_at=now if status == "VERIFIED" else None,
            created_by=created_by,
            created_at=now,
        )
    )
    for etype, ref, ts in evidence or []:
        await conn.execute(
            evidence_links.insert().values(
                id=uuid7(), claim_id=cid, evidence_type=etype, evidence_ref=ref[:200], source_ts=ts
            )
        )
    return cid


def verify_numeric(stated: Decimal, computed: Decimal, rel_tol: Decimal = Decimal("0.005")) -> bool:
    if computed == 0:
        return stated == 0
    return abs(stated - computed) / abs(computed) <= rel_tol


async def add_contradiction(
    conn: AsyncConnection,
    *,
    claim_id: uuid.UUID,
    description: str,
    now: datetime,
    other_claim_id: uuid.UUID | None = None,
) -> None:
    await conn.execute(
        contradictions.insert().values(
            id=uuid7(),
            claim_id=claim_id,
            other_claim_id=other_claim_id,
            description=description[:500],
            detected_at=now,
            status="OPEN",
        )
    )


async def mission_claims(conn: AsyncConnection, mission_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(claims).where(claims.c.mission_id == mission_id).order_by(claims.c.created_at)
    )
    return [dict(r._mapping) for r in rows]
