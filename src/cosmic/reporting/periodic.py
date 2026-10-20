"""Weekly and monthly reports (directive Phase 10): comparable periods, clear definitions.

A period is a complete ISO week (Monday 00:00 UTC to the next Monday) or calendar month, sent at
the first check after the report hour on the Monday or the 1st. Each account is reported apart,
in its own currency and environment, next to the same figure for the period before it; nothing
is added across accounts. Fees are listed per currency as the venue charged them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema import incidents, notifications
from cosmic.db.schema_execution import fills, post_trade_reviews, trade_intents
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_trading import accounts, equity_snapshots
from cosmic.reporting.command_center import connections, strategy_status
from cosmic.reporting.daily import backup_line, money, strategy_line
from cosmic.trading.modes import environment_short

WEEKLY, MONTHLY = "weekly_report", "monthly_report"
CENTS = frozenset({"USD", "USDT", "USDC", "EUR"})
DEFINITIONS = (
    "Definitions: equity = cash + holdings at the last fresh price (5-minute samples); change = "
    "last sample in the period against the first; drawdown = the largest fall from a peak "
    "within the period; realised = trades closed in the period, after fees."
)


@dataclass(frozen=True)
class Period:
    kind: str
    start: datetime
    end: datetime  # exclusive
    label: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.start.date().isoformat()}"


def _midnight(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def last_week(now: datetime) -> Period:
    """The last complete ISO week before ``now``."""
    end = _midnight(now) - timedelta(days=now.astimezone(UTC).weekday())
    start = end - timedelta(days=7)
    year, week, _ = start.isocalendar()
    label = f"{year}-W{week:02d} ({start:%a %d %b} – {end - timedelta(days=1):%a %d %b})"
    return Period(WEEKLY, start, end, label)


def last_month(now: datetime) -> Period:
    """The last complete calendar month before ``now``."""
    end = _midnight(now).replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    return Period(MONTHLY, start, end, f"{start:%B %Y}")


def before(p: Period) -> Period:
    """The period of the same kind just before ``p`` (for comparison)."""
    return last_week(p.start) if p.kind == WEEKLY else last_month(p.start)


@dataclass(frozen=True)
class AccountPeriod:
    first: Decimal | None
    last: Decimal | None
    max_drawdown_pct: Decimal | None
    realised: Decimal
    trades: int
    won: int
    lost: int
    fees: dict[str, Decimal]

    @property
    def change_pct(self) -> Decimal | None:
        if self.first is None or self.last is None or self.first <= 0:
            return None
        return (self.last - self.first) / self.first * 100


async def account_period(conn: AsyncConnection, account_id: object, p: Period) -> AccountPeriod:
    samples = [
        r.equity
        for r in await conn.execute(
            select(equity_snapshots.c.equity)
            .where(
                equity_snapshots.c.account_id == account_id,
                equity_snapshots.c.taken_at >= p.start,
                equity_snapshots.c.taken_at < p.end,
            )
            .order_by(equity_snapshots.c.taken_at)
        )
    ]
    worst: Decimal | None = None
    peak: Decimal | None = None
    for e in samples:
        peak = e if peak is None or e > peak else peak
        if peak > 0:
            dd = (peak - e) / peak * 100
            worst = dd if worst is None or dd > worst else worst
    closed = list(
        await conn.execute(
            select(post_trade_reviews.c.realized_pnl)
            .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
            .where(
                trade_intents.c.account_id == account_id,
                post_trade_reviews.c.created_at >= p.start,
                post_trade_reviews.c.created_at < p.end,
            )
        )
    )
    pnl = [r.realized_pnl or Decimal(0) for r in closed]
    fees: dict[str, Decimal] = defaultdict(Decimal)
    for r in await conn.execute(
        select(fills.c.fee_currency, func.sum(fills.c.fee).label("fee"))
        .join(orders_t, orders_t.c.id == fills.c.order_id)
        .where(
            orders_t.c.account_id == account_id,
            fills.c.filled_at >= p.start,
            fills.c.filled_at < p.end,
        )
        .group_by(fills.c.fee_currency)
    ):
        if r.fee:
            fees[r.fee_currency] += r.fee
    return AccountPeriod(
        first=samples[0] if samples else None,
        last=samples[-1] if samples else None,
        max_drawdown_pct=worst,
        realised=sum(pnl, Decimal(0)),
        trades=len(pnl),
        won=sum(1 for x in pnl if x > 0),
        lost=sum(1 for x in pnl if x < 0),
        fees=dict(fees),
    )


def _fee(value: Decimal, currency: str) -> str:
    """Cents for dollar-like currencies; an asset fee (e.g. BTC) keeps its precision."""
    shown = money(value) if currency in CENTS else f"{value.normalize():f}"
    return f"{shown} {currency}"


def _duration(d: timedelta) -> str:
    minutes = int(d.total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _pct(value: Decimal | None) -> str:
    return "UNKNOWN" if value is None else f"{value:+.2f} %"


async def period_report(conn: AsyncConnection, p: Period, now: datetime) -> str:
    title = "weekly" if p.kind == WEEKLY else "monthly"
    lines = [f"COSMICTRADING {title} report · {p.label}, UTC"]
    lines.append("Accounts (each in its own currency; never added together):")
    prior = before(p)
    rows = list(
        await conn.execute(
            select(accounts).where(accounts.c.status == "ACTIVE").order_by(accounts.c.alias)
        )
    )
    for a in rows:
        now_p = await account_period(conn, a.id, p)
        then = await account_period(conn, a.id, prior)
        env = environment_short(a.kind, a.broker_adapter)
        ccy = a.base_currency
        lines.append(
            f"• {a.alias} [{env}] {ccy}: equity {money(now_p.first)} → {money(now_p.last)} "
            f"({_pct(now_p.change_pct)}; the period before: {_pct(then.change_pct)}) · "
            f"max drawdown {_pct(now_p.max_drawdown_pct).lstrip('+')}"
        )
        if now_p.trades:
            fees = ", ".join(_fee(v, c) for c, v in sorted(now_p.fees.items()))
            lines.append(
                f"   {now_p.trades} trade(s) closed ({now_p.won} won, {now_p.lost} lost) · "
                f"realised {money(now_p.realised)} {ccy} · fees {fees or 'none'}"
            )
        else:
            lines.append("   no trade closed")
    if not rows:
        lines.append("• no accounts")
    opened = list(
        await conn.execute(
            select(
                incidents.c.severity,
                incidents.c.title,
                incidents.c.opened_at,
                incidents.c.resolved_at,
            ).where(incidents.c.opened_at >= p.start, incidents.c.opened_at < p.end)
        )
    )
    critical = sum(1 for i in opened if i.severity == "CRITICAL")
    longest = max(opened, key=lambda i: (i.resolved_at or now) - i.opened_at, default=None)
    lines.append(
        f"Incidents: {len(opened)} opened ({critical} critical)"
        + (
            f" · longest: {longest.title} "
            f"({_duration((longest.resolved_at or now) - longest.opened_at)})"
            if longest is not None
            else ""
        )
    )
    links = await connections(conn, now)
    lines.append(backup_line(links["backups"], now))
    lines.append(strategy_line(await strategy_status(conn)))
    lines.append(DEFINITIONS)
    return "\n".join(lines)[:3900]


def due_periods(now: datetime, hour: int) -> list[Period]:
    """The reports due at ``now``: weekly on Mondays and monthly on the 1st, after ``hour``."""
    t = now.astimezone(UTC)
    if t.hour < hour:
        return []
    out = []
    if t.weekday() == 0:
        out.append(last_week(t))
    if t.day == 1:
        out.append(last_month(t))
    return out


async def already_sent(conn: AsyncConnection, p: Period) -> bool:
    n = await conn.scalar(
        select(func.count())
        .select_from(notifications)
        .where(notifications.c.dedup_key.like(f"{p.key}:%"))
    )
    return bool(n)
