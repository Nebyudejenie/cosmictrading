"""DEMO TRAINING MODE data and reports (S80, the P0 demo directive of 2026-11-11), read from the
records the robots already keep. Nothing here trades, and nothing here changes a rule.

Per closed robot trade at HFM:
- its signal (the decision's reasons) and its market regime (the hourly trend and the
  volatility at entry);
- the timeframe and session;
- the spread at the decision;
- the stop and take-profit distances;
- the planned risk and the result;
- the entry slippage;
- the outcome, R multiple, MAE and MFE (from HFM's 1-minute candles while it was open) and the
  holding time;
- the rule and AI versions in force.

The report gives:
- win rate, profit factor and expectancy;
- the Sharpe ratio, only with 20 trading days or more;
- the largest drawdown, and the average win and loss;
- results by regime and by session;
- the trade count;
- the signals not taken, and why.

Collected trades are data, not learning: the rules change only by a new, versioned rule tested
out of sample (the directive: "Do not claim the AI has learned merely because it has collected
trades")."""

from __future__ import annotations

import math
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import ZERO
from cosmic.db.schema_execution import post_trade_reviews, trade_intents, trading_mandates
from cosmic.db.schema_gold import ai_gold_opinions, gold_hfm_decisions
from cosmic.db.schema_market import bars, instruments, sources

RULE_VERSIONS = {  # the rules' versions; a change of rule is a new version (never in place)
    "gold_reading": "gold_reading v1 (S64–S79: M1 RSI-50 turn in the H1/D1 trend, 1.5×H1 ATR)",
    "gold_daily": "gold_daily v1 (S75: Turtle 55/20 on D1, 2×ATR(20))",
}
ROBOT_RULES = tuple(RULE_VERSIONS)
SHARPE_DAYS = 20  # fewer trading days than this: no Sharpe ratio (too little to mean much)


def session_of(at: datetime) -> str:
    """HFM gold's main sessions, by the hour in UTC."""
    h = at.hour
    if h < 7:
        return "Asia (00–07 UTC)"
    if h < 12:
        return "London (07–12 UTC)"
    if h < 16:
        return "London + New York (12–16 UTC)"
    if h < 21:
        return "New York (16–21 UTC)"
    return "Late (21–24 UTC)"


def regime_of(reasons: list[str], stop_distance: Decimal | None, price: Decimal | None) -> str:
    """The hourly trend the rule saw, and how volatile the market was (the stop distance, 1.5 ×
    the hourly ATR, as a share of the price)."""
    trend = next((r.split("H1 trend ", 1)[1].split()[0] for r in reasons if "H1 trend " in r), "")
    vol = ""
    if stop_distance and price:
        pct = stop_distance / price * 100
        vol = "quiet" if pct < Decimal("0.4") else "busy" if pct > Decimal("0.9") else "normal"
    return f"{trend or 'unknown'} trend, {vol or 'unknown'} market"


@dataclass(frozen=True)
class Trade:
    intent_id: uuid.UUID
    rule: str
    rule_version: str
    timeframe: str
    side: str
    opened_at: datetime
    closed_at: datetime
    ounces: Decimal
    entry: Decimal | None
    exit: Decimal | None
    spread: Decimal | None
    stop_distance: Decimal | None
    take_profit_distance: Decimal | None
    planned_risk: Decimal | None
    net: Decimal  # after commission and swap
    r_multiple: Decimal | None
    slippage_bps: Decimal | None
    mae: Decimal | None  # the worst floating result while open (≤ 0)
    mfe: Decimal | None  # the best floating result while open (≥ 0)
    signal: str
    regime: str
    session: str
    ai_version: str

    @property
    def outcome(self) -> str:
        return "WIN" if self.net > 0 else "LOSS" if self.net < 0 else "FLAT"

    @property
    def held(self) -> timedelta:
        return self.closed_at - self.opened_at


@dataclass
class Report:
    trades: list[Trade]
    count: int = 0
    win_rate: Decimal | None = None
    profit_factor: Decimal | None = None
    expectancy: Decimal | None = None  # net USD a trade
    expectancy_r: Decimal | None = None
    average_win: Decimal | None = None
    average_loss: Decimal | None = None
    net: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    sharpe: Decimal | None = None
    sharpe_why: str = ""
    by_regime: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_session: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejected: list[tuple[str, int]] = field(default_factory=list)


