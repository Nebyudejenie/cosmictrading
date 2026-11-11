"""The HFM demo self-test (S67, ADR-037 addendum): the owner's proof that My Gold Auto-Trader trades
the HFM demo end to end, in both directions, without a code for each trade.

Under the owner's mandate (granted with their code at START, Automatic):

1. **BUY:** HFM's smallest gold trade (1 ounce) is proposed through every risk check, approved by
   the mandate and sent with its stop-loss (1.5 × the hourly ATR) and a near take-profit. Its
   fill is HFM's deal; its stop is read back from HFM's own position.
2. It closes by itself: at its take-profit (monitored, sold with the stop still on), or after
   ``TIME_LIMIT`` by an exit through the same path.
3. **SELL:** the same as a genuine short (when the owner chose buying and selling).
4. **Reconciliation:** the next check of our books against HFM's passes.

Every step is recorded (``gold_hfm_selftests.evidence``: HFM's deal and position numbers, the
stop and target, how each closed, the result) and the owner is told the outcome once. Nothing
here is a trading idea: the two trades cost the spread. Virtual money; never real money."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import desc, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError
from cosmic.core.ids import uuid7
from cosmic.core.jsonutil import to_jsonable
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_execution import (
    fills,
    order_events,
    orders,
    reconciliation_runs,
    trade_intents,
)
from cosmic.db.schema_gold import gold_hfm_selftests
from cosmic.db.schema_trading import accounts, bridge_state
from cosmic.gold import analysis, hfm_demo, sim
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.trading import mandates
from cosmic.trading.accounts import get_account
from cosmic.trading.brokers.mt5_bridge import BY_ADAPTER, SHORTS_VERSION, bridge_version, magic
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate, entry_held

TIME_LIMIT = timedelta(minutes=15)  # a test trade its target has not closed is closed then
OLD_BRIDGE = "the HFM bridge is older than version 5"  # the SELL then follows by itself
ENDED = ("BLOCKED", "REJECTED", "EXPIRED", "NON_EXECUTABLE", "CANCELLED", "SUPERSEDED")
SIDES = ("BUY", "SELL")


async def _report(conn: AsyncConnection, account_id: uuid.UUID) -> dict[str, Any]:
    """The latest report of the bridge that trades this account (S74: each HFM demo account has
    its own bridge), or {} when none has reported."""
    adapter = await conn.scalar(
        select(accounts.c.broker_adapter).where(accounts.c.id == account_id)
    )
    bridge = BY_ADAPTER.get(adapter or "")
    if bridge is None:
        return {}
    snap = await conn.scalar(
        select(bridge_state.c.snapshot).where(bridge_state.c.bridge == bridge.name)
    )
    return dict(snap or {})


async def open_run(conn: AsyncConnection, mandate_id: uuid.UUID) -> Any | None:
    return (
        await conn.execute(
            select(gold_hfm_selftests).where(
                gold_hfm_selftests.c.mandate_id == mandate_id,
                gold_hfm_selftests.c.finished_at.is_(None),
            )
        )
    ).first()


async def latest(conn: AsyncConnection) -> Any | None:
    """The newest run, finished or not, for the panel."""
    return (
        await conn.execute(
            select(gold_hfm_selftests).order_by(desc(gold_hfm_selftests.c.requested_at)).limit(1)
        )
    ).first()


async def start(
    conn: AsyncConnection, m: mandates.Mandate, owner: uuid.UUID, now: datetime
) -> uuid.UUID:
    """A new run under ``m`` (Automatic only: an asked trade would wait for the owner)."""
    if m.strategy_key != mandates.GOLD_READING or m.mode != "AUTO":
        raise ConflictError("the self-test runs under My Gold Auto-Trader on HFM Demo, Automatic")
    if await open_run(conn, m.id) is not None:
        raise ConflictError("a self-test is already running")
    run_id = uuid7()
    await conn.execute(
        gold_hfm_selftests.insert().values(
            id=run_id,
            mandate_id=m.id,
            requested_by=owner,
            requested_at=now,
            phase="BUY",
            evidence={},
            updated_at=now,
        )
    )
    return run_id


async def advance(db: Database, now: datetime) -> list[str]:
    """Every second (the gold job): each open run takes its next step."""
    async with db.connect() as conn:
        runs = list(
            await conn.execute(
                select(gold_hfm_selftests.c.id, gold_hfm_selftests.c.mandate_id).where(
                    gold_hfm_selftests.c.finished_at.is_(None)
                )
            )
        )
    out = await _follow_up_sells(db, now)
    for r in runs:
        async with db.transaction() as conn:
            await conn.execute(  # the same lock as the mandate's own decisions
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"gold-hfm:{r.mandate_id}"},
            )
            run = (
                await conn.execute(
                    select(gold_hfm_selftests).where(gold_hfm_selftests.c.id == r.id)
                )
            ).first()
            if run is not None and run.finished_at is None:
                step = await _step(conn, run, now)
                if step:
                    out.append(f"{run.id}:{step}")
    return out


async def _follow_up_sells(db: Database, now: datetime) -> list[str]:
    """A SELL the owner's self-test could not run only because the bridge was older than 5 runs
    by itself, under the same mandate and the same authorisation, once HFM's report shows
    bridge 5 (no second press, S70). Once: never when a later run exists."""
    started = []
    async with db.transaction() as conn:
        for m in await hfm_demo.gold_mandates(conn, now):
            if m.mode != "AUTO" or await open_run(conn, m.id) is not None:
                continue
            if bridge_version(await _report(conn, m.account_id)) < SHORTS_VERSION:
                continue
            last = (
                await conn.execute(
                    select(gold_hfm_selftests)
                    .where(gold_hfm_selftests.c.mandate_id == m.id)
                    .order_by(desc(gold_hfm_selftests.c.requested_at))
                    .limit(1)
                )
            ).first()
            sell = ((last.evidence or {}).get("SELL") or {}) if last is not None else {}
            if last is None or sell.get("skipped") != OLD_BRIDGE:
                continue
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"gold-hfm:{m.id}"},
            )
            run_id = uuid7()
            await conn.execute(
                gold_hfm_selftests.insert().values(
                    id=run_id,
                    mandate_id=m.id,
                    requested_by=last.requested_by,
                    requested_at=now,
                    phase="SELL",
                    evidence={"BUY": {"skipped": f"proved in the run before ({last.id})"}},
                    updated_at=now,
                )
            )
            started.append(f"{run_id}:SELL_FOLLOWS")
    return started


async def _save(conn: AsyncConnection, run: Any, now: datetime, **values: Any) -> None:
    if "evidence" in values:
        values["evidence"] = to_jsonable(values["evidence"])
    await conn.execute(
        update(gold_hfm_selftests)
        .where(gold_hfm_selftests.c.id == run.id)
        .values(updated_at=now, **values)
    )


async def _finish(
    conn: AsyncConnection,
    run: Any,
    evidence: dict[str, Any],
    outcome: str,
    why: str,
    now: datetime,
    *,
    tell: bool = True,
) -> str:
    evidence = {**evidence, "outcome": why}
    await _save(conn, run, now, phase="DONE", outcome=outcome, evidence=evidence, finished_at=now)
    if not tell:
        return outcome
    await notify_owners(
        conn,
        kind="gold_sim",
        payload={"text": f"HFM DEMO SELF-TEST {outcome}: {summary(evidence)}"},
        now=now,
        dedup_key=f"gold-hfm-selftest:{run.id}:done",
    )
    return outcome


def summary(evidence: dict[str, Any]) -> str:
    """The run in one paragraph, HFM's own numbers first."""
    parts = []
    for side in SIDES:
        e = evidence.get(side) or {}
        if not e:
            continue
        if e.get("skipped"):
            parts.append(f"{side}: not tested ({e['skipped']})")
            continue
        opened = e.get("opened") or {}
        closed = e.get("closed") or {}
        words = f"{side} {opened.get('ounces', '?')} oz"
        if opened:
            words += (
                f" at {opened.get('price')} (HFM deal {opened.get('deal')}), stop-loss "
                f"{opened.get('stop_at_hfm')} held by HFM on position {opened.get('position')}"
            )
        if closed:
            words += (
                f"; closed by {closed.get('by')} at {closed.get('price')} (HFM deal "
                f"{closed.get('deal')}), result {closed.get('result')}"
            )
        parts.append(words)
    rec = evidence.get("reconciliation") or {}
    if rec:
        parts.append(f"books against HFM: {rec.get('status')} (check {rec.get('run')})")
    parts.append(str(evidence.get("outcome", "")))
    return "; ".join(p for p in parts if p)


