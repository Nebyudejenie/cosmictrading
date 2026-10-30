"""Read models for the portal, API and Telegram (no business rules; no writes)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema import incidents, jobs, worker_heartbeats
from cosmic.db.schema_execution import (
    approval_packets,
    approvals,
    fills,
    order_events,
    orders,
    post_trade_reviews,
    reconciliation_items,
    reconciliation_runs,
    trade_intents,
)
from cosmic.db.schema_market import instruments
from cosmic.db.schema_trading import accounts, equity_snapshots, positions, risk_check_runs
from cosmic.market import data as md
from cosmic.trading import ledger
from cosmic.trading.accounts import Account, list_accounts
from cosmic.trading.authority import AuthorityState, get_state, live_preconditions
from cosmic.trading.context_builder import account_state
from cosmic.trading.modes import account_ref, environment_of
from cosmic.trading.policy import active_policy

QUEUE_STATUSES = ("PROPOSED", "REQUIRES_REAPPROVAL", "APPROVED", "EXECUTING")


async def authority_banner(conn: AsyncConnection) -> dict[str, Any]:
    state: AuthorityState = await get_state(conn)
    # No settings here: enforce_cap keeps a live mode only while COSMIC_LIVE_VENUES names a
    # venue, so the venue lock is held at startup.
    unmet = await live_preconditions(conn, live_venues=None)
    return {
        "mode": state.mode.value,
        "live_enabled": state.live_enabled and not unmet,
        # Every precondition met but the mode is not a live one: say so, never a bare dash.
        "live_reason": unmet[0]
        if unmet
        else ("" if state.live_enabled else f"the mode is {state.mode.value}, not a live mode"),
        "version": state.version,
        "changed_at": state.changed_at,
        "reason": state.reason,
        "mode_before_stop": state.mode_before_stop.value if state.mode_before_stop else None,
    }


# Every list names each row's account with its environment (DIR-02).
ACCOUNT_COLUMNS = (
    accounts.c.alias,
    accounts.c.kind.label("account_kind"),
    accounts.c.broker_adapter,
)


def with_account(row: Any) -> dict[str, Any]:
    """A row joined with ACCOUNT_COLUMNS, plus how the account is named everywhere."""
    d = dict(row._mapping)
    d["account_ref"] = account_ref(d["alias"], d["account_kind"], d["broker_adapter"])
    d["environment"] = environment_of(d["account_kind"], d["broker_adapter"]).code
    return d


async def intent_list(
    conn: AsyncConnection, *, statuses: tuple[str, ...] | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    q = (
        select(trade_intents, instruments.c.canonical_key, *ACCOUNT_COLUMNS)
        .join(instruments, instruments.c.id == trade_intents.c.instrument_id)
        .join(accounts, accounts.c.id == trade_intents.c.account_id)
    )
    if statuses:
        q = q.where(trade_intents.c.status.in_(statuses))
    rows = await conn.execute(q.order_by(desc(trade_intents.c.created_at)).limit(limit))
    return [with_account(r) for r in rows]


@dataclass
class IntentDetail:
    intent: Any
    instrument_key: str
    packets: list[Any]
    approvals: list[Any]
    risk_runs: list[Any]
    orders: list[Any]
    review: Any = None
    account_ref: str = ""  # "alias [SIMULATED · DEMO]", with or without a packet
    environment: str = ""


async def intent_detail(conn: AsyncConnection, intent_id: uuid.UUID) -> IntentDetail | None:
    intent = (
        await conn.execute(select(trade_intents).where(trade_intents.c.id == intent_id))
    ).first()
    if intent is None:
        return None
    key = (
        await conn.execute(
            select(instruments.c.canonical_key).where(instruments.c.id == intent.instrument_id)
        )
    ).scalar_one()
    packets = list(
        await conn.execute(
            select(approval_packets)
            .where(approval_packets.c.intent_id == intent_id)
            .order_by(desc(approval_packets.c.version))
        )
    )
    apps = list(
        await conn.execute(
            select(approvals)
            .where(approvals.c.intent_id == intent_id)
            .order_by(desc(approvals.c.decided_at))
        )
    )
    runs = list(
        await conn.execute(
            select(risk_check_runs)
            .where(risk_check_runs.c.intent_id == intent_id)
            .order_by(desc(risk_check_runs.c.created_at))
        )
    )
    ords = list(
        await conn.execute(
            select(orders).where(orders.c.intent_id == intent_id).order_by(orders.c.created_at)
        )
    )
    review = (
        await conn.execute(
            select(post_trade_reviews).where(post_trade_reviews.c.intent_id == intent_id)
        )
    ).first()
    acct = with_account(
        (
            await conn.execute(select(*ACCOUNT_COLUMNS).where(accounts.c.id == intent.account_id))
        ).one()
    )
    return IntentDetail(
        intent, key, packets, apps, runs, ords, review, acct["account_ref"], acct["environment"]
    )


async def order_list(conn: AsyncConnection, limit: int = 200) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(orders, instruments.c.canonical_key, trade_intents.c.trade_code, *ACCOUNT_COLUMNS)
        .join(instruments, instruments.c.id == orders.c.instrument_id)
        .join(trade_intents, trade_intents.c.id == orders.c.intent_id)
        .join(accounts, accounts.c.id == orders.c.account_id)
        .order_by(desc(orders.c.created_at))
        .limit(limit)
    )
    return [with_account(r) for r in rows]


async def order_detail(conn: AsyncConnection, order_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        await conn.execute(
            select(
                orders, instruments.c.canonical_key, trade_intents.c.trade_code, *ACCOUNT_COLUMNS
            )
            .join(instruments, instruments.c.id == orders.c.instrument_id)
            .join(trade_intents, trade_intents.c.id == orders.c.intent_id)
            .join(accounts, accounts.c.id == orders.c.account_id)
            .where(orders.c.id == order_id)
        )
    ).first()
    if row is None:
        return None
    evs = list(
        await conn.execute(
            select(order_events)
            .where(order_events.c.order_id == order_id)
            .order_by(order_events.c.seq)
        )
    )
    fls = list(
        await conn.execute(
            select(fills).where(fills.c.order_id == order_id).order_by(fills.c.filled_at)
        )
    )
    acct = with_account(row)
    return {
        "order": row,
        "account_ref": acct["account_ref"],
        "environment": acct["environment"],
        "events": evs,
        "fills": fls,
    }


@dataclass
class AccountView:
    account: Account
    cash: Decimal
    equity: Decimal | None
    unrealized: Decimal | None
    realized_today: Decimal | None
    positions: list[dict[str, Any]]
    open_orders: int
    unknown_orders: int
    last_run: Any
    last_run_items: list[Any]
    limits: list[dict[str, Any]]
    drawdown_pct: Decimal | None


async def portfolio(conn: AsyncConnection, now: datetime) -> list[AccountView]:
    views: list[AccountView] = []
    try:
        _, policy = await active_policy(conn)
    except Exception:
        policy = None
    for acct in await list_accounts(conn):
        state = await account_state(conn, acct, now)
        pos_rows = list(
            await conn.execute(
                select(positions, instruments.c.canonical_key)
                .join(instruments, instruments.c.id == positions.c.instrument_id)
                .where(positions.c.account_id == acct.id)
            )
        )
        pos_list = []
        for p in pos_rows:
            q = await md.latest_quote(conn, p.instrument_id, now)
            mark = q.mid if q.label is DataLabel.DELAYED else None
            pos_list.append(
                {
                    "key": p.canonical_key,
                    "quantity": p.quantity,
                    "avg_cost": p.avg_cost,
                    "mark": mark,
                    "mark_label": q.label.value,
                    "mark_ts": q.source_ts,
                    "value": None if mark is None else p.quantity * mark,
                    "unrealized": None
                    if mark is None or p.avg_cost is None or p.quantity == 0
                    else p.quantity * (mark - p.avg_cost),
                    "realized": p.realized_pnl,
                    "protection": p.protection_state,
                }
            )
        run = (
            await conn.execute(
                select(reconciliation_runs)
                .where(reconciliation_runs.c.account_id == acct.id)
                .order_by(desc(reconciliation_runs.c.started_at))
                .limit(1)
            )
        ).first()
        items = (
            []
            if run is None
            else list(
                await conn.execute(
                    select(reconciliation_items).where(reconciliation_items.c.run_id == run.id)
                )
            )
        )
        limits: list[dict[str, Any]] = []
        dd = None
        if state.equity is not None and state.high_water_mark:
            dd = (state.high_water_mark - state.equity) / state.high_water_mark * 100
        if policy is not None:
            day_loss = (
                None
                if state.unrealized_pnl is None or state.realized_pnl_today is None
                else max(ZERO, -(state.realized_pnl_today + state.unrealized_pnl))
            )
            limits = [
                {
                    "name": "Daily loss",
                    "value": day_loss,
                    "limit": policy.max_daily_loss,
                    "unit": acct.base_currency,
                },
                {"name": "Drawdown", "value": dd, "limit": policy.max_drawdown_pct, "unit": "%"},
                {
                    "name": "Loss streak",
                    "value": state.loss_streak,
                    "limit": policy.max_loss_streak,
                    "unit": "trades",
                },
            ]
        views.append(
            AccountView(
                account=acct,
                cash=state.cash_available or ZERO,
                equity=state.equity,
                unrealized=state.unrealized_pnl,
                realized_today=state.realized_pnl_today,
                positions=pos_list,
                open_orders=len(state.open_orders or ()),
                unknown_orders=state.unknown_orders or 0,
                last_run=run,
                last_run_items=items,
                limits=limits,
                drawdown_pct=dd,
            )
        )
    return views


async def ops_overview(conn: AsyncConnection, now: datetime) -> dict[str, Any]:
    beats = list(
        await conn.execute(
            select(worker_heartbeats).order_by(desc(worker_heartbeats.c.last_beat_at))
        )
    )
    queue = list(
        await conn.execute(
            select(
                jobs.c.kind,
                jobs.c.status,
                func.count().label("n"),
                func.min(jobs.c.run_at).label("oldest"),
            )
            .where(jobs.c.status.in_(["QUEUED", "RUNNING", "DEAD"]))
            .group_by(jobs.c.kind, jobs.c.status)
            .order_by(jobs.c.kind)
        )
    )
    open_incidents = list(
        await conn.execute(
            select(incidents)
            .where(incidents.c.status != "RESOLVED")
            .order_by(desc(incidents.c.opened_at))
            .limit(50)
        )
    )
    return {
        "heartbeats": [
            {
                "worker_id": b.worker_id,
                "age": (now - b.last_beat_at).total_seconds(),
                "info": b.info,
            }
            for b in beats
        ],
        "queue": [
            {
                "kind": q.kind,
                "status": q.status,
                "count": q.n,
                "oldest_age": (now - q.oldest).total_seconds() if q.oldest else None,
            }
            for q in queue
        ],
        "incidents": open_incidents,
    }


async def reviews(conn: AsyncConnection, limit: int = 50) -> list[Any]:
    return list(
        await conn.execute(
            select(post_trade_reviews, trade_intents.c.trade_code, *ACCOUNT_COLUMNS)
            .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
            .join(accounts, accounts.c.id == trade_intents.c.account_id)
            .order_by(desc(post_trade_reviews.c.created_at))
            .limit(limit)
        )
    )


async def equity_history(
    conn: AsyncConnection, account_id: uuid.UUID, since: datetime, *, points: int = 240
) -> list[tuple[datetime, Decimal]]:
    """The account's equity samples since ``since`` (every 5 minutes), thinned evenly to at
    most ``points`` for a chart; the latest sample is always kept."""
    rows = [
        (r.taken_at, r.equity)
        for r in await conn.execute(
            select(equity_snapshots.c.taken_at, equity_snapshots.c.equity)
            .where(
                equity_snapshots.c.account_id == account_id,
                equity_snapshots.c.taken_at >= since,
            )
            .order_by(equity_snapshots.c.taken_at)
        )
    ]
    if len(rows) <= points:
        return rows
    step = len(rows) / points
    thinned = [rows[int(i * step)] for i in range(points - 1)]
    return [*thinned, rows[-1]]


async def cash_of(conn: AsyncConnection, account: Account) -> Decimal:
    return await ledger.book_balance(conn, account.id, "CASH", account.base_currency)
