"""Explicit, authenticated, single-use approval (V6 §1808, ADR-011).

Flow: ``issue_challenge`` (operator reviews the exact packet) → ``decide`` (operator
confirms with the challenge token + packet hash, and fresh MFA). Silence, timeouts,
free text or a stale button never create an approval: only ``decide`` does, and only
when every binding matches server-side state.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.config import Settings
from cosmic.core.errors import AuthenticationError, ConflictError, PermissionDeniedError
from cosmic.core.ids import uuid7
from cosmic.db.schema_execution import approval_challenges, approvals, trade_intents
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.notify.outbox import notify_owners
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import get_intent, latest_packet, set_intent_status

CHALLENGE_TTL = timedelta(minutes=5)
Decision = Literal["APPROVE", "REJECT", "REQUEST_CHANGES"]
DECISION_STATUS = {
    "APPROVE": "APPROVED",
    "REJECT": "REJECTED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
}


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


@dataclass(frozen=True)
class Challenge:
    token: str
    challenge_id: uuid.UUID
    intent_id: uuid.UUID
    trade_code: str
    packet_hash: str
    packet_version: int
    terms: dict[str, Any]
    expires_at: datetime


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: uuid.UUID
    intent_id: uuid.UUID
    decision: str
    status: str
    expires_at: datetime


def confirmation_terms(packet: dict[str, Any]) -> dict[str, Any]:
    """The exact terms shown on the confirmation screen (V6 §1823)."""
    order = packet["order"]
    return {
        "trade_code": packet["trade_code"],
        "account": packet["account"]["alias"],
        "account_kind": packet["account"]["kind"],
        "instrument": packet["instrument"]["canonical_key"],
        "side": order["side"],
        "quantity": order["quantity"],
        "order_type": order["order_type"],
        "limit_price": order["limit_price"],
        "price_band": order["price_band"],
        "max_slippage_bps": order["max_slippage_bps"],
        "time_in_force": order["time_in_force"],
        "management_plan": packet["management_plan"],
        "policy_version": packet["policy_version"],
        "closes": [c["trade_code"] for c in order.get("closes", [])],
    }


async def issue_challenge(
    conn: AsyncConnection,
    *,
    principal: Principal,
    intent_id: uuid.UUID,
    channel: str,
    now: datetime,
) -> Challenge:
    if not principal.has(Capability.APPROVE_TRADE):
        raise PermissionDeniedError("missing capability APPROVE_TRADE")
    intent = await get_intent(conn, intent_id)
    if intent.status != "PROPOSED":
        raise ConflictError(f"this trade is {intent.status}; nothing to approve")
    if intent.intent_expires_at <= now:
        raise ConflictError("this proposal has expired")
    packet = await latest_packet(conn, intent_id)
    token = secrets.token_urlsafe(32)
    challenge_id = uuid7()
    expires = min(now + CHALLENGE_TTL, intent.intent_expires_at)
    await conn.execute(
        approval_challenges.insert().values(
            id=challenge_id,
            token_hash=_hash(token),
            intent_id=intent_id,
            packet_id=packet.id,
            packet_hash=packet.packet_hash,
            user_id=principal.user_id,
            channel=channel,
            created_at=now,
            expires_at=expires,
        )
    )
    await record(
        conn,
        action="approval.challenge_issued",
        actor=principal.actor,
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=intent_id,
        payload={"channel": channel, "packet_version": packet.version},
    )
    return Challenge(
        token,
        challenge_id,
        intent_id,
        intent.trade_code,
        packet.packet_hash,
        int(packet.version),
        confirmation_terms(packet.packet),
        expires,
    )


async def decide(
    conn: AsyncConnection,
    *,
    principal: Principal,
    token: str,
    packet_hash: str,
    decision: Decision,
    now: datetime,
    settings: Settings,
    channel: str = "PORTAL",
    second_factor_ok: bool = False,
    note: str | None = None,
    challenge_id: uuid.UUID | None = None,
) -> ApprovalRecord:
    """Record the operator's decision. Every binding is verified server-side."""
    if decision not in DECISION_STATUS:
        raise ConflictError("unknown decision")
    if channel == "PORTAL":
        require(principal, Capability.APPROVE_TRADE, now=now, settings=settings)  # fresh MFA
    else:
        if not principal.has(Capability.APPROVE_TRADE):
            raise PermissionDeniedError("missing capability APPROVE_TRADE")
        if (
            decision == "APPROVE"
            and settings.telegram_approval_requires_totp
            and not second_factor_ok
        ):
            raise AuthenticationError("an authenticator code is required to approve from Telegram")
    if channel == "TELEGRAM":
        # Telegram callback data carries only an opaque challenge reference (never a
        # credential); authority comes from the bound Telegram user + TOTP checks.
        if challenge_id is None:
            raise ConflictError("invalid confirmation")
        lookup = approval_challenges.c.id == challenge_id
    else:
        if not token or len(token) > 100:
            raise ConflictError("invalid confirmation")
        lookup = approval_challenges.c.token_hash == _hash(token)
    ch = (await conn.execute(select(approval_challenges).where(lookup).with_for_update())).first()
    if ch is None:
        raise ConflictError("unknown confirmation; review the trade again")
    if ch.user_id != principal.user_id or ch.channel != channel:
        raise ConflictError("this confirmation belongs to another operator or channel")
    if ch.used_at is not None:
        raise ConflictError("this confirmation was already used")
    if ch.expires_at <= now:
        raise ConflictError("this confirmation expired; review the trade again")
    if ch.packet_hash != packet_hash:
        raise ConflictError("the confirmed terms do not match the reviewed packet")
    used = await conn.execute(
        update(approval_challenges)
        .where(approval_challenges.c.id == ch.id, approval_challenges.c.used_at.is_(None))
        .values(used_at=now)
    )
    if used.rowcount != 1:
        raise ConflictError("this confirmation was already used")
    intent = await get_intent(conn, ch.intent_id, lock=True)
    if intent.status != "PROPOSED":
        raise ConflictError(f"this trade is {intent.status}; the decision was not recorded")
    if intent.intent_expires_at <= now:
        await set_intent_status(conn, intent.id, "EXPIRED", "intent expired before decision", now)
        raise ConflictError("this proposal has expired")
    packet = await latest_packet(conn, intent.id)
    if packet.id != ch.packet_id or packet.packet_hash != packet_hash:
        raise ConflictError("a newer version of this proposal exists; review it again")
    policy_version, policy = await active_policy(conn)
    if policy_version != packet.policy_version:
        await set_intent_status(
            conn,
            intent.id,
            "REQUIRES_REAPPROVAL",
            "risk policy changed after the packet was created",
            now,
        )
        raise ConflictError(
            "risk limits changed since this proposal was created; it must be re-checked"
        )
    expires = min(now + timedelta(seconds=policy.approval_ttl_seconds), intent.intent_expires_at)
    approval_id = uuid7()
    status = "ACTIVE" if decision == "APPROVE" else "FINAL"
    await conn.execute(
        approvals.insert().values(
            id=approval_id,
            intent_id=intent.id,
            packet_id=packet.id,
            packet_hash=packet.packet_hash,
            decision=DECISION_STATUS[decision],
            operator_user_id=principal.user_id,
            channel=channel,
            nonce=secrets.token_urlsafe(24),
            policy_version=packet.policy_version,
            decided_at=now,
            expires_at=expires,
            status=status,
            note=(note or None) and note[:1000],
        )
    )
    await set_intent_status(conn, intent.id, DECISION_STATUS[decision], note, now)
    await record(
        conn,
        action=f"approval.{DECISION_STATUS[decision].lower()}",
        actor=principal.actor,
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=intent.id,
        payload={
            "approval_id": approval_id,
            "packet_hash": packet.packet_hash,
            "packet_version": packet.version,
            "channel": channel,
            "expires_at": expires,
            "policy_version": packet.policy_version,
        },
    )
    return ApprovalRecord(approval_id, intent.id, DECISION_STATUS[decision], status, expires)


