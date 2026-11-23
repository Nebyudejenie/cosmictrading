"""Everything the Manual Trading page shows, gathered in one read (S113; S114: costs, the
contract as reported, open risk and the safety gates).

Three kinds of figures are kept apart and labelled: LIVE ACCOUNT DATA (HFM's own report of the
verified real account), PREVIEW (the owner's typed equity on the demo's contract) and HISTORICAL
RESULTS (HFM's closed deals on the live account). A preview figure never reaches the results."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.config import Settings
from cosmic.core.errors import ValidationFailedError
from cosmic.manual import book, evidence, execution, insight, journal, live, risk
from cosmic.manual.calc import (
    BLOCK,
    Commission,
    Line,
    Sizing,
    Ticket,
    commission_of,
    size,
    swap_per_lot,
    triple_night,
)

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
    lots = _num(params.get("lots"), "Lot size")
    if lots is not None and lots <= 0:
        raise ValidationFailedError("Lot size: more than 0, or leave it empty")
    return Ticket(
        side=side,
        entry=entry,
        stop=stop,
        target=_num(params.get("target"), "Target"),
        risk_pct=pct if amount is None else None,
        risk_amount=amount,
        note=str(params.get("note") or "")[:1000],
        lots=lots,
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
    commission: Commission
    contract_from: str  # which terminal reported the contract, and when
    swap_long: Decimal | None  # a lot, a night, in the account's currency
    swap_short: Decimal | None
    triple: str | None
    open_risk: Decimal | None  # the money lost if every open stop-loss is hit
    unlimited: list[str]  # open positions without a stop-loss
    gates: list[execution.GateRow]
    leverage: int | None  # the one the margin estimate uses
    facts_demo: list[evidence.Fact]  # HFM's contract on the demo, and what proves each figure
    facts_real: list[evidence.Fact] | None  # the same on the real account, once it reports
    spreads: evidence.Spreads | None
    months: list[book.Period]
    weeks: list[book.Period]
    days: list[book.Period]
    current_drawdown: Decimal
    record: list[insight.RecordRow]

    @property
    def fact_rows(self) -> list[tuple[str, evidence.Fact | None, evidence.Fact | None]]:
        """Each contract figure on the demo and on the real account, side by side."""
        demo = {f.item: f for f in self.facts_demo}
        real = {f.item: f for f in self.facts_real or []}
        items = list(demo) + [k for k in real if k not in demo]
        return [(k, demo.get(k), real.get(k)) for k in items]

    @property
    def open_lots(self) -> Decimal:
        return sum((p.lots for p in self.account.positions), Decimal(0))

    @property
    def forward_min(self) -> int:
        return insight.FORWARD_MIN

    @property
    def open_risk_pct(self) -> Decimal | None:
        a = self.account
        if not a.live or self.open_risk is None or not a.equity:
            return None
        return self.open_risk / a.equity * 100

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
    spreads = await evidence.spreads(conn)
    # HFM's stored spreads are the usual spread only for an HFM price (S115: not Exness's).
    hfm_price = not account.live or account.broker in (None, "HFM")
    usual = spreads.median if spreads is not None and hfm_price else None
    idea = await insight.build(conn, now, account, normal_spread=usual)
    preview = not account.live
    equity: Decimal | None
    margin_free: Decimal | None
    currency: str | None
    demo = await live.demo_contract(conn, now)
    if preview:
        spec, quote, leverage = demo.spec, demo.quote, demo.leverage
        equity = preview_equity or PREVIEW_EQUITY
        margin_free, currency = equity, "USD"
        commission = commission_of(demo.deals)
        contract_from = "terminal 1 (HFM demo)" + (
            f", reported {demo.reported_at:%Y-%m-%d %H:%M:%S} UTC" if demo.reported_at else ""
        )
    else:
        spec, quote, leverage = account.spec, account.quote, account.leverage
        equity, margin_free, currency = account.equity, account.margin_free, account.currency
        commission = commission_of(account.deals)
        contract_from = f"terminal 3 (your real {account.broker or ''} account)".replace(
            "  ", " "
        ) + (
            f", reported {account.reported_at:%Y-%m-%d %H:%M:%S} UTC" if account.reported_at else ""
        )
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
                commission_per_lot=commission.per_lot or Decimal(0),
                atr=idea.atr,
                normal_spread=usual,
                margin_used=None if preview else account.margin,
                stop_out_pct=account.stop_out
                if not preview and account.stop_out_mode == "PERCENT"
                else None,
                broker="HFM" if preview else account.broker or "the broker",
            )
        placed = await risk.recent_placed(conn, account.server_day(now))
        checks += list(sizing.lines if sizing is not None else ()) + risk.check(
            account, ticket, sizing, rules, now, placed
        )
    trades = book.closed_trades(account.deals) if account.live else []
    day = account.server_day(now)
    real = account.trade_mode == "REAL" and account.spec is not None and account.broker is not None
    facts_real = (
        evidence.facts(
            account.spec,
            deals=account.deals,
            commission=commission_of(account.deals),
            currency=account.currency,
            leverage=account.leverage,
            quote=account.quote,
            spread_history=None,
            stop_out=account.stop_out,
            stop_out_mode=account.stop_out_mode,
            margin_call=account.margin_call,
            broker=account.broker or "the broker",
        )
        if real
        else None
    )
    price = quote.mid if quote is not None else Decimal(0)
    open_cash, unlimited = (
        risk.open_risk(account, account.spec)
        if account.live and account.spec is not None
        else (None, [])
    )
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
        commission=commission,
        contract_from=contract_from,
        swap_long=swap_per_lot(spec, live.BUY, price, currency) if spec else None,
        swap_short=swap_per_lot(spec, live.SELL, price, currency) if spec else None,
        triple=triple_night(spec) if spec else None,
        open_risk=open_cash,
        unlimited=unlimited,
        gates=await execution.gates(conn, now),
        leverage=leverage,
        facts_demo=evidence.facts(
            demo.spec,
            deals=demo.deals,
            commission=commission_of(demo.deals),
            currency=demo.currency,
            leverage=demo.leverage,
            quote=demo.quote,
            spread_history=spreads,
        ),
        facts_real=facts_real,
        spreads=spreads,
        months=book.by_month(trades, account.server_offset)[-6:],
        weeks=book.by_week(trades, account.server_offset)[-6:],
        days=book.by_day(trades, account.server_offset)[-10:],
        current_drawdown=book.current_drawdown(trades),
        record=insight.record_rows(),
    )