async def _step(conn: AsyncConnection, run: Any, now: datetime) -> str:
    m = await mandates.get_mandate(conn, run.mandate_id)
    evidence: dict[str, Any] = dict(run.evidence or {})
    if not m.active(now):
        if m.status == "REVOKED" and (m.end_reason or "").startswith(mandates.REPLACED_REASON):
            # The owner's newer START replaced it: not a failure, and the new START says what
            # runs now (S72: three presses in a minute were told as two failures).
            return await _finish(
                conn, run, evidence, "REPLACED", "replaced by your newer START", now, tell=False
            )
        why = {
            "REVOKED": "you switched HFM Demo off before it finished",
            "STOPPED": f"HFM Demo stopped before it finished ({m.end_reason})",
        }.get(m.status, "HFM Demo ended before it finished")
        return await _finish(conn, run, evidence, "FAILED", why, now)
    if run.phase == "RECONCILE":
        return await _reconcile(conn, run, m, evidence, now)
    side = run.phase
    intent_id = run.buy_intent if side == "BUY" else run.sell_intent
    if intent_id is None:
        return await _open(conn, run, m, side, evidence, now)
    return await _follow(conn, run, m, side, intent_id, evidence, now)


async def _open(
    conn: AsyncConnection,
    run: Any,
    m: mandates.Mandate,
    side: str,
    evidence: dict[str, Any],
    now: datetime,
) -> str:
    account = await get_account(conn, account_id=m.account_id)
    inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
    if side == "SELL":
        _, policy = await active_policy(conn)
        why = (
            "you chose buying only"
            if not policy.shorts_allowed(inst.venue_key, inst.asset_class)
            else OLD_BRIDGE
            if bridge_version(await _report(conn, account.id)) < SHORTS_VERSION
            else None
        )
        if why is not None:
            evidence["SELL"] = {"skipped": why}
            if why == OLD_BRIDGE:
                evidence["SELL"]["follows"] = "runs by itself once HFM's bridge is version 5"
            await _save(conn, run, now, phase="RECONCILE", evidence=evidence)
            return "SELL_SKIPPED"
    price = await sim.price_now(conn, now)
    reading = await sim.reading_now(conn, now)
    if price is None or reading.stop_distance is None or inst.tick_size is None:
        return ""  # the market is closed or the candles too few: it waits
    tick, lot = inst.tick_size, inst.lot_size or Decimal(1)
    short = side == "SELL"
    margin = 2 * price.spread  # a marketable limit, a little past the price
    if short:
        entry = hfm_demo._tick(price.bid - margin, tick)
        stop = hfm_demo._tick(price.bid + reading.stop_distance, tick, ROUND_CEILING)
        target = entry - tick  # near: the first move down past the entry closes it
    else:
        entry = hfm_demo._tick(price.ask + margin, tick, ROUND_CEILING)
        stop = hfm_demo._tick(price.ask - reading.stop_distance, tick)
        target = entry + tick
    if abs(entry - stop) * lot > m.max_loss_per_trade:
        evidence[side] = {"skipped": "1 ounce would risk more than your loss a trade now"}
        return await _finish(
            conn, run, evidence, "FAILED", "HFM's smallest trade does not fit your risk now", now
        )
    candidate = Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": hfm_demo.GOLD,
            "side": side,
            "effect": "OPEN",
            "quantity": lot,
            "limit_price": entry,
            "price_band_low": entry
            if short
            else hfm_demo._tick(price.bid * Decimal("0.995"), tick),
            "price_band_high": hfm_demo._tick(price.ask * Decimal("1.005"), tick, ROUND_CEILING)
            if short
            else entry,
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",
            "horizon": "minutes",
            "thesis": (
                f"HFM demo SELF-TEST ({side}), asked by the owner to prove the automatic "
                "workflow: the smallest gold trade, its stop-loss held by HFM, closed by itself "
                "at a near take-profit or after 15 minutes, then reconciled. Not a trading idea."
            ),
            "plain_summary": (
                f"HFM demo self-test: {'sell (short)' if short else 'buy'} {lot.normalize():f} "
                f"ounce, stop-loss {stop}, take-profit {target}, closed within 15 minutes."
            ),
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{hfm_demo.GOLD}",
                    "summary": f"HFM bid {price.bid} / ask {price.ask}",
                },
                {
                    "kind": "NOTE",
                    "ref": f"selftest:{run.id}",
                    "summary": "the owner's HFM demo self-test",
                },
            ],
            "counterarguments": [
                "A test trade costs the spread; it proves the workflow, not an edge."
            ],
            "red_team": "A sudden move could hit the stop instead; the stop is the normal one.",
            "scenarios": [
                {"name": "Target", "description": f"Gold reaches {target}: closed there."},
                {"name": "Time", "description": "Not reached in 15 minutes: closed at the price."},
            ],
            "invalidation": [f"HFM's price reaches the stop {stop}"],
            "entry_conditions": f"Limit {side.lower()} at {entry}, now or never",
            "management_plan": {
                "stop_loss": {"stop_price": stop, "limit_price": stop},
                "take_profit_price": target,
            },
        }
    )
    intent, refused = await hfm_demo._propose(
        conn, m, candidate, now, extra={"self_test": side, "self_test_id": str(run.id)}
    )
    evidence[side] = {
        "proposed_at": now,
        "limit": entry,
        "stop": stop,
        "target": target,
        **({"refused": refused} if refused else {}),
    }
    column = "buy_intent" if side == "BUY" else "sell_intent"
    await _save(conn, run, now, evidence=evidence, **{column: intent})
    if refused is not None or intent is None:
        return await _finish(
            conn, run, evidence, "FAILED", f"the {side} was refused: {refused}", now
        )
    return f"{side}_PROPOSED"


