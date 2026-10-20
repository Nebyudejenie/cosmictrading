"""The owner's short daily report and the Telegram health summary (directive Phases 8 and 10).

Every account is reported on its own line, in its own currency and environment (PAPER,
TESTNET, DEMO, LIVE): results are never added across accounts, so paper or test money is never
mixed with real money. Realised and unrealised P&L stay apart. A value the system cannot verify
is shown as UNKNOWN, never guessed. Plain text, no secrets, aliases only. What needs attention
follows the same rules as the portal's Overview (``command_center.attention_items``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema import incidents, jobs, notifications
from cosmic.db.schema_execution import post_trade_reviews, trade_intents
from cosmic.db.schema_trading import accounts
from cosmic.reporting.command_center import attention_items, connections, strategy_status
from cosmic.trading import queries
from cosmic.trading.modes import environment_short

DAY = timedelta(hours=24)
KIND = "daily_report"
MAX_POSITIONS = 4  # per account, to keep the message short
SHOWN_ABOVE = ("pending", "worker")  # attention kinds the health lines already state


def money(value: Decimal | None, places: str = "0.01") -> str:
    if value is None:
        return "UNKNOWN"
    return f"{value.quantize(Decimal(places), ROUND_HALF_EVEN):,}"


def _age(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def _ts(value: str | None, now: datetime) -> str:
    if not value:
        return "UNKNOWN"
    return f"{_age((now - datetime.fromisoformat(value)).total_seconds())} ago"


def backup_line(backups: dict[str, Any] | None, now: datetime) -> str:
    if not backups:
        return "Backups: not reported by the worker"
    if backups.get("error"):
        return "Backups: CANNOT BE CHECKED (the worker cannot read the backup directory)"
    b_new, drill = backups.get("newest"), backups.get("drill")
    return (
        "Backups: "
        + (
            "NONE FOUND"
            if not b_new
            else f"newest {_ts(b_new['created_at'], now)}"
            + (" (INCOMPLETE)" if b_new["problems"] else "")
        )
        + " · restore drill "
        + (
            "never recorded"
            if not drill
            else f"{'PASS' if drill['passed'] else 'FAILED'} {drill['drilled_at'][:10]}"
        )
    )


async def health_lines(conn: AsyncConnection, now: datetime) -> list[str]:
    """Mode, worker, incidents, failed jobs, the HFM bridge, backups, pending approvals and what
    needs attention: the /status answer."""
    banner = await queries.authority_banner(conn)
    links = await connections(conn, now)
    open_incidents = list(
        await conn.execute(select(incidents.c.severity).where(incidents.c.status != "RESOLVED"))
    )
    critical = sum(1 for i in open_incidents if i.severity == "CRITICAL")
    failed = await conn.scalar(
        select(func.count())
        .select_from(jobs)
        .where(jobs.c.status == "DEAD", jobs.c.finished_at >= now - DAY)
    )
    waiting = await queries.intent_list(conn, statuses=("PROPOSED",))
    live = "ENABLED" if banner["live_enabled"] else "DISABLED"
    worker = links["worker_age_s"]
    lines = [
        f"Mode {banner['mode']} · live orders {live}",
        "Worker: "
        + ("NO HEARTBEAT" if worker is None else f"beat {_age(worker)} ago")
        + f" · open incidents {len(open_incidents)} ({critical} critical)"
        + f" · failed jobs (24 h) {failed or 0}",
    ]
    for b in links["bridges"]:
        state = (
            f"report {_age(b['age_s'])} ago ({b['account_mode']})"
            if b["fresh"]
            else f"NOT REPORTING for {_age(b['age_s'])}: HFM orders wait"
        )
        lines.append(f"HFM bridge v{b['version']}: {state}")
    lines.append(backup_line(links["backups"], now))
    lines.append(f"Proposals waiting for you: {len(waiting)}")
    rest = [text for kind, text in await attention_items(conn, now) if kind not in SHOWN_ABOVE]
    if rest:
        lines.append("Needs attention: " + "; ".join(rest))
    return lines


async def _accounts(conn: AsyncConnection, now: datetime) -> list[str]:
    out: list[str] = []
    for v in await queries.portfolio(conn, now):
        a = v.account
        env = environment_short(a.kind, a.broker_adapter)
        ccy = a.base_currency
        out.append(
            f"• {a.alias} [{env}] {ccy}: equity {money(v.equity)} · cash {money(v.cash)} · "
            f"unrealised {money(v.unrealized)} · realised today {money(v.realized_today)} · "
            f"reconciliation {a.reconciliation_status}"
        )
        used = [
            f"{x['name'].lower()} {money(x['value'])} of {money(x['limit'])}"
            + ("%" if x["unit"] == "%" else "")
            for x in v.limits
            if x["unit"] != "trades"
        ]
        if used:
            out.append("   limits: " + " · ".join(used))
        held = [p for p in v.positions if p["quantity"]]
        for p in held[:MAX_POSITIONS]:
            out.append(
                f"   {p['key']} {p['quantity'].normalize():f} · stop {p['protection']} · "
                f"unrealised {money(p['unrealized'])}"
            )
        if len(held) > MAX_POSITIONS:
            out.append(f"   … and {len(held) - MAX_POSITIONS} more on the portal")
        if v.unknown_orders:
            out.append(f"   {v.unknown_orders} order(s) in an UNKNOWN state: see Orders")
    return out or ["• no accounts"]


async def _closed(conn: AsyncConnection, now: datetime) -> list[str]:
    rows = list(
        await conn.execute(
            select(
                post_trade_reviews.c.realized_pnl,
                trade_intents.c.trade_code,
                accounts.c.alias,
                accounts.c.kind,
                accounts.c.broker_adapter,
                accounts.c.base_currency,
            )
            .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
            .join(accounts, accounts.c.id == trade_intents.c.account_id)
            .where(post_trade_reviews.c.created_at >= now - DAY)
            .order_by(desc(post_trade_reviews.c.created_at))
            .limit(6)
        )
    )
    if not rows:
        return ["Closed in the last 24 h: none"]
    out = ["Closed in the last 24 h (realised P&L after fees):"]
    for r in rows:
        env = environment_short(r.kind, r.broker_adapter)
        out.append(
            f"   {r.trade_code} on {r.alias} [{env}]: {money(r.realized_pnl)} {r.base_currency}"
        )
    return out


def strategy_line(status: dict[str, int]) -> str:
    on = status["switched_on"]
    if status["qualified"] == 0:
        return f"Strategies: NO QUALIFIED STRATEGY · rules switched on: {on}"
    return f"Strategies: {status['qualified']} qualified · rules switched on: {on}"


async def daily_report(conn: AsyncConnection, now: datetime) -> str:
    opened = await conn.scalar(
        select(func.count()).select_from(incidents).where(incidents.c.opened_at >= now - DAY)
    )
    lines = [f"COSMICTRADING daily report · {now:%Y-%m-%d %H:%M} UTC"]
    lines += await health_lines(conn, now)
    lines.append(f"Incidents opened in the last 24 h: {opened or 0}")
    lines.append("Accounts (each in its own currency; never added together):")
    lines += await _accounts(conn, now)
    lines += await _closed(conn, now)
    lines.append(strategy_line(await strategy_status(conn)))
    return "\n".join(lines)[:3900]  # Telegram's limit is 4096 characters


async def sent_today(conn: AsyncConnection, now: datetime) -> bool:
    n = await conn.scalar(
        select(func.count())
        .select_from(notifications)
        .where(
            notifications.c.kind == KIND,
            notifications.c.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
        )
    )
    return bool(n)


def report_payload(text: str) -> dict[str, Any]:
    return {"text": text}
