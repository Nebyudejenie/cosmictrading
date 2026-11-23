"""Everything the Manual Trading page shows, gathered in one read (S113; S114: costs, the
contract as reported, open risk and the safety gates).

Three kinds of figures are kept apart and labelled: LIVE ACCOUNT DATA (HFM's own report of the
verified real account), PREVIEW (the owner's typed equity on the demo's contract) and HISTORICAL
RESULTS (HFM's closed deals on the live account). A preview figure never reaches the results."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.config import Settings
from cosmic.core.errors import ValidationFailedError
from cosmic.gold import trend_buy, trend_sim
from cosmic.manual import advice, book, challenge, evidence, execution, insight, journal, live, risk
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
class Reality:
    """The PDF plan simulated at a win rate measured somewhere (SIMULATED)."""

    label: str
    tag: str  # where the win rate comes from
    p: Decimal
    n: int  # the trades it was measured on
    a_year: int | None  # such setups a year, on average
    sim: challenge.Simulated


async def _simulated(p: Decimal, plan: challenge.Scenario) -> challenge.Simulated:
    """A simulation (cached; about 0.3 s of CPU the first time) off the event loop, so the
    bridge's requests to this process are not held up by it."""
    return await asyncio.to_thread(challenge.simulate_at, p, plan)


async def _reality(plan: challenge.Scenario, progress: challenge.Progress | None) -> list[Reality]:
    """The plan at the rule's own tested win rates, and at the owner's once they have enough
    trades. Only at 1:2: the record was measured with a 1:2 target."""
    if plan.rr != trend_buy.REWARD:
        return []
    buy, sell = trend_sim.SIDES[trend_buy.BUY], trend_sim.SIDES[trend_buy.SELL]
    calm = buy.regimes[0]
    days = 250  # weekdays a year, about
    pooled = (buy.p_win * buy.trades + sell.p_win * sell.trades) / (buy.trades + sell.trades)
    rows = [
        Reality(
            "the rule's every tested trade (BUY and SELL)",
            "HISTORICAL SIMULATION · IN SAMPLE",
            pooled,
            buy.trades + sell.trades,
            int((buy.per_day + sell.per_day) * days),
            await _simulated(pooled, plan),
        ),
        Reality(
            "only the setups recommended TAKE (BUY in calm markets)",
            "HISTORICAL SIMULATION · IN SAMPLE",
            calm.p_win,
            calm.trades,
            int(buy.per_day * days / 3),
            await _simulated(calm.p_win, plan),
        ),
    ]
    mine = progress.win_rate if progress is not None else None
    if progress is not None and mine is not None and progress.trades >= challenge.MEASURED_FROM:
        rows.append(
            Reality(
                "your own trades since the plan started",
                "YOUR RESULTS",
                mine,
                progress.trades,
                None,
                await _simulated(mine, plan),
            )
        )
    return rows