async def _follow(
    conn: AsyncConnection,
    run: Any,
    m: mandates.Mandate,
    side: str,
    intent_id: uuid.UUID,
    evidence: dict[str, Any],
    now: datetime,
) -> str:
    e: dict[str, Any] = dict(evidence.get(side) or {})
    intent = (
        await conn.execute(select(trade_intents).where(trade_intents.c.id == intent_id))
    ).first()
    rows = list(
        await conn.execute(
            select(orders).where(orders.c.intent_id == intent_id).order_by(orders.c.created_at)
        )
    )
    entry = next((o for o in rows if o.purpose == "ENTRY"), None)
    filled = entry is not None and entry.filled_qty > 0
    if not filled:
        if intent is not None and intent.status in ENDED:
            return await _finish(
                conn,
                run,
                {**evidence, side: e},
                "FAILED",
                f"the {side} was not filled: {intent.status_reason or intent.status}",
                now,
            )
        if entry is not None and entry.status in ("EXPIRED", "REJECTED", "CANCELLED"):
            return await _finish(
                conn, run, {**evidence, side: e}, "FAILED", f"HFM did not fill the {side}", now
            )
        return ""
    if "opened" not in e:
        assert entry is not None
        deal = await conn.scalar(
            select(fills.c.broker_fill_id).where(fills.c.order_id == entry.id).limit(1)
        )
        snap = await _report(conn, m.account_id)
        mine = [
            p
            for p in snap.get("positions") or []
            if int(p.get("magic") or 0) == magic(entry.client_order_id)
        ]
        if not mine:
            return ""  # HFM's next report shows the position and its stop
        e["opened"] = {
            "trade_code": intent.trade_code if intent is not None else None,
            "ounces": entry.filled_qty,
            "price": entry.avg_fill_price,
            "deal": deal,
            "position": mine[0].get("ticket"),
            "stop_at_hfm": mine[0].get("sl"),
            "type_at_hfm": "SELL (short)" if int(mine[0].get("type") or 0) == 1 else "BUY",
            "at": now,
        }
        await _save(conn, run, now, evidence={**evidence, side: e})
        return f"{side}_OPENED"
    held = await entry_held(conn, intent_id)
    if held > ZERO:
        opened_at = datetime.fromisoformat(str(e["opened"]["at"]))
        if now - opened_at > TIME_LIMIT and "time_exit" not in e:
            in_flight = any(
                o.purpose in ("EXIT", "TAKE_PROFIT")
                and o.status in ("AUTHORIZED", "SUBMITTING", "SUBMITTED", "ACKNOWLEDGED")
                for o in rows
            )
            if not in_flight:
                price = await sim.price_now(conn, now)
                inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
                if price is None or inst.tick_size is None:
                    return ""
                account = await get_account(conn, account_id=m.account_id)
                reading = await sim.reading_now(conn, now)
                long = side == "BUY"
                exit_candidate = hfm_demo._exit(
                    account,
                    inst.tick_size,
                    price.bid if long else price.ask,
                    held,
                    [intent_id],
                    reading if reading.frames else analysis.Reading(now, {}, analysis.WAIT),
                    long=long,
                )
                exit_id, refused = await hfm_demo._propose(
                    conn, m, exit_candidate, now, extra={"self_test": f"{side}_EXIT"}
                )
                e["time_exit"] = {"intent": exit_id, "refused": refused, "at": now}
                await _save(conn, run, now, evidence={**evidence, side: e})
                return f"{side}_TIME_EXIT"
        return ""
    if "closed" not in e:
        closer = next(
            (
                o
                for o in reversed(rows)
                if o.purpose in ("TAKE_PROFIT", "EXIT", "STOP_LOSS") and o.filled_qty > 0
            ),
            None,
        )
        exit_rows = list(
            await conn.execute(
                select(orders).where(
                    orders.c.purpose == "EXIT",
                    orders.c.intent_id == intent_id,
                    orders.c.filled_qty > 0,
                )
            )
        )
        closer = closer or (exit_rows[-1] if exit_rows else None)
        result = await conn.scalar(
            select(fills.c.realized_pnl)
            .join(orders, orders.c.id == fills.c.order_id)
            .where(orders.c.intent_id == intent_id, fills.c.realized_pnl.is_not(None))
            .order_by(fills.c.recorded_at.desc())
            .limit(1)
        )
        deal = (
            await conn.scalar(
                select(fills.c.broker_fill_id).where(fills.c.order_id == closer.id).limit(1)
            )
            if closer is not None
            else None
        )
        how = {
            "TAKE_PROFIT": "its take-profit",
            "EXIT": "the time limit",
            "STOP_LOSS": "its stop-loss at HFM",
        }.get(closer.purpose if closer is not None else "", "a close")
        if closer is not None and closer.purpose == "EXIT":  # booked from HFM's own deal?
            first = await conn.scalar(
                select(order_events.c.detail)
                .where(order_events.c.order_id == closer.id)
                .order_by(order_events.c.seq)
                .limit(1)
            )
            if "closed_at_take_profit" in (first or {}):
                how = "its take-profit, held and executed by HFM"
            elif "closed_by_hand" in (first or {}):
                how = "a close by hand in MT5"
        e["closed"] = {
            "by": how,
            "price": closer.avg_fill_price if closer is not None else None,
            "deal": deal,
            "result": result,
            "at": now,
        }
        nxt = "SELL" if side == "BUY" else "RECONCILE"
        await _save(conn, run, now, phase=nxt, evidence={**evidence, side: e})
        return f"{side}_CLOSED"
    return ""


