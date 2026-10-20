"""Demo mandates (ADR-030): a bounded standing approval for the demo auto-trader.

The owner grants it once, with a fresh authenticator code, for ONE simulated account (PAPER or
SANDBOX; the database refuses any other). While it is active and unexpired, the auto-trader's
own proposals on that account are approved here instead of by a person, but only when:

- the proposal was made by the auto-trader under this mandate, on its account and instruments;
- the authority mode allows new exposure (an entry), and every risk check already passed;
- the estimated loss at the stop is at most the mandate's maximum loss per trade;
- today's mandate entries, the account's open positions and today's realised loss are within
  the mandate's limits.

An exit of what the auto-trader bought is approved whenever the mandate is active: it only
reduces exposure. Revoking or expiry stops everything at once; open positions keep their
broker stop and are then the owner's to manage. Every decision is audited and reported.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.schema_execution import (
    approvals,
    post_trade_reviews,
    trade_intents,
    trading_mandates,
)
from cosmic.db.schema_trading import positions
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, require
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.trading.accounts import get_account, list_accounts
from cosmic.trading.authority import get_state
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, Operation, account_fields, mode_permits
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import get_intent, latest_packet, set_intent_status

ACTOR = Actor.system("mandate")
TIMEFRAME = "1h"
# Rules the auto-trader may run, by key, with their names (the owner's choice, 2026-10-20).
AUTO_RULES: dict[str, str] = {"turtle_20_10": "Turtle system 1 (20/10 breakout)"}
# Where each tradable instrument's hourly signal comes from: the venue's own bars where they
# exist, otherwise the closest stored series (HFM CFDs have no stored bars).
AUTO_SIGNAL_SERIES: dict[str, str] = {
    "CFD:BTC/USD@HFM": "SPOT:BTC/USDT@BINANCE",
    "CFD:XAU/USD@HFM": "SPOT:PAXG/USDT@BINANCE",
    "SPOT:BTC/USDT@BINANCE": "SPOT:BTC/USDT@BINANCE",
    "SPOT:PAXG/USDT@BINANCE": "SPOT:PAXG/USDT@BINANCE",
}


class MandateTerms(BaseModel):
    """What the owner grants. Conservative bounds; the database checks them again."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_alias: str = Field(min_length=2, max_length=42)
    strategy_key: str
    instruments: tuple[str, ...] = Field(min_length=1, max_length=5)
    max_loss_per_trade: Decimal = Field(gt=0, le=1000)
    max_trades_per_day: int = Field(ge=1, le=20)
    max_open_positions: int = Field(ge=1, le=5)
    daily_loss_limit: Decimal = Field(gt=0, le=5000)
    days: int = Field(ge=1, le=90)


@dataclass(frozen=True)
class Mandate:
    id: uuid.UUID
    account_id: uuid.UUID
    strategy_key: str
    timeframe: str
    instruments: tuple[str, ...]
    max_loss_per_trade: Decimal
    max_trades_per_day: int
    max_open_positions: int
    daily_loss_limit: Decimal
    status: str
    granted_by: uuid.UUID
    granted_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    end_reason: str | None

    def active(self, now: datetime) -> bool:
        return self.status == "ACTIVE" and self.expires_at > now


def _mandate(row: Any) -> Mandate:
    return Mandate(
        id=row.id,
        account_id=row.account_id,
        strategy_key=row.strategy_key,
        timeframe=row.timeframe,
        instruments=tuple(row.instruments),
        max_loss_per_trade=row.max_loss_per_trade,
        max_trades_per_day=row.max_trades_per_day,
        max_open_positions=row.max_open_positions,
        daily_loss_limit=row.daily_loss_limit,
        status=row.status,
        granted_by=row.granted_by,
        granted_at=row.granted_at,
        expires_at=row.expires_at,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
    )


async def get_mandate(conn: AsyncConnection, mandate_id: uuid.UUID) -> Mandate:
    row = (
        await conn.execute(select(trading_mandates).where(trading_mandates.c.id == mandate_id))
    ).first()
    if row is None:
        raise NotFoundError("unknown mandate")
    return _mandate(row)


async def active_mandates(conn: AsyncConnection, now: datetime) -> list[Mandate]:
    rows = await conn.execute(
        select(trading_mandates).where(
            trading_mandates.c.status == "ACTIVE", trading_mandates.c.expires_at > now
        )
    )
    return [_mandate(r) for r in rows]


