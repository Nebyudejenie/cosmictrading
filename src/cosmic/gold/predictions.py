"""The 1-minute rule's forward test, and the owner's approvals in advance (S85).

**Forward test, without a single trade.** At every new 1-minute candle of HFM the model's
probabilities are stored once (``gold_predictions``), whether or not a robot runs. Later the
same candles say what happened to each direction with the model's own trade shape: the
take-profit first, the stop-loss first, or neither within four hours
(``gold_prediction_outcomes``). Predicted against happened is the model's calibration on data
it has never seen (``accuracy``): the evidence a new model version needs before it may replace
this one. Nothing here changes the model.

**Approvals in advance (tickets).** A 1-minute trade is over in about two minutes (lab Run 7),
so an approval asked after the signal would come too late. The owner instead approves the
robot's next trade, once, with their authenticator code; the robot takes the next setup under
it, and the approval is used up. One waiting per robot; it lapses after an hour.
"""

from __future__ import annotations

import bisect
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import case, desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.audit.service import record as audit
from cosmic.core.errors import ConflictError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_gold import gold_prediction_outcomes, gold_predictions, robot_tickets
from cosmic.gold import probability, sim
from cosmic.gold.probability import BUY, SELL, WAIT, Model, Prediction
from cosmic.market import hfm_candles
from cosmic.trading import mandates
from cosmic.trading.brokers.hfm_trade_check import session_closed

CANDLES = {"1m": 240, "5m": 200, "15m": 200}  # complete candles the estimate reads
HORIZON = 240  # minutes: a direction not decided by then is "neither"
SCORE_WINDOW = timedelta(hours=6)
SCORE_CANDLES = 420
TICKET_LIFE = timedelta(minutes=60)
TP, SL, NONE = "TP", "SL", "NONE"
USED, REFUSED, CANCELLED, EXPIRED = "USED", "REFUSED", "CANCELLED", "EXPIRED"
ACTOR = Actor.system("gold_robot")


def _prediction(r: Any) -> Prediction:
    return Prediction(r.bar, r.model, r.p_buy, r.p_sell, r.setup, r.why, dict(r.features or {}))


async def ensure(conn: AsyncConnection, now: datetime, model: Model | None = None) -> Prediction:
    """The prediction at HFM's last complete 1-minute candle: read if stored, else made and
    stored (once per candle and model). No estimate, with the reason, when the candles are too
    few or too old, gold stops trading within the half hour, or HFM gives no price."""
    model = model or probability.MODEL
    m1 = await hfm_candles.candles(conn, "1m", CANDLES["1m"])
    if m1:
        row = (
            await conn.execute(
                select(gold_predictions).where(
                    gold_predictions.c.bar == m1[-1].open_time,
                    gold_predictions.c.model == model.version,
                )
            )
        ).first()
        if row is not None:
            return _prediction(row)
    m5 = await hfm_candles.candles(conn, "5m", CANDLES["5m"])
    m15 = await hfm_candles.candles(conn, "15m", CANDLES["15m"])
    pred = probability.predict(model, now, m1, m5, m15, session_closed(now))
    if pred.bar is None:
        return pred
    price = await sim.price_now(conn, now)
    if price is None and pred.p_buy is not None:
        pred = replace(pred, p_buy=None, p_sell=None, setup=WAIT, why="no current HFM price")
    await conn.execute(
        pg_insert(gold_predictions)
        .values(
            id=uuid7(),
            bar=pred.bar,
            model=pred.model,
            at=now,
            bid=price.bid if price else None,
            ask=price.ask if price else None,
            p_buy=pred.p_buy,
            p_sell=pred.p_sell,
            setup=pred.setup,
            why=pred.why,
            features=pred.features,
        )
        .on_conflict_do_nothing(constraint="gold_predictions_bar_model")
    )
    return pred


async def latest(conn: AsyncConnection, model: Model | None = None) -> Any | None:
    """The newest stored prediction of the model in force (for the screen)."""
    model = model or probability.MODEL
    return (
        await conn.execute(
            select(gold_predictions)
            .where(gold_predictions.c.model == model.version)
            .order_by(desc(gold_predictions.c.bar))
            .limit(1)
        )
    ).first()


