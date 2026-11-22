"""The owner's trade journal (S113): each decision at a trade preview, as it was shown, and the
result HFM's own deals give it once the trade is closed. Append-only (migration 0038)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import record as audit
from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.db.schema_manual import manual_journal
from cosmic.identity.service import Principal
from cosmic.manual.book import Trade
from cosmic.manual.calc import Line, Sizing, Ticket
from cosmic.manual.live import SYMBOL

KINDS = ("PLACED", "SKIPPED", "NOTE")
MATCH_WINDOW = timedelta(minutes=30)  # a recorded trade and HFM's opening deal this close


async def add(
    conn: AsyncConnection,
    *,
    principal: Principal,
    kind: str,
    ticket: Ticket | None,
    sizing: Sizing | None,
    checks: Sequence[Line],
    insight: dict[str, Any],
    note: str,
    request_key: str,
    currency: str | None,
    fingerprint: str | None,
    now: datetime,
) -> uuid.UUID:
    """One entry; the same ``request_key`` twice is refused (a double click), never doubled."""
    if kind not in KINDS:
        raise ValidationFailedError("choose PLACED, SKIPPED or NOTE")
    note = note.strip()
    if len(note) > 1000:
        raise ValidationFailedError("the note is longer than 1,000 characters")
    if kind == "SKIPPED" and not note:
        raise ValidationFailedError("say why you skipped it: it is what the journal learns from")
    if kind != "NOTE" and ticket is None:
        raise ValidationFailedError("a placed or skipped trade needs its prices")
    if not 8 <= len(request_key) <= 80:
        raise ValidationFailedError("the form is out of date: open the page again")
    entry_id = uuid7()
    values: dict[str, Any] = {
        "id": entry_id,
        "owner_id": principal.user_id,
        "created_at": now,
        "kind": kind,
        "symbol": SYMBOL,
        "insight": to_jsonable(insight),
        "checks": [{"level": x.level, "text": x.text} for x in checks],
        "note": note or None,
        "request_key": request_key,
        "currency": currency,
        "account_fingerprint": fingerprint,
    }
    if ticket is not None:
        values.update(side=ticket.side, entry=ticket.entry, stop=ticket.stop, target=ticket.target)
    if sizing is not None and not sizing.blocked:
        values.update(
            lots=sizing.lots,
            risk_cash=sizing.risk_cash,
            risk_pct=sizing.risk_pct,
            reward_cash=sizing.reward_cash,
        )
    try:
        async with conn.begin_nested():
            await conn.execute(manual_journal.insert().values(**values))
    except IntegrityError:
        raise ConflictError("this was already recorded (the same form was sent twice)") from None
    await audit(
        conn,
        action=f"manual.journal_{kind.lower()}",
        actor=principal.actor,
        occurred_at=now,
        entity_type="manual_journal",
        entity_id=entry_id,
        payload={k: v for k, v in values.items() if k not in ("insight", "checks", "owner_id")},
    )
    return entry_id


@dataclass(frozen=True)
class Entry:
    id: uuid.UUID
    at: datetime
    kind: str
    side: str | None
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    lots: Decimal | None
    risk_cash: Decimal | None
    risk_pct: Decimal | None
    note: str | None
    checks: list[dict[str, str]]
    insight: dict[str, Any]
    result: Trade | None  # HFM's closed trade it matches, once closed


async def recent(conn: AsyncConnection, trades: Sequence[Trade], limit: int = 30) -> list[Entry]:
    rows = list(
        await conn.execute(
            select(manual_journal).order_by(desc(manual_journal.c.created_at)).limit(limit)
        )
    )
    used: set[int] = set()
    out = []
    for r in reversed(rows):
        match = None
        if r.kind == "PLACED" and r.side:
            for t in trades:
                if (
                    t.position_id not in used
                    and t.side == r.side
                    and abs(t.opened_at - r.created_at) <= MATCH_WINDOW
                    and (r.lots is None or t.lots == r.lots)
                ):
                    match = t
                    used.add(t.position_id)
                    break
        out.append(
            Entry(
                r.id,
                r.created_at,
                r.kind,
                r.side,
                r.entry,
                r.stop,
                r.target,
                r.lots,
                r.risk_cash,
                r.risk_pct,
                r.note,
                list(r.checks or []),
                dict(r.insight or {}),
                match,
            )
        )
    return list(reversed(out))
