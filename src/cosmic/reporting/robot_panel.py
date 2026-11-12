"""The robot cockpit on the home page (S82, the P0 demo directive's admin dashboard): for each
running HFM demo robot, in one place:
- the account type (DEMO, unmistakably), the broker and MT5 connection, balance and equity;
- the robot's status;
- today's and the session's net result, the profit goal's progress and the daily loss limit's;
- its open trades, their stop-loss and take-profit, the floating result and the total money they
  can still lose at their stops;
- today's trades, the win rate and profit factor;
- the AI's connection, model and last inference;
- with START DEMO, PAUSE, RESUME and EMERGENCY STOP beside it;
- since S85 (the owner's "one clear screen"): the market's direction and the model's measured
  probability, the robot's last decision, the approval waiting for its next trade and what
  such a trade can lose and win, the day's remaining risk budget, and predicted against
  happened over every scored prediction.

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
from cosmic.db.schema_execution import order_events, orders, trade_intents
from cosmic.db.schema_gold import ai_gold_opinions, gold_hfm_decisions
from cosmic.db.schema_trading import bridge_fast, bridge_state
from cosmic.gold import controls, hfm_demo, predictions, probability, sim
from cosmic.market import data as md
from cosmic.reporting import gold_training
from cosmic.trading import mandates
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.mt5_bridge import BY_ADAPTER, bridge_version

FRESH = timedelta(seconds=60)
AI_FRESH = timedelta(minutes=15)
ACCURACY_DAYS = timedelta(days=30)  # predicted against happened: over this long


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
    ticket: Any | None = None  # the owner's approval waiting for its next trade (S85)
    last_decision: Any | None = None

    @property
    def m1(self) -> bool:
        """The 1-minute rule (S85): its trades open by the probability rules or an approval."""
        return self.mandate.strategy_key == mandates.GOLD_M1

    @property
    def budget(self) -> Decimal:
        """What today may still lose before the day's limit."""
        lost = max(-self.outcome.today, ZERO) if self.outcome is not None else ZERO
        return max(self.mandate.daily_loss_limit - lost, ZERO)

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


@dataclass(frozen=True)
class Speed:
    """The owner's rule (2026-11-12, S87): an order leaves for HFM within a second of its
    prediction, or not at all. What the path is like now, and what the last order measured."""

    limit_ms: int
    problem: str | None  # why an order could not leave within the second now; None: it can
    round_trip_ms: int | None  # the server to the bridge and back, as the bridge measures it
    last_ms: int | None = None  # the last order: from its prediction until MT5 was asked
    last_answer_ms: int | None = None  # and how long HFM then took to answer
    last_at: datetime | None = None


async def _speed(conn: AsyncConnection, now: datetime) -> Speed:
    snap = await conn.scalar(
        select(bridge_state.c.snapshot).where(bridge_state.c.bridge == sim.BRIDGE)
    )
    problem = await hfm_demo.fast_path_problem(conn, now, bridge_version(snap or {}))
    info = await conn.scalar(select(bridge_fast.c.info).where(bridge_fast.c.bridge == sim.BRIDGE))
    rtt = (info or {}).get("rtt_ms")
    last = (
        await conn.execute(
            select(order_events.c.detail, order_events.c.occurred_at)
            .join(orders, orders.c.id == order_events.c.order_id)
            .where(orders.c.purpose == "ENTRY", order_events.c.detail.has_key("speed_total_ms"))
            .order_by(desc(order_events.c.occurred_at))
            .limit(1)
        )
    ).first()
    limit = int(hfm_demo.SEND_WITHIN.total_seconds() * 1000)
    if last is None:
        return Speed(limit, problem, rtt if isinstance(rtt, int) else None)
    answer = last.detail.get("speed_hfm_answer_ms")
    return Speed(
        limit,
        problem,
        rtt if isinstance(rtt, int) else None,
        int(last.detail["speed_total_ms"]),
        int(answer) if answer is not None else None,
        last.occurred_at,
    )


@dataclass
class Panel:
    robots: list[Robot]
    win_rate: Decimal | None
    profit_factor: Decimal | None
    closed: int
    ai_model: str | None
    ai_last: Any | None
    ai_fresh: bool
    # The 1-minute model (S85): its latest prediction, what one ounce can lose and win at the
    # better direction's probability, and predicted against happened so far.
    model: probability.Model = field(default_factory=lambda: probability.MODEL)
    prediction: Any | None = None
    direction: str | None = None  # UP or DOWN when the 5- and 15-minute trends agree
    best: tuple[str, Decimal] | None = None  # the trade in that direction, and its probability
    economics: probability.Economics | None = None
    accuracy: predictions.Accuracy | None = None
    ask_at: Decimal = probability.ASK_AT
    auto_at: Decimal = probability.AUTO_AT
    speed: Speed | None = None  # S87: the order path's speed (the 1-minute rule's own rule)


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
                ticket=await predictions.waiting(conn, m.id, now, expire=False),
                last_decision=(
                    await conn.execute(
                        select(gold_hfm_decisions)
                        .where(gold_hfm_decisions.c.mandate_id == m.id)
                        .order_by(desc(gold_hfm_decisions.c.bar))
                        .limit(1)
                    )
                ).first(),
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
    pred = await predictions.latest(conn)
    best, direction = None, None
    if pred is not None and pred.p_buy is not None and pred.p_sell is not None:
        trends = ((pred.features or {}).get("m15"), (pred.features or {}).get("m5"))
        if trends == ("UP", "UP"):
            direction, best = "UP", (probability.BUY, pred.p_buy)
        elif trends == ("DOWN", "DOWN"):
            direction, best = "DOWN", (probability.SELL, pred.p_sell)
        else:  # no agreed trend: the direction the model rates higher, which is no setup
            direction = "NO CLEAR TREND"
            best = (
                (probability.BUY, pred.p_buy)
                if pred.p_buy >= pred.p_sell
                else (probability.SELL, pred.p_sell)
            )
    return Panel(
        robots,
        report.win_rate,
        report.profit_factor,
        report.count,
        model,
        ai_last,
        ai_last is not None and now - ai_last.at <= AI_FRESH,
        prediction=pred,
        direction=direction,
        best=best,
        economics=probability.economics(probability.MODEL, Decimal(1), ZERO, best[1])
        if best is not None
        else None,
        accuracy=await predictions.accuracy(conn, since=now - ACCURACY_DAYS),
        speed=await _speed(conn, now),
    )
