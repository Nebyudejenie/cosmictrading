"""The owner's short daily report and the Telegram health summary (directive Phases 8 and 10).

Every account is reported on its own line, in its own currency and environment (PAPER,
TESTNET, DEMO, LIVE): results are never added across accounts, so paper or test money is never
mixed with real money. Realised and unrealised P&L stay apart. A value the system cannot verify
is shown as UNKNOWN, never guessed. Plain text, no secrets, aliases only.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.db.schema import incidents, jobs, notifications, worker_heartbeats
from cosmic.db.schema_execution import post_trade_reviews, trade_intents
from cosmic.db.schema_trading import accounts
from cosmic.trading import queries
from cosmic.trading.modes import environment_of

DAY = timedelta(hours=24)
KIND = "daily_report"
REPORT_HOUR_UTC = 5  # 08:00 in Addis Ababa
MAX_POSITIONS = 4  # per account, to keep the message short


def _m(value: Decimal | None, places: str = "0.01") -> str:
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


async def health_lines(conn: AsyncConnection, now: datetime) -> list[str]:
    """Mode, worker, incidents, failed jobs, backups, pending approvals: the /status answer."""
    banner = await queries.authority_banner(conn)
    beat = (
        await conn.execute(
            select(worker_heartbeats)
            .where(worker_heartbeats.c.process == "worker")
            .order_by(desc(worker_heartbeats.c.last_beat_at))
            .limit(1)
        )
    ).first()
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
    lines = [
        f"Mode {banner['mode']} · live orders {live}",
        "Worker: "
        + (
            "NO HEARTBEAT"
            if beat is None
            else f"beat {_age((now - beat.last_beat_at).total_seconds())} ago"
        )
        + f" · open incidents {len(open_incidents)} ({critical} critical)"
        + f" · failed jobs (24 h) {failed or 0}",
    ]
    backups = None if beat is None else (beat.info or {}).get("backups")
    if backups:
        b_new, drill = backups.get("newest"), backups.get("drill")
        lines.append(
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
    else:
        lines.append("Backups: not reported by the worker")
    lines.append(f"Proposals waiting for you: {len(waiting)}")
    return lines


async def _accounts(conn: AsyncConnection, now: datetime) -> list[str]:
    out: list[str] = []
    for v in await queries.portfolio(conn, now):
        a = v.account
        env = environment_of(a.kind, a.broker_adapter).code
        ccy = a.base_currency
        out.append(
            f"• {a.alias} [{env}] {ccy}: equity {_m(v.equity)} · cash {_m(v.cash)} · "
            f"unrealised {_m(v.unrealized)} · realised today {_m(v.realized_today)} · "
            f"reconciliation {a.reconciliation_status}"
        )
        used = [
            f"{x['name'].lower()} {_m(x['value'])} of {_m(x['limit'])}"
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
                f"unrealised {_m(p['unrealized'])}"
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
        env = environment_of(r.kind, r.broker_adapter).code
        out.append(
            f"   {r.trade_code} on {r.alias} [{env}]: {_m(r.realized_pnl)} {r.base_currency}"
        )
    return out


async def _strategies(conn: AsyncConnection) -> str:
    # Imported here: the bot answers /status without loading the research library.
    from cosmic.research.library.engine import BINANCE_LIKE, COINBASE_LIKE
    from cosmic.research.library.store import active, latest_run, run_evaluations

    qualified = 0
    for costs in (BINANCE_LIKE, COINBASE_LIKE):
        run = await latest_run(conn, costs.name)
        if run is not None:
            qualified += sum(
                1 for e in await run_evaluations(conn, run.id) if e.tier == "QUALIFIED"
            )
    on = len(await active(conn))
    if qualified == 0:
        return f"Strategies: NO QUALIFIED STRATEGY · rules switched on: {on}"
    return f"Strategies: {qualified} qualified · rules switched on: {on}"


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
    lines.append(await _strategies(conn))
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
