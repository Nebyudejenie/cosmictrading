"""The robot cockpit on the home page (S82, the P0 demo directive's admin dashboard): for each
running HFM demo robot, in one place:
- the account type (DEMO, unmistakably), the broker and MT5 connection, balance and equity;
- the robot's status;
- today's and the session's net result, the profit goal's progress and the daily loss limit's;
- its open trades, their stop-loss and take-profit, the floating result and the total money they
  can still lose at their stops;
- today's trades, the win rate and profit factor;
- the AI's connection, model and last inference;
- with START DEMO, PAUSE, RESUME and EMERGENCY STOP beside it.

Read-only: every figure comes from the records the robots, the bridge and the AI keep."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.money import ZERO
from cosmic.db.schema_ai import ai_model_approvals
from cosmic.db.schema_execution import orders, trade_intents
from cosmic.db.schema_gold import ai_gold_opinions
from cosmic.db.schema_trading import bridge_state
from cosmic.gold import controls, hfm_demo, sim
from cosmic.market import data as md
from cosmic.reporting import gold_training
from cosmic.trading import mandates
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.mt5_bridge import BY_ADAPTER

FRESH = timedelta(seconds=60)
AI_FRESH = timedelta(minutes=15)


@dataclass(frozen=True)
class OpenTrade:
    side: str
    ounces: Decimal
    entry: Decimal | None
    stop: Decimal | None
    take_profit: Decimal | None
    floating: Decimal | None
    at_risk: Decimal | None  # what it can still lose at its stop (≥ 0)


@dataclass
class Robot:
    mandate: mandates.Mandate
    account_alias: str
    demo: bool
    broker_ok: bool
    mt5_ok: bool
    balance: Decimal | None
    equity: Decimal | None
    rules: controls.Controls | None
    outcome: controls.Results | None
    open: list[OpenTrade] = field(default_factory=list)
    trades_today: int = 0

    @property
    def status(self) -> str:
        return self.rules.status if self.rules is not None else "RUNNING"

    @property
    def at_risk(self) -> Decimal:
        return sum((t.at_risk or ZERO for t in self.open), ZERO)

    @property
    def goal(self) -> tuple[str, Decimal, Decimal] | None:
        """The nearest configured goal: (which, target, value now)."""
        c, r = self.rules, self.outcome
        if c is None or r is None:
            return None
        extra = r.floating if c.measure == controls.WITH_FLOATING else ZERO
        for label, target, value in (
            ("daily", c.daily_target, r.today),
            ("session", c.session_target, r.session),
            ("long-term", c.long_target, r.total),
        ):
            if target is not None:
                return label, target, value + extra
        return None

    @property
    def loss_used(self) -> Decimal:
        """How much of today's loss limit is used, in %."""
        if self.outcome is None or not self.mandate.daily_loss_limit:
            return ZERO
        lost = max(-self.outcome.today, ZERO)
        return (lost / self.mandate.daily_loss_limit * 100).quantize(Decimal("0.1"))


@dataclass
class Panel:
    robots: list[Robot]
    win_rate: Decimal | None
    profit_factor: Decimal | None
    closed: int
    ai_model: str | None
    ai_last: Any | None
    ai_fresh: bool


async def _open_trades(
    conn: AsyncConnection, held: list[tuple[uuid.UUID, Decimal]], bid: Decimal, ask: Decimal
) -> list[OpenTrade]:
    out = []
    for intent_id, qty in held:
        entry = (
            await conn.execute(
                select(orders.c.avg_fill_price, orders.c.side).where(
                    orders.c.intent_id == intent_id, orders.c.purpose == "ENTRY"
                )
            )
        ).first()
        stop = await conn.scalar(
            select(orders.c.stop_price)
            .where(
                orders.c.intent_id == intent_id,
                orders.c.purpose == "STOP_LOSS",
                orders.c.status.in_(["ACKNOWLEDGED", "SUBMITTED", "PARTIALLY_FILLED"]),
            )
            .order_by(desc(orders.c.created_at))
            .limit(1)
        )
        plan = await conn.scalar(
            select(trade_intents.c.management_plan).where(trade_intents.c.id == intent_id)
        )
        target = (plan or {}).get("take_profit_price")
        side = entry.side if entry is not None else "BUY"
        fill = entry.avg_fill_price if entry is not None else None
        floating = at_risk = None
        if fill is not None and bid and ask:
            floating = (bid - fill) * qty if side == "BUY" else (fill - ask) * qty
        if fill is not None and stop is not None:
            at_risk = max((fill - stop) * qty if side == "BUY" else (stop - fill) * qty, ZERO)
        out.append(
            OpenTrade(
                side,
                qty,
                fill,
                stop,
                None if target is None else Decimal(str(target)),
                floating,
                at_risk,
            )
        )
    return out


async def build(conn: AsyncConnection, now: datetime) -> Panel:
    robots = []
    last = await sim.last_price(conn)
    bid, ask = (last.bid, last.ask) if last is not None else (ZERO, ZERO)
    inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
    for m in await hfm_demo.gold_mandates(conn, now):
        account = await get_account(conn, account_id=m.account_id)
        bridge = BY_ADAPTER.get(account.broker_adapter or "")
        row = (
            (
                await conn.execute(
                    select(bridge_state.c.received_at, bridge_state.c.snapshot).where(
                        bridge_state.c.bridge == bridge.name
                    )
                )
            ).first()
            if bridge is not None
            else None
        )
        snap = (row.snapshot or {}) if row is not None else {}
        acct = snap.get("account") or {}
        terminal = snap.get("terminal") or {}
        held = await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)})
        ctl = await controls.get(conn, m.id)
        res = await controls.results(conn, m, ctl, now, held, bid, ask) if ctl else None
        trades_today = await conn.scalar(
            select(func.count())
            .select_from(trade_intents)
            .where(
                trade_intents.c.account_id == account.id,
                trade_intents.c.effect.in_(["OPEN", "INCREASE"]),
                trade_intents.c.status.in_(["EXECUTING", "EXECUTED"]),
                trade_intents.c.created_at >= mandates.day_start(now),
                trade_intents.c.details["mandate_id"].astext == str(m.id),
            )
        )
        robots.append(
            Robot(
                mandate=m,
                account_alias=account.alias,
                demo=acct.get("trade_mode") == "DEMO" and account.kind == "SANDBOX",
                broker_ok=row is not None and now - row.received_at <= FRESH,
                mt5_ok=bool(terminal.get("connected")) and bool(terminal.get("trade_allowed")),
                balance=Decimal(str(acct["balance"])) if acct.get("balance") is not None else None,
                equity=Decimal(str(acct["equity"])) if acct.get("equity") is not None else None,
                rules=ctl,
                outcome=res,
                open=await _open_trades(conn, held, bid, ask),
                trades_today=int(trades_today or 0),
            )
        )
    report = gold_training.report(await gold_training.trades(conn), [])
    model = await conn.scalar(
        select(ai_model_approvals.c.model_id)
        .where(ai_model_approvals.c.revoked_at.is_(None))
        .order_by(desc(ai_model_approvals.c.approved_at))
        .limit(1)
    )
    ai_last = (
        await conn.execute(select(ai_gold_opinions).order_by(desc(ai_gold_opinions.c.at)).limit(1))
    ).first()
    return Panel(
        robots,
        report.win_rate,
        report.profit_factor,
        report.count,
        model,
        ai_last,
        ai_last is not None and now - ai_last.at <= AI_FRESH,
    )