async def recent_mandates(conn: AsyncConnection, limit: int = 5) -> list[Mandate]:
    rows = await conn.execute(
        select(trading_mandates).order_by(trading_mandates.c.granted_at.desc()).limit(limit)
    )
    return [_mandate(r) for r in rows]


async def grant(
    conn: AsyncConnection,
    *,
    principal: Principal,
    terms: MandateTerms,
    now: datetime,
    settings: Settings,
) -> uuid.UUID:
    """The owner grants a mandate (APPROVE_TRADE with a fresh authenticator code)."""
    require(principal, Capability.APPROVE_TRADE, now=now, settings=settings)
    account = await get_account(conn, alias=terms.account_alias)
    if account.kind not in SIMULATED_ACCOUNT_KINDS:
        raise PermissionDeniedError("the auto-trader may only use a paper or demo account")
    if account.status != "ACTIVE":
        raise ConflictError(f"account {account.alias} is {account.status}")
    if terms.strategy_key not in AUTO_RULES:
        raise ConflictError(f"rule {terms.strategy_key} is not available to the auto-trader")
    for key in terms.instruments:
        if key not in AUTO_SIGNAL_SERIES:
            raise ConflictError(f"{key} has no hourly signal series for the auto-trader")
        inst = await md.instrument_by_key(conn, key)
        if inst.venue_key != account.venue_key or not inst.tradable:
            raise ConflictError(f"{key} is not tradable on {account.alias}")
    existing = await conn.scalar(
        select(trading_mandates.c.id).where(
            trading_mandates.c.account_id == account.id, trading_mandates.c.status == "ACTIVE"
        )
    )
    if existing is not None:
        raise ConflictError("this account already has an active mandate; revoke it first")
    mandate_id = uuid7()
    expires = now + timedelta(days=terms.days)
    await conn.execute(
        trading_mandates.insert().values(
            id=mandate_id,
            account_id=account.id,
            strategy_key=terms.strategy_key,
            timeframe=TIMEFRAME,
            instruments=list(terms.instruments),
            max_loss_per_trade=terms.max_loss_per_trade,
            max_trades_per_day=terms.max_trades_per_day,
            max_open_positions=terms.max_open_positions,
            daily_loss_limit=terms.daily_loss_limit,
            status="ACTIVE",
            granted_by=principal.user_id,
            granted_at=now,
            expires_at=expires,
        )
    )
    fields = account_fields(account.alias, account.kind, account.broker_adapter)
    await record(
        conn,
        action="mandate.granted",
        actor=principal.actor,
        occurred_at=now,
        entity_type="mandate",
        entity_id=mandate_id,
        payload={**terms.model_dump(mode="json"), **fields, "expires_at": expires},
    )
    await notify_owners(
        conn,
        kind="mandate_changed",
        now=now,
        dedup_key=f"mandate:{mandate_id}:granted",
        payload={
            **fields,
            "text": f"Auto-trader ON for {fields['account_ref']} until {expires:%Y-%m-%d %H:%M} "
            f"UTC: rule {AUTO_RULES[terms.strategy_key]} on 1-hour bars, "
            f"{', '.join(terms.instruments)}; at most {terms.max_loss_per_trade} loss per "
            f"trade, {terms.max_trades_per_day} trades a day, {terms.max_open_positions} open "
            f"positions; no new trades after a {terms.daily_loss_limit} loss in a day.",
        },
    )
    return mandate_id


async def end(
    conn: AsyncConnection,
    *,
    mandate_id: uuid.UUID,
    status: str,
    by: Actor,
    reason: str,
    now: datetime,
) -> bool:
    """End an active mandate (REVOKED or EXPIRED). False if it had already ended."""
    ended = await conn.execute(
        update(trading_mandates)
        .where(trading_mandates.c.id == mandate_id, trading_mandates.c.status == "ACTIVE")
        .values(status=status, ended_at=now, ended_by=f"{by.type.value}:{by.id}", end_reason=reason)
    )
    if ended.rowcount != 1:
        return False
    m = await get_mandate(conn, mandate_id)
    a = await get_account(conn, account_id=m.account_id)
    fields = account_fields(a.alias, a.kind, a.broker_adapter)
    await record(
        conn,
        action=f"mandate.{status.lower()}",
        actor=by,
        occurred_at=now,
        entity_type="mandate",
        entity_id=mandate_id,
        payload={"reason": reason, **fields},
    )
    await notify_owners(
        conn,
        kind="mandate_changed",
        now=now,
        dedup_key=f"mandate:{mandate_id}:{status}",
        payload={
            **fields,
            "text": f"Auto-trader OFF for {fields['account_ref']} ({status.lower()}: {reason}). "
            "No new auto trades; open positions keep their stop and are yours to manage.",
        },
    )
    return True


