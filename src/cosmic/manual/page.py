"""Everything the Manual Trading page shows, gathered in one read (S113)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.config import Settings
from cosmic.core.errors import ValidationFailedError
from cosmic.manual import book, execution, insight, journal, live, risk
from cosmic.manual.calc import BLOCK, Line, Sizing, Ticket, size

PREVIEW_EQUITY = Decimal(100)


def _num(raw: Any, label: str) -> Decimal | None:
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValidationFailedError(f"{label}: write a number, for example 4021.50") from None
    if not value.is_finite():
        raise ValidationFailedError(f"{label}: write a number")
    return value


def ticket_from(params: Any, default_risk: Decimal) -> Ticket | None:
    """The owner's ticket from the form, or None when no price was given yet."""
    side = str(params.get("side") or "").upper()
    entry, stop = _num(params.get("entry"), "Entry"), _num(params.get("stop"), "Stop-loss")
    if not side and entry is None and stop is None:
        return None
    if side not in (live.BUY, live.SELL):
        raise ValidationFailedError("choose BUY or SELL")
    if entry is None or stop is None:
        raise ValidationFailedError("give the entry and the stop-loss: the size depends on them")
    amount = _num(params.get("risk_amount"), "Risk amount")
    pct = _num(params.get("risk_pct"), "Risk %")
    return Ticket(
        side=side,
        entry=entry,
        stop=stop,
        target=_num(params.get("target"), "Target"),
        risk_pct=pct if amount is None else None,
        risk_amount=amount,
        note=str(params.get("note") or "")[:1000],
    )


@dataclass(frozen=True)
class Workspace:
    now: datetime
    account: live.LiveAccount
    preview: bool  # the calculator on the demo's contract: never the live account's
    spec: live.Spec | None
    quote: live.Quote | None
    equity: Decimal | None
    rules_version: int
    rules: risk.RiskRules
    idea: insight.Insight
    ticket: Ticket | None
    sizing: Sizing | None
    checks: list[Line]
    verdict: str | None
    locks: list[str]
    stats: book.Stats
    today: Decimal
    week: Decimal
    month: Decimal
    entries: list[journal.Entry]
    request_key: str

    @property
    def insight_query(self) -> dict[str, str]:
        """The form filled with Trader Insight's idea."""
        i = self.idea
        if i.entry is None or i.rule_side is None or i.stop is None:
            return {}
        out = {
            "side": i.rule_side,
            "entry": f"{i.entry}",
            "stop": f"{i.stop}",
            "risk_pct": f"{self.rules.risk_pct_default.normalize():f}",
        }
        if i.target is not None:
            out["target"] = f"{i.target}"
        return out


async def build(
    conn: AsyncConnection,
    now: datetime,
    settings: Settings,
    params: Any,
    *,
    preview_equity: Decimal | None = None,
) -> Workspace:
    account = await live.read(conn, now)
    version, rules = await risk.current(conn)
    idea = await insight.build(conn, now, account)
    preview = not account.live
    equity: Decimal | None
    margin_free: Decimal | None
    currency: str | None
    if preview:
        demo = await live.demo_contract(conn, now)
        spec, quote, leverage = demo.spec, demo.quote, demo.leverage
        equity = preview_equity or PREVIEW_EQUITY
        margin_free, currency = equity, "USD"
    else:
        spec, quote, leverage = account.spec, account.quote, account.leverage
        equity, margin_free, currency = account.equity, account.margin_free, account.currency
    ticket = ticket_from(params, rules.risk_pct_default)
    sizing: Sizing | None = None
    checks: list[Line] = []
    if ticket is not None:
        if ticket.risk_pct is None and ticket.risk_amount is None:
            ticket = Ticket(
                ticket.side,
                ticket.entry,
                ticket.stop,
                ticket.target,
                risk_pct=rules.risk_pct_default,
                note=ticket.note,
            )
        if spec is None or equity is None:
            checks.append(
                Line(BLOCK, "HFM's gold contract is not reported yet: nothing can be sized")
            )
        else:
            sizing = size(
                ticket,
                equity=equity,
                margin_free=margin_free,
                leverage=leverage,
                currency=currency,
                spec=spec,
                quote=quote if quote is not None and quote.fresh else None,
                max_risk_pct=rules.risk_pct_max,
                atr=idea.atr,
            )
        placed = await risk.recent_placed(conn, account.server_day(now))
        checks += list(sizing.lines if sizing is not None else ()) + risk.check(
            account, ticket, sizing, rules, now, placed
        )
    trades = book.closed_trades(account.deals)
    day = account.server_day(now)
    return Workspace(
        now=now,
        account=account,
        preview=preview,
        spec=spec,
        quote=quote,
        equity=equity,
        rules_version=version,
        rules=rules,
        idea=idea,
        ticket=ticket,
        sizing=sizing,
        checks=checks,
        verdict=risk.verdict(checks) if ticket is not None else None,
        locks=await execution.locks(
            conn,
            settings,
            now,
            account_live=account.live,
            account_read_only=account.read_only,
        ),
        stats=book.stats(trades),
        today=book.period(trades, day),
        week=book.period(trades, book.week_start(day, account.server_offset)),
        month=book.period(trades, book.month_start(day, account.server_offset)),
        entries=await journal.recent(conn, trades),
        request_key=uuid.uuid4().hex,
    )
