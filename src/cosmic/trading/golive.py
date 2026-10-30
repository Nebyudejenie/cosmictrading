"""Go-live readiness (V6 §1830 gates A–I, §9 live gates). Evaluated from recorded evidence.

Gates A–H are recorded append-only (``golive_gates``) by the operator or CI with evidence; the
latest record counts. Gate I is the owner's own live activation (ADR-034, ``trading/live``):
it is never recorded here, and passes only while an activation is active and A–H pass. Some
gates also depend on facts no record can change: without a live adapter, C, F and I stay
BLOCKED; G needs its paper period and the paper fills compared with Binance's real order book
(S41). ``authority.live_preconditions`` requires every gate PASSED as evaluated here, so a
record alone never opens live trading.
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
from cosmic.trading import real_book
from cosmic.trading.authority import LIVE_ADAPTERS
from cosmic.trading.live import active_activations
from cosmic.trading.sandbox import SANDBOXES, latest_contract_run

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
OWNER_GATE = "I"  # the owner's activation: never recorded by a command
# What evidence passes each gate (the runbook, docs/15 §13, has the commands).
HOW: dict[str, str] = {
    "A": (
        "Record the VM check (deploy/vm/verify.sh), the VM's CPU, RAM and disk with their "
        "measured use, and the backup taken before changes."
    ),
    "B": "Record the output of `make check` for the deployed version.",
    "C": "The contract run of each venue's adapter; live needs the live adapter's own run.",
    "D": (
        "Record the adversarial tests (tests/trading/test_acceptance.py AT-04 to AT-08 and "
        "AT-14; docs/reports/2026-10-16-acceptance-scenarios.md) and tests/security."
    ),
    "E": (
        "Record the failure and chaos tests (AT-09, AT-11, AT-20, "
        "tests/integration/test_database_outage.py; the chaos and restart phases of "
        "scripts/local-verify.sh) and a VM restart test."
    ),
    "F": "Record a kill-switch drill and the live key's permissions as read from the venue.",
    "G": (
        f"At least {PAPER_MIN_CLOSED_TRADES} closed simulated trades over {PAPER_MIN_DAYS} "
        "days, and the paper fills compared with Binance's real order book: at least "
        f"{real_book.MIN_CHECKS}, and on median neither kinder than it by more than "
        f"{real_book.KINDER_LIMIT_BPS} bps nor harsher by more than "
        f"{real_book.CAUTIOUS_LIMIT_BPS} bps; none on a book too thin, at most one in five "
        "missed."
    ),
    "H": "Record monitoring, alerts, backups with a restore drill, and the runbook.",
    "I": "Your activation with a fresh code, after every other gate has passed.",
}


def gate_g_fills(book: real_book.BookSummary) -> tuple[bool, list[str]]:
    """Gate G's fill comparison (S41): whether it holds, and why not, in plain words."""
    if not book.checks and not book.thin:
        return False, [
            "fills compared with Binance's real order book: none yet (each paper fill on "
            "Binance prices is compared as it happens)"
        ]
    lines = [
        f"fills compared with Binance's real order book: {book.checks}/{real_book.MIN_CHECKS}"
        + (
            f", median {book.median_bps:+} bps (positive: the paper fill was the harsher)"
            if book.median_bps is not None
            else ""
        )
    ]
    lines += [f"fill comparison: {why}" for why in book.shortfalls if "fills compared" not in why]
    return book.realistic, lines


@dataclass(frozen=True)
class GateStatus:
    key: str
    title: str
    status: str  # PASSED | NOT_MET | BLOCKED
    reasons: list[str]
    recorded: dict[str, Any] | None
    how: str = ""  # what evidence passes it


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
    if gate == OWNER_GATE:
        raise ValidationFailedError(
            "gate I is the owner's own activation with a fresh code; it cannot be recorded"
        )
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


async def _contract_runs(conn: AsyncConnection) -> list[str]:
    """Each sandbox adapter's latest contract run, and its last PASSED one if that is older."""
    lines: list[str] = []
    for adapter, venue in SANDBOXES.items():
        latest = await latest_contract_run(conn, adapter=adapter)
        if latest is None:
            lines.append(f"no contract run on the {venue.label} yet")
            continue
        lines.append(
            f"contract run {latest.status} on the {venue.label} "
            f"({latest.finished_at:%Y-%m-%d %H:%M:%S} UTC, run {str(latest.id)[-8:]})"
        )
        if latest.status != "PASSED":
            passed = await latest_contract_run(conn, passed_only=True, adapter=adapter)
            if passed is not None:
                lines.append(
                    f"last PASSED on the {venue.label}: {passed.finished_at:%Y-%m-%d %H:%M:%S} "
                    f"UTC (run {str(passed.id)[-8:]})"
                )
    return lines


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
        rec = None if key == OWNER_GATE else latest.get(key)
        reasons: list[str] = []
        status = "PASSED" if rec is not None and rec.status == "PASSED" else "NOT_MET"
        if key != OWNER_GATE:
            if rec is None:
                reasons.append("no evidence recorded")
            elif rec.status != "PASSED":
                reasons.append(f"latest record is {rec.status}")
        if key == "C":
            reasons.extend(await _contract_runs(conn))
        if key in ("C", "F") and not LIVE_ADAPTERS:
            reasons.append(
                "no live adapter exists yet: the sandbox runs cover the test networks only (B-07)"
            )
            status = "BLOCKED"
        if key == "G":
            days = 0 if first is None else (now - first).days
            if closed < PAPER_MIN_CLOSED_TRADES or days < PAPER_MIN_DAYS:
                reasons.append(
                    f"paper period: {closed}/{PAPER_MIN_CLOSED_TRADES} closed trades, "
                    f"{days}/{PAPER_MIN_DAYS} days"
                )
                status = "NOT_MET" if status != "BLOCKED" else status
            # The fill comparison (S41): paper fills against Binance's real order book.
            book = await real_book.summary(conn, now=now)
            met, why = gate_g_fills(book)
            reasons.extend(why)
            if not met:
                status = "NOT_MET" if status != "BLOCKED" else status
        if key == OWNER_GATE:
            if live_accounts == 0:
                reasons.append("no live account configured")
            active = await active_activations(conn, now)
            others_passed = all(g.status == "PASSED" for g in out)
            for a in active:
                reasons.append(
                    f"activated: {a.account_ref} up to {a.cap:,.2f} {a.currency} until "
                    f"{a.expires_at:%Y-%m-%d %H:%M} UTC"
                )
            if not active:
                reasons.append(
                    "requires every other gate PASSED, then your own activation with a fresh code"
                )
            elif not others_passed:
                reasons.append("an activation counts only while every other gate has passed")
            status = "PASSED" if active and others_passed else "NOT_MET"
            if not LIVE_ADAPTERS:
                reasons.append("no live adapter exists in this build (ADR-034 lock 1)")
                status = "BLOCKED"
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
                HOW[key],
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