def _d(v: Any) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def summary(trades: list[Trade]) -> dict[str, Any]:
    """Win rate, profit factor and expectancy of a group of trades."""
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net < 0]
    n = len(trades)
    gross_loss = -sum(losses, ZERO)
    return {
        "count": n,
        "win_rate": (Decimal(len(wins)) / n * 100).quantize(Decimal("0.1")) if n else None,
        "profit_factor": (sum(wins, ZERO) / gross_loss).quantize(Decimal("0.01"))
        if gross_loss > 0
        else None,
        "expectancy": (sum((t.net for t in trades), ZERO) / n).quantize(Decimal("0.01"))
        if n
        else None,
        "net": sum((t.net for t in trades), ZERO).quantize(Decimal("0.01")),
    }


def report(trades: list[Trade], rejected: list[tuple[str, int]]) -> Report:
    trades = sorted(trades, key=lambda t: t.closed_at)
    out = Report(trades, rejected=rejected)
    s = summary(trades)
    out.count, out.win_rate, out.profit_factor = s["count"], s["win_rate"], s["profit_factor"]
    out.expectancy, out.net = s["expectancy"], s["net"]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    out.expectancy_r = (sum(rs, ZERO) / len(rs)).quantize(Decimal("0.01")) if rs else None
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net < 0]
    out.average_win = (sum(wins, ZERO) / len(wins)).quantize(Decimal("0.01")) if wins else None
    out.average_loss = (
        (sum(losses, ZERO) / len(losses)).quantize(Decimal("0.01")) if losses else None
    )
    equity = peak = ZERO
    for t in trades:
        equity += t.net
        peak = max(peak, equity)
        out.max_drawdown = max(out.max_drawdown, peak - equity)
    days: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in trades:
        days[t.closed_at.date().isoformat()] += t.net
    if len(days) >= SHARPE_DAYS:
        values = [float(v) for v in days.values()]
        sd = statistics.stdev(values)
        out.sharpe = (
            Decimal(str(round(statistics.mean(values) / sd * math.sqrt(252), 2))) if sd else None
        )
    else:
        out.sharpe_why = (
            f"not shown: {len(days)} trading day(s) with closed trades; it needs {SHARPE_DAYS}"
        )
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        groups[t.regime].append(t)
    out.by_regime = {k: summary(v) for k, v in sorted(groups.items())}
    groups = defaultdict(list)
    for t in trades:
        groups[t.session].append(t)
    out.by_session = {k: summary(v) for k, v in sorted(groups.items())}
    return out


async def _excursion(
    conn: AsyncConnection,
    side: str,
    ounces: Decimal,
    entry: Decimal | None,
    a: datetime,
    b: datetime,
) -> tuple[Decimal | None, Decimal | None]:
    """MAE and MFE from HFM's own 1-minute candles (bid) while the trade was open."""
    if entry is None:
        return None, None
    row = (
        await conn.execute(
            select(func.min(bars.c.low), func.max(bars.c.high))
            .join(instruments, instruments.c.id == bars.c.instrument_id)
            .join(sources, sources.c.id == bars.c.source_id)
            .where(
                instruments.c.canonical_key == "CFD:XAU/USD@HFM",
                sources.c.key == "hfm_mt5_bridge",
                bars.c.timeframe == "1m",
                bars.c.open_time >= a.replace(second=0, microsecond=0),
                bars.c.open_time <= b,
            )
        )
    ).first()
    if row is None or row[0] is None:
        return None, None
    low, high = Decimal(str(row[0])), Decimal(str(row[1]))
    if side == "BUY":
        return min((low - entry) * ounces, ZERO), max((high - entry) * ounces, ZERO)
    return min((entry - high) * ounces, ZERO), max((entry - low) * ounces, ZERO)