async def revoke(
    conn: AsyncConnection,
    *,
    principal: Principal,
    approval_id: uuid.UUID,
    reason: str,
    now: datetime,
) -> None:
    if not principal.has(Capability.APPROVE_TRADE):
        raise PermissionDeniedError("missing capability APPROVE_TRADE")
    row = (
        await conn.execute(select(approvals).where(approvals.c.id == approval_id).with_for_update())
    ).first()
    if row is None or row.status != "ACTIVE":
        raise ConflictError("only an unconsumed active approval can be revoked")
    await conn.execute(
        update(approvals)
        .where(approvals.c.id == approval_id)
        .values(status="REVOKED", status_reason=reason[:500])
    )
    await set_intent_status(conn, row.intent_id, "REVOKED", reason, now, expected=("APPROVED",))
    await record(
        conn,
        action="approval.revoked",
        actor=principal.actor,
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=row.intent_id,
        payload={"reason": reason},
    )


async def expire_sweep(conn: AsyncConnection, *, now: datetime) -> dict[str, int]:
    """Expire stale approvals and intents. Expired approvals never execute."""
    expired_approvals = list(
        await conn.execute(
            update(approvals)
            .where(approvals.c.status == "ACTIVE", approvals.c.expires_at <= now)
            .values(status="EXPIRED", status_reason="approval expired before execution")
            .returning(approvals.c.intent_id)
        )
    )
    for row in expired_approvals:
        await set_intent_status(
            conn,
            row.intent_id,
            "REQUIRES_REAPPROVAL",
            "approval expired",
            now,
            expected=("APPROVED",),
        )
    expired_intents = list(
        await conn.execute(
            update(trade_intents)
            .where(
                trade_intents.c.status.in_(["PROPOSED", "REQUIRES_REAPPROVAL", "APPROVED"]),
                trade_intents.c.intent_expires_at <= now,
            )
            .values(status="EXPIRED", status_reason="intent expired", updated_at=now)
            .returning(trade_intents.c.id, trade_intents.c.trade_code)
        )
    )
    for row in expired_intents:
        await conn.execute(
            update(approvals)
            .where(approvals.c.intent_id == row.id, approvals.c.status == "ACTIVE")
            .values(status="EXPIRED", status_reason="intent expired")
        )
        await notify_owners(
            conn,
            kind="proposal_expired",
            now=now,
            dedup_key=f"expired:{row.id}",
            payload={"trade_code": row.trade_code},
        )
    if expired_approvals or expired_intents:
        await record(
            conn,
            action="approval.expiry_sweep",
            actor=Actor.system("expiry"),
            occurred_at=now,
            payload={"approvals": len(expired_approvals), "intents": len(expired_intents)},
        )
    return {"approvals": len(expired_approvals), "intents": len(expired_intents)}
