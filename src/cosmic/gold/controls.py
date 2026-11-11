"""Robot controls (S79, the P0 demo directive of 2026-11-11): the owner's rules and profit goals
for a gold robot at HFM, and its status, beside its immutable mandate.

- **Rules:** a pause after N losing trades in a row (3 by default), the take-profit as a multiple
  of the trade's whole risk (2: the owner's 2 USD for 4), and a day ceiling: no entry whose full
  loss would take the robot's day past the mandate's daily loss.
- **Profit goals:** daily, session (since START or RESUME) and long-term (since START) targets,
  each optional, measured on the robot's own net realized result (after commission and swap) or
  that plus the floating result of its open trades.
- **Status:** RUNNING; PAUSED (no new entry; open trades keep their stop-loss and take-profit);
  TARGET_REACHED and LOSS_LIMIT (no new entry, this robot's waiting entries are withdrawn and its
  open trades are closed at market; when it holds nothing, its final net result is recorded).
  Nothing restarts by itself: only the owner's RESUME does. Every change is audited and told.
- A robot without a control row (granted before S79) trades as before.

Only this robot's own trades are ever touched: its mandate's entries, never another robot's,
never a trade made by hand."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.audit.service import record as audit
from cosmic.core.money import ZERO
from cosmic.db.schema_execution import approvals, orders, post_trade_reviews, trade_intents
from cosmic.db.schema_gold import robot_controls
from cosmic.notify.outbox import notify_owners
from cosmic.trading import mandates

RUNNING, PAUSED = "RUNNING", "PAUSED"
TARGET_REACHED, LOSS_LIMIT = "TARGET_REACHED", "LOSS_LIMIT"
SHUT = (TARGET_REACHED, LOSS_LIMIT)  # stop entries and close this robot's trades
REALIZED, WITH_FLOATING = "REALIZED", "REALIZED_AND_FLOATING"
ACTOR = Actor.system("gold_robot")
WAITING = ("PROPOSED", "APPROVED", "REQUIRES_REAPPROVAL")


@dataclass(frozen=True)
class Controls:
    mandate_id: uuid.UUID
    pause_after_losses: int = 3
    take_profit_r: Decimal = Decimal(2)
    daily_target: Decimal | None = None
    session_target: Decimal | None = None
    long_target: Decimal | None = None
    measure: str = REALIZED
    session_started_at: datetime | None = None
    status: str = RUNNING
    status_reason: str = "started"
    status_at: datetime | None = None
    final_net: Decimal | None = None


@dataclass(frozen=True)
class Results:
    """This robot's net results (after commission and swap), in the account's money."""

    today: Decimal
    session: Decimal
    total: Decimal
    floating: Decimal
    losses_in_a_row: int  # since it last ran on


def _row(r: Any) -> Controls:
    return Controls(
        r.mandate_id,
        r.pause_after_losses,
        r.take_profit_r,
        r.daily_target,
        r.session_target,
        r.long_target,
        r.measure,
        r.session_started_at,
        r.status,
        r.status_reason,
        r.status_at,
        r.final_net,
    )


async def get(conn: AsyncConnection, mandate_id: uuid.UUID) -> Controls | None:
    row = (
        await conn.execute(select(robot_controls).where(robot_controls.c.mandate_id == mandate_id))
    ).first()
    return None if row is None else _row(row)


def check(c: Controls) -> list[str]:
    """Each setting that cannot be used, in the owner's words; nothing is changed."""
    out = []
    if not 1 <= c.pause_after_losses <= 20:
        out.append("Pause after losses in a row: 1 to 20.")
    if not Decimal("0.5") <= c.take_profit_r <= 10:
        out.append("Take-profit: 0.5 to 10 times the risk.")
    for label, v in (
        ("Daily target", c.daily_target),
        ("Session target", c.session_target),
        ("Long-term target", c.long_target),
    ):
        if v is not None and v <= 0:
            out.append(f"{label}: more than 0, or empty for none.")
    if c.measure not in (REALIZED, WITH_FLOATING):
        out.append("Measured on: net realized, or net realized plus floating.")
    return out