def _outcome(
    side: str, p: Any, model: Model, after: list[hfm_candles.Candle], now: datetime
) -> tuple[str, datetime, int] | None:
    """What HFM's bid candles after the prediction did to ``side``: (outcome, when, minutes), or
    None while undecided. Both levels inside one candle count as the stop, as in the study."""
    spread = p.ask - p.bid
    if side == BUY:  # closed at the bid: the stop below the ask paid, the target above it
        stop, target = p.ask - model.stop, p.ask + model.target
    else:  # a short closes at the ask: the bid levels are a spread lower
        stop, target = p.bid + model.stop - spread, p.bid - model.target - spread
    minute = timedelta(minutes=1)
    for k, c in enumerate(after[:HORIZON]):
        hit_stop = c.low <= stop if side == BUY else c.high >= stop
        hit_target = c.high >= target if side == BUY else c.low <= target
        if hit_stop:
            return SL, c.open_time + minute, k + 1
        if hit_target:
            return TP, c.open_time + minute, k + 1
    if len(after) >= HORIZON or now - p.bar > timedelta(minutes=HORIZON + 5):
        return NONE, now, HORIZON
    return None


async def score(conn: AsyncConnection, now: datetime, model: Model | None = None) -> int:
    """Record the outcomes that HFM's candles have decided since; returns how many."""
    model = model or probability.MODEL
    scored = (
        select(func.count())
        .select_from(gold_prediction_outcomes)
        .where(gold_prediction_outcomes.c.prediction_id == gold_predictions.c.id)
        .scalar_subquery()
    )
    pending = list(
        await conn.execute(
            select(gold_predictions)
            .where(
                gold_predictions.c.model == model.version,
                gold_predictions.c.p_buy.is_not(None),
                gold_predictions.c.bar > now - SCORE_WINDOW,
                scored < 2,
            )
            .order_by(gold_predictions.c.bar)
            .limit(400)
        )
    )
    if not pending:
        return 0
    done = {
        (r.prediction_id, r.side)
        for r in await conn.execute(
            select(gold_prediction_outcomes.c.prediction_id, gold_prediction_outcomes.c.side).where(
                gold_prediction_outcomes.c.prediction_id.in_([p.id for p in pending])
            )
        )
    }
    candles = await hfm_candles.candles(conn, "1m", SCORE_CANDLES)
    times = [c.open_time for c in candles]
    written = 0
    for p in pending:
        after = candles[bisect.bisect_right(times, p.bar) :]
        for side in (BUY, SELL):
            if (p.id, side) in done:
                continue
            found = _outcome(side, p, model, after, now)
            if found is None:
                continue
            await conn.execute(
                pg_insert(gold_prediction_outcomes)
                .values(
                    prediction_id=p.id,
                    side=side,
                    outcome=found[0],
                    resolved_at=found[1],
                    minutes=found[2],
                )
                .on_conflict_do_nothing()
            )
            written += 1
    return written


async def tick(db: Database, now: datetime, model: Model | None = None) -> None:
    """Once per new 1-minute candle: this candle's prediction, and the outcomes now known."""
    model = model or probability.MODEL
    async with db.connect() as conn:
        last = await hfm_candles.candles(conn, "1m", 1)
        if not last:
            return
        seen = await conn.scalar(
            select(gold_predictions.c.id).where(
                gold_predictions.c.bar == last[-1].open_time,
                gold_predictions.c.model == model.version,
            )
        )
    if seen is not None:
        return
    async with db.transaction() as conn:
        await ensure(conn, now, model)
        await score(conn, now, model)


@dataclass(frozen=True)
class Accuracy:
    """Predicted against happened, over every scored prediction and over the robot's setups."""

    scored: int
    predicted: Decimal | None
    happened: Decimal | None
    setups: int
    setups_predicted: Decimal | None
    setups_happened: Decimal | None


