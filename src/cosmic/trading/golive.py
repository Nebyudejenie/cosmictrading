"""Go-live readiness (V6 §1830 gates A–I, §9 live gates). Evaluated from recorded evidence.

Gates are recorded append-only (``golive_gates``) by the operator or CI with evidence.
Some gates are evaluated automatically and can never pass in this build (no broker).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.errors import ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable_lenient
from cosmic.db.schema_execution import post_trade_reviews
from cosmic.db.schema_trading import accounts, golive_gates
from cosmic.trading.authority import LIVE_ADAPTERS

GATES: dict[str, str] = {
    "A": "Repository and environment assessment (verified VM limits, backup before changes)",
    "B": "Unit and property tests",
    "C": "Integration and end-to-end tests against the broker sandbox",
    "D": "Adversarial safety tests",
    "E": "Recovery and chaos tests (incl. VM restart)",
    "F": "Kill switch and least-privilege API permissions",
    "G": "Paper/shadow period with realistic fill comparison",
    "H": "Operational readiness (monitoring, alerts, backups, restore, runbooks)",
    "I": "Explicit final activation by the owner",
}
PAPER_MIN_CLOSED_TRADES = 20
PAPER_MIN_DAYS = 30


@dataclass(frozen=True)
class GateStatus:
    key: str
    title: str
    status: str  # PASSED | NOT_MET | BLOCKED
    reasons: list[str]
    recorded: dict[str, Any] | None


async def record_gate(
    conn: AsyncConnection,
    *,
    gate: str,
    status: str,
    evidence: dict[str, Any],
    actor: Actor,
    now: datetime,
) -> None:
    if gate not in GATES:
        raise ValidationFailedError("unknown gate")
    if status not in ("PASSED", "FAILED", "NOT_MET"):
        raise ValidationFailedError("status must be PASSED, FAILED or NOT_MET")
    if not evidence:
        raise ValidationFailedError("evidence is required")
    await conn.execute(
        golive_gates.insert().values(
            id=uuid7(),
            gate_key=gate,
            status=status,
            evidence=to_jsonable_lenient(evidence),
            recorded_by=f"{actor.type.value}:{actor.id}",
            recorded_at=now,
        )
    )
    await record(
        conn,
        action="golive.gate_recorded",
        actor=actor,
        occurred_at=now,
        entity_type="golive_gate",
        entity_id=gate,
        payload={"status": status},
    )


async def readiness(conn: AsyncConnection, now: datetime) -> list[GateStatus]:
    latest: dict[str, Any] = {}
    for r in await conn.execute(select(golive_gates).order_by(golive_gates.c.recorded_at)):
        latest[r.gate_key] = r
    live_accounts = int(
        await conn.scalar(
            select(func.count()).select_from(accounts).where(accounts.c.kind == "LIVE")
        )
        or 0
    )
    closed = int(
        await conn.scalar(
            select(func.count())
            .select_from(post_trade_reviews)
            .where(post_trade_reviews.c.is_simulated.is_(True))
        )
        or 0
    )
    first = await conn.scalar(select(func.min(post_trade_reviews.c.created_at)))
    out: list[GateStatus] = []
    for key, title in GATES.items():
        rec = latest.get(key)
        reasons: list[str] = []
        status = "PASSED" if rec is not None and rec.status == "PASSED" else "NOT_MET"
        if rec is None:
            reasons.append("no evidence recorded")
        elif rec.status != "PASSED":
            reasons.append(f"latest record is {rec.status}")
        if key in ("A", "E"):
            reasons.append(
                "Proxmox VM not accessible (B-02): VM limits and VM restart cannot be verified"
            )
            status = "BLOCKED"
        if key in ("C", "F") and not LIVE_ADAPTERS:
            reasons.append("no broker selected or sandbox adapter registered (B-06/B-07)")
            status = "BLOCKED"
        if key == "G":
            days = 0 if first is None else (now - first).days
            if closed < PAPER_MIN_CLOSED_TRADES or days < PAPER_MIN_DAYS:
                reasons.append(
                    f"paper period: {closed}/{PAPER_MIN_CLOSED_TRADES} closed trades, "
                    f"{days}/{PAPER_MIN_DAYS} days"
                )
                status = "NOT_MET" if status != "BLOCKED" else status
            if not LIVE_ADAPTERS:
                reasons.append("no live venue to compare simulated fills against")
                status = "BLOCKED"
        if key == "I":
            if live_accounts == 0:
                reasons.append("no live account configured")
            reasons.append("requires every other gate PASSED and a separate owner activation")
            status = "BLOCKED" if not LIVE_ADAPTERS else "NOT_MET"
        out.append(
            GateStatus(
                key,
                title,
                status,
                reasons,
                None
                if rec is None
                else {
                    "status": rec.status,
                    "recorded_at": rec.recorded_at,
                    "by": rec.recorded_by,
                    "evidence": rec.evidence,
                },
            )
        )
    return out


async def latest_evidence(conn: AsyncConnection, gate: str) -> Any:
    return (
        await conn.execute(
            select(golive_gates)
            .where(golive_gates.c.gate_key == gate)
            .order_by(desc(golive_gates.c.recorded_at))
            .limit(1)
        )
    ).first()