async def create(conn: AsyncConnection, c: Controls, *, now: datetime, actor: Actor) -> Controls:
    """The controls of a robot just started (its status RUNNING, its session from now)."""
    c = Controls(**{**c.__dict__, "session_started_at": now, "status_at": now})
    await conn.execute(
        robot_controls.insert().values(
            mandate_id=c.mandate_id,
            pause_after_losses=c.pause_after_losses,
            take_profit_r=c.take_profit_r,
            daily_target=c.daily_target,
            session_target=c.session_target,
            long_target=c.long_target,
            measure=c.measure,
            session_started_at=now,
            status=RUNNING,
            status_reason="started by the owner",
            status_at=now,
            updated_at=now,
            updated_by=f"{actor.type.value}:{actor.id}",
        )
    )
    await audit(
        conn,
        action="robot.controls_set",
        actor=actor,
        occurred_at=now,
        entity_type="trading_mandate",
        entity_id=c.mandate_id,
        payload=_payload(c),
    )
    return c


async def change(conn: AsyncConnection, c: Controls, *, now: datetime, actor: Actor) -> Controls:
    """The owner's new rules and goals; the status and session are kept."""
    await conn.execute(
        update(robot_controls)
        .where(robot_controls.c.mandate_id == c.mandate_id)
        .values(
            pause_after_losses=c.pause_after_losses,
            take_profit_r=c.take_profit_r,
            daily_target=c.daily_target,
            session_target=c.session_target,
            long_target=c.long_target,
            measure=c.measure,
            updated_at=now,
            updated_by=f"{actor.type.value}:{actor.id}",
        )
    )
    await audit(
        conn,
        action="robot.controls_changed",
        actor=actor,
        occurred_at=now,
        entity_type="trading_mandate",
        entity_id=c.mandate_id,
        payload=_payload(c),
    )
    return c


def _payload(c: Controls) -> dict[str, Any]:
    return {
        "pause_after_losses": c.pause_after_losses,
        "take_profit_r": str(c.take_profit_r),
        "daily_target": None if c.daily_target is None else str(c.daily_target),
        "session_target": None if c.session_target is None else str(c.session_target),
        "long_target": None if c.long_target is None else str(c.long_target),
        "measure": c.measure,
    }


async def set_status(
    conn: AsyncConnection,
    mandate_id: uuid.UUID,
    status: str,
    reason: str,
    *,
    now: datetime,
    actor: Actor,
    new_session: bool = False,
) -> None:
    values: dict[str, Any] = {
        "status": status,
        "status_reason": reason[:500],
        "status_at": now,
        "updated_at": now,
        "updated_by": f"{actor.type.value}:{actor.id}",
    }
    if new_session:
        values.update(session_started_at=now, final_net=None)
    await conn.execute(
        update(robot_controls).where(robot_controls.c.mandate_id == mandate_id).values(**values)
    )
    await audit(
        conn,
        action=f"robot.{status.lower()}",
        actor=actor,
        occurred_at=now,
        entity_type="trading_mandate",
        entity_id=mandate_id,
        payload={"reason": reason[:500]},
    )
    await notify_owners(
        conn,
        kind="gold_sim",
        payload={"text": f"HFM DEMO robot: {status.replace('_', ' ')} — {reason}"},
        now=now,
        dedup_key=f"robot:{mandate_id}:{status}:{now:%Y-%m-%dT%H:%M:%S}",
    )


async def results(
    conn: AsyncConnection,
    m: mandates.Mandate,
    c: Controls,
    now: datetime,
    held: list[tuple[uuid.UUID, Decimal]],
    bid: Decimal,
    ask: Decimal,
) -> Results:
    """The robot's own net results: today (UTC), this session, since START; the floating
    result of its open trades at HFM's price; and its losing trades in a row since it last ran
    on."""
    rows = list(
        await conn.execute(
            select(post_trade_reviews.c.realized_pnl, post_trade_reviews.c.created_at)
            .select_from(post_trade_reviews)
            .join(trade_intents, trade_intents.c.id == post_trade_reviews.c.intent_id)
            .where(
                trade_intents.c.account_id == m.account_id,
                trade_intents.c.details["mandate_id"].astext == str(m.id),
            )
            .order_by(post_trade_reviews.c.created_at)
        )
    )
    day = mandates.day_start(now)
    since = c.session_started_at or m.granted_at
    run_on = c.status_at or m.granted_at

    def total(after: datetime) -> Decimal:
        return sum((Decimal(str(r.realized_pnl or 0)) for r in rows if r.created_at >= after), ZERO)

    streak = 0
    for r in reversed([r for r in rows if r.created_at >= run_on]):
        if r.realized_pnl is not None and r.realized_pnl < 0:
            streak += 1
        else:
            break
    floating = ZERO
    for intent_id, qty in held:
        entry = (
            await conn.execute(
                select(orders.c.avg_fill_price, orders.c.side).where(
                    orders.c.intent_id == intent_id, orders.c.purpose == "ENTRY"
                )
            )
        ).first()
        if entry is None or entry.avg_fill_price is None:
            continue
        if entry.side == "BUY":
            floating += (bid - entry.avg_fill_price) * qty
        else:
            floating += (entry.avg_fill_price - ask) * qty
    return Results(total(day), total(since), total(m.granted_at), floating, streak)


