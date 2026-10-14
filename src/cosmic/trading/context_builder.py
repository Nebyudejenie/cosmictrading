"""Build an immutable RiskContext from the database (read inside the caller's transaction)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema import incidents
from cosmic.db.schema_execution import fills, post_trade_reviews, trade_intents
from cosmic.db.schema_execution import orders as orders_t
from cosmic.db.schema_market import instruments
from cosmic.db.schema_trading import equity_snapshots, positions
from cosmic.market import data as md
from cosmic.trading import ledger
from cosmic.trading.accounts import Account
from cosmic.trading.modes import Mode
from cosmic.trading.orders import TERMINAL, OrderStatus, count_unknown, list_orders
from cosmic.trading.risk.context import (
    AccountState,
    AuthorizationState,
    InstrumentState,
    MarketState,
    OpenOrderState,
    OrderRequest,
    PositionState,
    RiskContext,
    Stage,
)

NON_TERMINAL = frozenset(OrderStatus) - TERMINAL
MARK_MAX_AGE_SECONDS = 300


async def mark_price(conn: AsyncConnection, instrument_id: object, now: datetime) -> Decimal | None:
    """Fresh mid price for valuation, or None (unknown must not be treated as zero)."""
    q = await md.latest_quote(conn, instrument_id, now)  # type: ignore[arg-type]
    if q.label is not DataLabel.DELAYED or q.mid is None:
        return None
    if q.age_seconds is None or q.age_seconds > MARK_MAX_AGE_SECONDS:
        return None
    return q.mid


async def account_state(
    conn: AsyncConnection,
    account: Account,
    now: datetime,
    *,
    instrument_id: object | None = None,
    side: str | None = None,
) -> AccountState:
    cash = await ledger.book_balance(conn, account.id, "CASH", account.base_currency)
    pos_rows = list(
        await conn.execute(
            select(positions, instruments.c.canonical_key)
            .join(instruments, instruments.c.id == positions.c.instrument_id)
            .where(positions.c.account_id == account.id, positions.c.quantity > 0)
        )
    )
    pos_states: list[PositionState] = []
    equity: Decimal | None = cash
    unrealized: Decimal | None = ZERO
    for p in pos_rows:
        mark = await mark_price(conn, p.instrument_id, now)
        pos_states.append(PositionState(p.canonical_key, p.quantity, mark))
        if mark is None:
            equity = None
            unrealized = None
        else:
            if equity is not None:
                equity += p.quantity * mark
            if unrealized is not None and p.avg_cost is not None:
                unrealized += p.quantity * (mark - p.avg_cost)
    open_rows = await list_orders(conn, account_id=account.id, statuses=NON_TERMINAL)
    keys = {
        r.id: r.canonical_key
        for r in await conn.execute(select(instruments.c.id, instruments.c.canonical_key))
    }
    open_states = tuple(
        OpenOrderState(
            keys[o.instrument_id],
            o.side,
            o.remaining,
            o.limit_price,
            o.reduce_only,
            o.purpose,
            str(o.intent_id),
        )
        for o in open_rows
    )
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    realized_today = await conn.scalar(
        select(func.coalesce(func.sum(fills.c.realized_pnl), 0))
        .join(orders_t, orders_t.c.id == fills.c.order_id)
        .where(orders_t.c.account_id == account.id, fills.c.filled_at >= day_start)
    )
    hwm = await conn.scalar(
        select(func.max(equity_snapshots.c.equity)).where(
            equity_snapshots.c.account_id == account.id
        )
    )
    high_water = (
        None if equity is None else max(Decimal(hwm) if hwm is not None else equity, equity)
    )
    reviews = [
        r.realized_pnl
        for r in await conn.execute(
            select(post_trade_reviews.c.realized_pnl)
            .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
            .where(trade_intents.c.account_id == account.id)
            .order_by(desc(post_trade_reviews.c.created_at))
            .limit(50)
        )
    ]
    streak = 0
    for pnl in reviews:
        if pnl is not None and pnl < 0:
            streak += 1
        else:
            break
    pending = 0
    if instrument_id is not None and side is not None:
        pending = int(
            await conn.scalar(
                select(func.count())
                .select_from(trade_intents)
                .where(
                    trade_intents.c.account_id == account.id,
                    trade_intents.c.instrument_id == instrument_id,
                    trade_intents.c.side == side,
                    trade_intents.c.status.in_(
                        ["PROPOSED", "APPROVED", "EXECUTING", "REQUIRES_REAPPROVAL"]
                    ),
                )
            )
            or 0
        )
    return AccountState(
        account_id=str(account.id),
        alias=account.alias,
        kind=account.kind,
        status=account.status,
        venue_key=account.venue_key,
        reconciliation_status=account.reconciliation_status,
        last_reconciled_at=account.last_reconciled_at,
        cash_available=cash,
        positions=tuple(pos_states),
        open_orders=open_states,
        unknown_orders=await count_unknown(conn, account.id),
        equity=equity,
        high_water_mark=high_water,
        realized_pnl_today=Decimal(realized_today or 0),
        unrealized_pnl=unrealized,
        loss_streak=streak,
        active_intents_same_instrument_side=pending,
    )


async def market_state(
    conn: AsyncConnection, inst: md.InstrumentView, now: datetime
) -> MarketState:
    q = await md.latest_quote(conn, inst.id, now)
    dev = await md.cross_source_deviation(conn, inst.pair_key, now)
    venue_status, venue_label = await md.latest_venue_status(conn, inst.venue_key, now)
    return MarketState(
        bid=q.bid,
        ask=q.ask,
        last=q.last,
        quote_ts=q.source_ts,
        quote_label=q.label,
        deviation_bps=dev.bps,
        deviation_label=dev.label,
        venue_status=venue_status,
        venue_status_label=venue_label,
    )


def instrument_state(inst: md.InstrumentView) -> InstrumentState:
    return InstrumentState(
        canonical_key=inst.canonical_key,
        venue_key=inst.venue_key,
        quote_currency=inst.quote,
        status=inst.status,
        tradable=inst.tradable,
        tick_size=inst.tick_size,
        lot_size=inst.lot_size,
        min_qty=inst.min_qty,
        max_qty=inst.max_qty,
        min_notional=inst.min_notional,
    )


async def critical_categories(conn: AsyncConnection) -> frozenset[str]:
    rows = await conn.execute(
        select(incidents.c.category)
        .where(incidents.c.status != "RESOLVED", incidents.c.severity == "CRITICAL")
        .distinct()
    )
    return frozenset(r.category for r in rows)


async def build_context(
    conn: AsyncConnection,
    *,
    account: Account,
    inst: md.InstrumentView,
    request: OrderRequest,
    stage: Stage,
    mode: Mode,
    policy_version: int,
    now: datetime,
    authorization: AuthorizationState | None = None,
) -> RiskContext:
    return RiskContext(
        now=now,
        stage=stage,
        mode=mode,
        request=request,
        account=await account_state(conn, account, now, instrument_id=inst.id, side=request.side),
        instrument=instrument_state(inst),
        market=await market_state(conn, inst, now),
        policy_version=policy_version,
        critical_incident_categories=await critical_categories(conn),
        authorization=authorization,
    )


def within(seconds: int) -> timedelta:
    return timedelta(seconds=seconds)