async def trades(conn: AsyncConnection, limit: int = 500) -> list[Trade]:
    """The robots' closed trades at HFM, newest last."""
    rows = list(
        await conn.execute(
            select(
                trade_intents,
                post_trade_reviews.c.created_at.label("closed_at"),
                post_trade_reviews.c.actual,
                post_trade_reviews.c.assessment,
                post_trade_reviews.c.realized_pnl,
                trading_mandates.c.strategy_key,
                trading_mandates.c.timeframe.label("tf"),
            )
            .join(post_trade_reviews, post_trade_reviews.c.intent_id == trade_intents.c.id)
            .join(
                trading_mandates,
                trading_mandates.c.id
                == trade_intents.c.details["mandate_id"].astext.cast(trading_mandates.c.id.type),
            )
            .where(trading_mandates.c.strategy_key.in_(ROBOT_RULES))
            .order_by(desc(post_trade_reviews.c.created_at))
            .limit(limit)
        )
    )
    out = []
    for r in reversed(rows):
        actual = r.actual or {}
        sizing = (r.assessment or {}).get("sizing") or {}
        decision = (
            await conn.execute(
                select(gold_hfm_decisions).where(gold_hfm_decisions.c.intent_id == r.id)
            )
        ).first()
        plan = r.management_plan or {}
        entry = _d(actual.get("entry_avg_price"))
        ounces = _d(actual.get("entry_filled")) or r.quantity
        stop = _d((plan.get("stop_loss") or {}).get("stop_price"))
        target = _d(plan.get("take_profit_price"))
        net = _d(r.realized_pnl) or ZERO
        risk = _d(sizing.get("planned_max_loss"))
        opened = r.created_at
        mae, mfe = await _excursion(conn, r.side, ounces, entry, opened, r.closed_at)
        opinion = (
            await conn.execute(
                select(ai_gold_opinions.c.provider_key, ai_gold_opinions.c.model_id)
                .where(ai_gold_opinions.c.at <= opened)
                .order_by(desc(ai_gold_opinions.c.at))
                .limit(1)
            )
        ).first()
        reasons = list(decision.reasons) if decision is not None else []
        bid = decision.bid if decision is not None else None
        ask = decision.ask if decision is not None else None
        out.append(
            Trade(
                intent_id=r.id,
                rule=r.strategy_key,
                rule_version=RULE_VERSIONS.get(r.strategy_key, r.strategy_key),
                timeframe=r.tf,
                side=r.side,
                opened_at=opened,
                closed_at=r.closed_at,
                ounces=ounces,
                entry=entry,
                exit=_d(actual.get("exit_avg_price")),
                spread=(ask - bid) if bid is not None and ask is not None else None,
                stop_distance=abs(entry - stop) if entry is not None and stop is not None else None,
                take_profit_distance=abs(target - entry)
                if entry is not None and target is not None
                else None,
                planned_risk=risk,
                net=net,
                r_multiple=(net / risk).quantize(Decimal("0.01")) if risk else None,
                slippage_bps=_d(actual.get("entry_slippage_vs_limit_bps")),
                mae=mae,
                mfe=mfe,
                signal="; ".join(reasons[:-1])[:300] if reasons else "",
                regime=regime_of(reasons, decision.stop_distance if decision else None, bid),
                session=session_of(opened),
                ai_version=f"{opinion.provider_key} {opinion.model_id}"
                if opinion is not None
                else "no AI opinion in force",
            )
        )
    return out


async def rejected(conn: AsyncConnection, since: datetime) -> list[tuple[str, int]]:
    """The robots' BUY or SELL signals not taken since ``since``, by why (most often first)."""
    rows = await conn.execute(
        select(gold_hfm_decisions.c.reasons).where(
            gold_hfm_decisions.c.at >= since,
            gold_hfm_decisions.c.decision.in_(["BUY", "SELL"]),
            gold_hfm_decisions.c.action.in_(["WAIT", "REFUSED"]),
        )
    )
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        why = (r.reasons or ["?"])[-1]
        counts[why.split(":", 1)[0][:90] if why.startswith("WAIT") else why[:90]] += 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


async def build(conn: AsyncConnection, now: datetime, days: int = 90) -> Report:
    return report(await trades(conn), await rejected(conn, now - timedelta(days=days)))