async def revoke(
    conn: AsyncConnection,
    *,
    principal: Principal,
    mandate_id: uuid.UUID,
    reason: str,
    now: datetime,
) -> bool:
    """Revoking only removes authority, so no fresh code is needed."""
    if not principal.has(Capability.APPROVE_TRADE):
        raise PermissionDeniedError("missing capability APPROVE_TRADE")
    return await end(
        conn,
        mandate_id=mandate_id,
        status="REVOKED",
        by=principal.actor,
        reason=reason[:500] or "revoked by the owner",
        now=now,
    )


async def expire_due(conn: AsyncConnection, now: datetime) -> int:
    ids = [
        r.id
        for r in await conn.execute(
            select(trading_mandates.c.id).where(
                trading_mandates.c.status == "ACTIVE", trading_mandates.c.expires_at <= now
            )
        )
    ]
    for mid in ids:
        await end(conn, mandate_id=mid, status="EXPIRED", by=ACTOR, reason="expired", now=now)
    return len(ids)


@dataclass(frozen=True)
class Usage:
    trades_today: int
    open_positions: int
    realized_today: Decimal  # negative for a loss


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def usage(conn: AsyncConnection, m: Mandate, now: datetime) -> Usage:
    """Today (UTC) under this mandate: its entries, the account's open positions (all of
    them, so a manual trade counts too) and the account's realised result."""
    start = day_start(now)
    trades = await conn.scalar(
        select(func.count())
        .select_from(approvals)
        .join(trade_intents, trade_intents.c.id == approvals.c.intent_id)
        .where(
            approvals.c.mandate_id == m.id,
            approvals.c.decided_at >= start,
            trade_intents.c.side == "BUY",
        )
    )
    open_positions = await conn.scalar(
        select(func.count())
        .select_from(positions)
        .where(positions.c.account_id == m.account_id, positions.c.quantity > 0)
    )
    realized = await conn.scalar(
        select(func.coalesce(func.sum(post_trade_reviews.c.realized_pnl), 0))
        .select_from(post_trade_reviews)
        .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
        .where(trade_intents.c.account_id == m.account_id, post_trade_reviews.c.created_at >= start)
    )
    return Usage(int(trades or 0), int(open_positions or 0), Decimal(str(realized or ZERO)))


def entry_refusal(m: Mandate, u: Usage, est_max_loss: Decimal | None) -> str | None:
    """Why a new entry is outside the mandate, or None. Pure, so every limit is testable."""
    if est_max_loss is None:
        return "the trade has no protective stop, so its loss is not bounded"
    if est_max_loss > m.max_loss_per_trade:
        return f"its loss at the stop ({est_max_loss:.2f}) exceeds {m.max_loss_per_trade:.2f}"
    if u.trades_today >= m.max_trades_per_day:
        return f"today's {m.max_trades_per_day} auto trades are used"
    if u.open_positions >= m.max_open_positions:
        return f"{u.open_positions} position(s) are open (at most {m.max_open_positions})"
    if u.realized_today <= -m.daily_loss_limit:
        return f"today's loss ({u.realized_today:.2f}) reached the {m.daily_loss_limit:.2f} limit"
    return None