async def accuracy(
    conn: AsyncConnection, model: Model | None = None, since: datetime | None = None
) -> Accuracy:
    """``since``: only predictions of candles from then on (the dashboard's last 30 days)."""
    model = model or probability.MODEL
    o, p = gold_prediction_outcomes, gold_predictions
    said = case((o.c.side == BUY, p.c.p_buy), else_=p.c.p_sell)
    won = case((o.c.outcome == TP, 1.0), else_=0.0)
    base = (
        select(func.count(), func.avg(said), func.avg(won))
        .select_from(o.join(p, p.c.id == o.c.prediction_id))
        .where(p.c.model == model.version)
    )
    if since is not None:
        base = base.where(p.c.bar >= since)

    def pct(v: Any) -> Decimal | None:
        return None if v is None else Decimal(str(v)).quantize(Decimal("0.0001"))

    every = (await conn.execute(base)).one()
    setups = (await conn.execute(base.where(p.c.setup == o.c.side))).one()
    return Accuracy(
        int(every[0]), pct(every[1]), pct(every[2]), int(setups[0]), pct(setups[1]), pct(setups[2])
    )


# ---- the owner's approvals in advance ----------------------------------------------------------


async def waiting(
    conn: AsyncConnection, mandate_id: uuid.UUID, now: datetime, *, expire: bool = True
) -> Any | None:
    """The approval waiting for this robot's next trade, or None. One past its hour is ended
    EXPIRED here (``expire``: False on a page, which writes nothing)."""
    row = (
        await conn.execute(
            select(robot_tickets).where(
                robot_tickets.c.mandate_id == mandate_id, robot_tickets.c.ended.is_(None)
            )
        )
    ).first()
    if row is None or row.expires_at > now:
        return row
    if expire:
        await end(conn, row.id, EXPIRED, now)
    return None


async def end(
    conn: AsyncConnection,
    ticket_id: uuid.UUID,
    how: str,
    now: datetime,
    intent_id: uuid.UUID | None = None,
) -> None:
    await conn.execute(
        update(robot_tickets)
        .where(robot_tickets.c.id == ticket_id, robot_tickets.c.ended.is_(None))
        .values(ended=how, ended_at=now, intent_id=intent_id)
    )
    await audit(
        conn,
        action=f"robot.trade_approval_{how.lower()}",
        actor=ACTOR,
        occurred_at=now,
        entity_type="robot_ticket",
        entity_id=ticket_id,
        payload={"intent_id": str(intent_id) if intent_id else None},
    )


async def arm(
    conn: AsyncConnection, m: mandates.Mandate, *, actor: Actor, now: datetime
) -> uuid.UUID:
    """The owner approves this robot's next trade (their code is checked by the caller). What
    they were shown is kept with it: the latest probability, and whether that was below their
    30 % threshold (a PRACTICE trade)."""
    if not m.active(now):
        raise ConflictError("the robot is not running: start it first")
    if await waiting(conn, m.id, now) is not None:
        raise ConflictError("an approval is already waiting for the robot's next trade")
    row = await latest(conn)
    best = _prediction(row).best if row is not None else None
    shown = best[1] if best is not None else None
    practice = shown is None or shown < probability.ASK_AT
    ticket_id = uuid7()
    await conn.execute(
        robot_tickets.insert().values(
            id=ticket_id,
            mandate_id=m.id,
            armed_at=now,
            armed_by=f"{actor.type.value}:{actor.id}",
            expires_at=now + TICKET_LIFE,
            shown_probability=shown,
            practice=practice,
        )
    )
    await audit(
        conn,
        action="robot.trade_approved_in_advance",
        actor=actor,
        occurred_at=now,
        entity_type="robot_ticket",
        entity_id=ticket_id,
        payload={
            "mandate_id": str(m.id),
            "shown_probability": str(shown) if shown is not None else None,
            "practice": practice,
            "expires_at": (now + TICKET_LIFE).isoformat(),
        },
    )
    return ticket_id


async def cancel(conn: AsyncConnection, mandate_id: uuid.UUID, now: datetime) -> bool:
    """The owner takes the waiting approval back; True when there was one."""
    row = await waiting(conn, mandate_id, now)
    if row is None:
        return False
    await end(conn, row.id, CANCELLED, now)
    return True


__all__ = ["BUY", "SELL", "WAIT"]