async def _reconcile(
    conn: AsyncConnection, run: Any, m: mandates.Mandate, evidence: dict[str, Any], now: datetime
) -> str:
    closed = [
        datetime.fromisoformat(str((evidence.get(s) or {}).get("closed", {}).get("at")))
        for s in SIDES
        if (evidence.get(s) or {}).get("closed")
    ]
    since = max(closed) if closed else run.requested_at
    rec = (
        await conn.execute(
            select(reconciliation_runs)
            .where(
                reconciliation_runs.c.account_id == m.account_id,
                reconciliation_runs.c.started_at >= since,
                reconciliation_runs.c.finished_at.is_not(None),
            )
            .order_by(reconciliation_runs.c.started_at)
            .limit(1)
        )
    ).first()
    if rec is None:
        return ""  # the next check comes within minutes
    evidence["reconciliation"] = {"run": str(rec.id), "status": rec.status, "at": rec.started_at}
    if rec.status != "PASSED":
        return await _finish(
            conn, run, evidence, "FAILED", "the check of our books against HFM did not pass", now
        )
    tested = [s for s in SIDES if (evidence.get(s) or {}).get("closed")]
    return await _finish(
        conn,
        run,
        evidence,
        "PASSED",
        f"{' and '.join(tested)} executed by HFM, protected by HFM's stop-loss, closed by "
        "themselves and reconciled with HFM",
        now,
    )