def judge(m: mandates.Mandate, c: Controls, r: Results) -> tuple[str, str] | None:
    """The status this robot's results call for now, with why, or None (no change). Checked
    while RUNNING (and PAUSED for the loss limit): the daily loss first, then the goals, then
    the losing trades in a row."""
    if c.status in SHUT:
        return None
    floating = r.floating if c.measure == WITH_FLOATING else ZERO
    # Realized only: each open trade's stop caps the rest, and the day ceiling (day_room) never
    # lets one more full loss pass the limit.
    if r.today <= -m.daily_loss_limit:
        return LOSS_LIMIT, (
            f"today's net realized result {r.today:,.2f} reached the daily loss limit of "
            f"{m.daily_loss_limit:,.2f}"
        )
    if c.status != RUNNING:
        return None
    for label, target, value in (
        ("daily", c.daily_target, r.today),
        ("session", c.session_target, r.session),
        ("long-term", c.long_target, r.total),
    ):
        if target is not None and value + floating >= target:
            how = "net realized" + (" plus floating" if c.measure == WITH_FLOATING else "")
            return TARGET_REACHED, (
                f"the {label} profit goal of {target:,.2f} is reached ({how}: "
                f"{value + floating:,.2f})"
            )
    if r.losses_in_a_row >= c.pause_after_losses:
        return PAUSED, (
            f"{r.losses_in_a_row} losing trades in a row: paused until you resume it "
            f"(your limit is {c.pause_after_losses})"
        )
    return None


def day_room(m: mandates.Mandate, r: Results, risk: Decimal) -> str | None:
    """Why this entry may not open: a full loss on it would take the robot's day past the
    mandate's daily loss (the owner's 6 USD is a ceiling, never passed by one more trade)."""
    lost = max(-r.today, ZERO)
    if lost + risk > m.daily_loss_limit:
        return (
            f"WAIT — a full loss on this trade ({risk:,.2f}) would take today past the daily "
            f"limit of {m.daily_loss_limit:,.2f} (lost so far {lost:,.2f})"
        )
    return None


async def withdraw_entries(
    conn: AsyncConnection, m: mandates.Mandate, why: str, now: datetime
) -> int:
    """This robot's entries not yet sent to HFM are withdrawn (their approval revoked, the
    proposal blocked); an entry already on its way finishes as HFM answers (immediate or
    cancel), and its position is then closed like the others."""
    ids = [
        r.id
        for r in await conn.execute(
            select(trade_intents.c.id).where(
                trade_intents.c.account_id == m.account_id,
                trade_intents.c.effect.in_(["OPEN", "INCREASE"]),
                trade_intents.c.status.in_(WAITING),
                trade_intents.c.details["mandate_id"].astext == str(m.id),
            )
        )
    ]
    if not ids:
        return 0
    await conn.execute(
        update(approvals)
        .where(approvals.c.intent_id.in_(ids), approvals.c.status == "ACTIVE")
        .values(status="REVOKED", status_reason=why[:500])
    )
    await conn.execute(
        update(trade_intents)
        .where(trade_intents.c.id.in_(ids), trade_intents.c.status.in_(WAITING))
        .values(status="BLOCKED", status_reason=why[:2000], updated_at=now)
    )
    return len(ids)


async def record_final(
    conn: AsyncConnection, m: mandates.Mandate, net: Decimal, now: datetime
) -> None:
    """Once this robot holds nothing after a shutdown: its final net result, recorded once."""
    done = await conn.execute(
        update(robot_controls)
        .where(robot_controls.c.mandate_id == m.id, robot_controls.c.final_net.is_(None))
        .values(final_net=net, updated_at=now)
    )
    if done.rowcount:
        await audit(
            conn,
            action="robot.final_result",
            actor=ACTOR,
            occurred_at=now,
            entity_type="trading_mandate",
            entity_id=m.id,
            payload={"session_net": str(net)},
        )
        await notify_owners(
            conn,
            kind="gold_sim",
            payload={
                "text": f"HFM DEMO robot: every trade closed at HFM; the session's final net "
                f"result is {net:+,.2f} USD (after commission and swap)."
            },
            now=now,
            dedup_key=f"robot:{m.id}:final:{now:%Y-%m-%dT%H:%M:%S}",
        )