def _target_at(entry: Decimal, stop: Decimal, side: str, rr: Decimal, tick: Decimal) -> Decimal:
    """The take-profit ``rr`` times the stop distance away, on the price grid."""
    move = abs(entry - stop) * rr
    raw = entry + move if side == live.BUY else entry - move
    return (raw / tick).to_integral_value() * tick


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
    # S117: the owner's PDF plan, the next trade worked out by itself, and the recommendation.
    plan: challenge.Scenario  # the plan started, or 1:2 (the owner's default) before it starts
    plan_on: bool
    base: Decimal | None  # what 10 % is taken of: the balance (or the equity, if lower)
    next_trade: challenge.NextTrade | None
    progress: challenge.Progress | None
    streak_rows: list[challenge.StreakRow]
    reality: list[Reality]
    advice: advice.Advice
    auto_ticket: Ticket | None
    auto_sizing: Sizing | None
    auto_checks: list[Line]
    auto_verdict: str | None

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
    def auto_query(self) -> dict[str, str]:
        """The calculator filled with the next trade worked out for the owner (S117)."""
        t = self.auto_ticket
        if t is None:
            return {}
        out = {
            "side": t.side,
            "entry": f"{t.entry}",
            "stop": f"{t.stop}",
            "risk_pct": f"{self.rules.risk_pct_default.normalize():f}",
        }
        if t.target is not None:
            out["target"] = f"{t.target}"
        if self.preview and self.equity is not None:
            out["preview_equity"] = f"{self.equity.normalize():f}"
        return out

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
    light: bool = False,
) -> Workspace:
    """``light`` (S117): only what the next trade, its recommendation and the alert need, for
    the page's live part and the worker: no contract evidence, gates, journal or simulation."""
    account = await live.read(conn, now)
    version, rules = await risk.current(conn)
    # HFM's stored spreads are the usual spread only for an HFM price (S115: not Exness's).
    hfm_price = not account.live or account.broker in (None, "HFM")
    spreads = await evidence.spreads(conn) if hfm_price or not light else None
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
    # S117: a trade's risk is a share of the balance, as the owner's PDF says, or of the equity
    # when that is lower (a floating loss): never more than either.
    held = [x for x in (account.balance, account.equity) if x is not None]
    base = equity if preview else (min(held) if held else None)
    placed = await risk.recent_placed(conn, account.server_day(now))

    def sized(t: Ticket) -> tuple[Sizing | None, list[Line]]:
        found: Sizing | None = None
        lines: list[Line] = []
        if spec is None or base is None:
            lines.append(Line(BLOCK, "the gold contract is not reported yet: nothing can be sized"))
        else:
            found = size(
                t,
                equity=base,
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
            lines += found.lines
        return found, lines + risk.check(account, t, found, rules, now, placed)

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
        sizing, checks = sized(ticket)
    # The next trade, worked out by itself from the setup now and the plan (S117).
    plan = rules.plan or challenge.scenario(None)
    auto_ticket: Ticket | None = None
    auto_sizing: Sizing | None = None
    auto_checks: list[Line] = []
    if idea.rule_side is not None and idea.entry is not None and idea.stop is not None:
        tick = spec.tick_size if spec is not None else Decimal("0.01")
        auto_ticket = Ticket(
            idea.rule_side,
            idea.entry,
            idea.stop,
            idea.target
            if plan.rr == trend_buy.REWARD and idea.target is not None
            else _target_at(idea.entry, idea.stop, idea.rule_side, plan.rr, tick),
            risk_pct=rules.risk_pct_default,
            note="",
        )
        auto_sizing, auto_checks = sized(auto_ticket)
    trades = book.closed_trades(account.deals) if account.live else []
    day = account.server_day(now)
    real = account.trade_mode == "REAL" and account.spec is not None and account.broker is not None
    facts_real = (
        None
        if light
        else evidence.facts(
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
    progress = (
        challenge.progress(
            trades,
            started_at=rules.plan_started_at,
            start=rules.plan_start_balance,
            balance=account.balance,
            s=plan,
        )
        if rules.plan_started_at is not None and account.live
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
        locks=[]
        if light
        else await execution.locks(
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
        entries=[] if light else await journal.recent(conn, trades),
        request_key=uuid.uuid4().hex,
        commission=commission,
        contract_from=contract_from,
        swap_long=swap_per_lot(spec, live.BUY, price, currency) if spec else None,
        swap_short=swap_per_lot(spec, live.SELL, price, currency) if spec else None,
        triple=triple_night(spec) if spec else None,
        open_risk=open_cash,
        unlimited=unlimited,
        gates=[] if light else await execution.gates(conn, now),
        leverage=leverage,
        facts_demo=[]
        if light
        else evidence.facts(
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
        plan=plan,
        plan_on=rules.plan is not None,
        base=base,
        next_trade=challenge.next_trade(base, plan, rules.plan_target)
        if base is not None and base > 0
        else None,
        progress=progress,
        streak_rows=challenge.streaks(base if base else challenge.START, plan),
        reality=[] if light else await _reality(plan, progress),
        advice=advice.advise(idea, plan.rr, quote.spread if quote is not None else None),
        auto_ticket=auto_ticket,
        auto_sizing=auto_sizing,
        auto_checks=auto_checks,
        auto_verdict=risk.verdict(auto_checks) if auto_ticket is not None else None,
    )


def _s(x: Decimal | None, places: int = 2) -> str | None:
    return None if x is None else f"{x:.{places}f}"


def now_view(w: Workspace) -> dict[str, Any]:
    """What ``/manual/now.json`` gives the page: prices, the setup, the recommendation and the
    next trade, as text. No account number, no login, no secret."""
    q, t, s, adv = w.quote, w.auto_ticket, w.auto_sizing, w.advice
    lines = [c for c in w.auto_checks if c.level != "OK"]
    return {
        "as_of": w.now.isoformat(),
        "live": w.account.live,
        "preview": w.preview,
        "bid": _s(q.bid) if q else None,
        "ask": _s(q.ask) if q else None,
        "spread": _s(q.spread) if q else None,
        "fresh": bool(q and q.fresh),
        "direction": w.idea.direction,
        "side": t.side if t else None,
        "entry": _s(t.entry) if t else None,
        "stop": _s(t.stop) if t else None,
        "target": _s(t.target) if t and t.target is not None else None,
        "lots": f"{s.lots.normalize():f}" if s and not s.blocked else None,
        "risk": _s(s.risk_cash) if s and not s.blocked else None,
        "reward": _s(s.reward_cash) if s and not s.blocked and s.reward_cash else None,
        "verdict": w.auto_verdict,
        "checks": [f"{c.level}: {c.text}" for c in lines][:6],
        "advice": adv.level,
        "advice_why": adv.why,
        "rule_read_at": w.idea.as_of.isoformat() if w.idea.as_of else None,
    }