async def approve(
    conn: AsyncConnection, *, intent_id: uuid.UUID, mandate: Mandate, now: datetime
) -> tuple[bool, str]:
    """Approve the auto-trader's proposal under ``mandate``, or refuse with the reason.

    A refused entry is set BLOCKED with that reason; a refused exit stays PROPOSED for the
    owner. Either way the owner is told.
    """
    intent = await get_intent(conn, intent_id, lock=True)
    entry = intent.side == "BUY"
    m = await get_mandate(conn, mandate.id)
    why: str | None = None
    if intent.status != "PROPOSED":
        return False, f"the proposal is {intent.status}"
    if (intent.details or {}).get("mandate_id") != str(m.id) or intent.created_by_kind == "USER":
        why = "it was not proposed by the auto-trader under this mandate"
    elif not m.active(now):
        why = "the mandate is no longer active"
    elif intent.account_id != m.account_id:
        why = "it is for another account"
    packet = await latest_packet(conn, intent.id)
    inst_key = packet.packet["instrument"]["canonical_key"]
    if why is None and inst_key not in m.instruments:
        why = f"{inst_key} is not in the mandate"
    policy_version, policy = await active_policy(conn)
    if why is None and policy_version != packet.policy_version:
        why = "the risk policy changed after the proposal was made"
    if why is None and entry:
        state = await get_state(conn)
        account = await get_account(conn, account_id=intent.account_id)
        ok, reason = mode_permits(state.mode, Operation.OPEN_EXPOSURE, account_kind=account.kind)
        if not ok:
            why = f"mode {state.mode.value}: {reason}"
        else:
            raw = (packet.packet.get("risk") or {}).get("estimated_max_loss")
            est = Decimal(str(raw)) if raw is not None else None
            why = entry_refusal(m, await usage(conn, m, now), est)
    a = await get_account(conn, account_id=intent.account_id)
    fields = account_fields(a.alias, a.kind, a.broker_adapter)
    action = packet.packet["plain_language"]["action"]
    if why is not None:
        if entry:
            await set_intent_status(conn, intent.id, "BLOCKED", f"demo mandate: {why}", now)
        await notify_owners(
            conn,
            kind="auto_trade_skipped",
            now=now,
            dedup_key=f"auto_skip:{intent.id}",
            payload={**fields, "trade_code": intent.trade_code, "action": action, "reason": why},
        )
        return False, why
    expires = min(now + timedelta(seconds=policy.approval_ttl_seconds), intent.intent_expires_at)
    approval_id = uuid7()
    await conn.execute(
        approvals.insert().values(
            id=approval_id,
            intent_id=intent.id,
            packet_id=packet.id,
            packet_hash=packet.packet_hash,
            decision="APPROVED",
            operator_user_id=m.granted_by,  # the owner who granted the mandate
            channel="MANDATE",
            mandate_id=m.id,
            nonce=secrets.token_urlsafe(24),
            policy_version=packet.policy_version,
            decided_at=now,
            expires_at=expires,
            status="ACTIVE",
            note=f"demo mandate {m.id}",
        )
    )
    await set_intent_status(conn, intent.id, "APPROVED", "approved under the demo mandate", now)
    await record(
        conn,
        action="approval.approved",
        actor=ACTOR,
        occurred_at=now,
        entity_type="trade_intent",
        entity_id=intent.id,
        payload={
            **fields,
            "approval_id": approval_id,
            "packet_hash": packet.packet_hash,
            "packet_version": packet.version,
            "channel": "MANDATE",
            "mandate_id": m.id,
            "expires_at": expires,
            "policy_version": packet.policy_version,
        },
    )
    raw = (packet.packet.get("risk") or {}).get("estimated_max_loss")
    await notify_owners(
        conn,
        kind="auto_trade",
        now=now,
        dedup_key=f"auto_trade:{intent.id}",
        payload={
            **fields,
            "trade_code": intent.trade_code,
            "action": action,
            "max_loss": raw,
            "rule": AUTO_RULES.get(m.strategy_key, m.strategy_key),
        },
    )
    return True, "approved"


async def view(conn: AsyncConnection, now: datetime) -> dict[str, Any]:
    """For the Operations page: active mandates with today's use, recently ended ones, and
    what may be granted (simulated accounts and their auto-trader instruments)."""
    active = []
    for m in await active_mandates(conn, now):
        a = await get_account(conn, account_id=m.account_id)
        active.append(
            {
                "m": m,
                "account_ref": account_fields(a.alias, a.kind, a.broker_adapter)["account_ref"],
                "rule": AUTO_RULES.get(m.strategy_key, m.strategy_key),
                "usage": await usage(conn, m, now),
            }
        )
    choices = []
    for a in await list_accounts(conn):
        if a.kind not in SIMULATED_ACCOUNT_KINDS or a.status != "ACTIVE":
            continue
        keys = []
        for key in AUTO_SIGNAL_SERIES:
            try:
                inst = await md.instrument_by_key(conn, key)
            except NotFoundError:
                continue
            if inst.venue_key == a.venue_key and inst.tradable:
                keys.append(key)
        if keys:
            choices.append(
                {
                    "alias": a.alias,
                    "account_ref": account_fields(a.alias, a.kind, a.broker_adapter)["account_ref"],
                    "instruments": keys,
                }
            )
    ended = [m for m in await recent_mandates(conn) if m.status != "ACTIVE"]
    return {"active": active, "choices": choices, "rules": AUTO_RULES, "ended": ended}
