"""Approved position management (V6 §1810, §1822).

Protective stops, take-profit and cancel-unfilled — only the actions in the approved plan.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, record
from cosmic.core.errors import ValidationFailedError
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.schema import incidents
from cosmic.db.schema_execution import approvals, post_trade_reviews, trade_intents
from cosmic.db.schema_trading import positions
from cosmic.market import data as md
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, is_open, raise_incident, resolve_by_key
from cosmic.trading import orders, venue_terms
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.authority import get_state
from cosmic.trading.brokers.mt5_bridge import HandClose
from cosmic.trading.context_builder import build_context
from cosmic.trading.execution.base import ExecutionBase
from cosmic.trading.execution.common import (
    ACTIVE_STATES,
    ACTOR,
    Outcome,
    S,
    StepErrors,
    account_audit,
    log,
)
from cosmic.trading.exits import propose_close
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS, Operation, account_ref
from cosmic.trading.orders import OrderRow, Source
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import (
    ManagementPlan,
    entry_available,
    get_intent,
    record_risk_run,
    sellable,
    set_intent_status,
)
from cosmic.trading.risk.context import Effect, OrderRequest, Stage
from cosmic.trading.risk.engine import evaluate

# Stop orders a newer stop may replace on a venue with one stop-loss per position.
REPLACEABLE = frozenset({S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.CANCEL_PENDING})


GAP_ACTOR = Actor.system("gap-guard")


GAP_PREFIX = "trading.stop_gapped:"
TP_PREFIX = "trading.take_profit_not_held:"  # HFM does not hold a take-profit it was sent (S85)
# Alarms that say a position's stop is not in place, by trade (S88). They end when a stop is
# active again, when the venue's own close of the trade is booked, and here, when the trade
# holds nothing any more: an alarm written a moment after that booking was never ended
# (production, 2026-11-12 14:25 UTC: a CRITICAL stood for a trade closed by hand).
STOP_ALARMS = (
    "trading.protection_degraded:",
    "trading.protection_blocked:",
    "trading.unprotected_remainder:",
)
GAP_CREATED_BY = f"{GAP_ACTOR.type.value}:{GAP_ACTOR.id}"
REPROPOSE_AFTER = timedelta(hours=1)  # the guard's exit ended unsold: the next, at most hourly
WAITING_EXIT = ("PROPOSED", "REQUIRES_REAPPROVAL", "APPROVED", "EXECUTING")


async def _exits_for(conn: AsyncConnection, entry_id: Any) -> list[Any]:
    """The exits that name this entry (a sell for a long, a buy for a short), newest first,
    whoever proposed them."""
    return list(
        await conn.execute(
            select(
                trade_intents.c.id,
                trade_intents.c.trade_code,
                trade_intents.c.status,
                trade_intents.c.created_by,
                trade_intents.c.created_at,
                trade_intents.c.details,
            )
            .where(
                trade_intents.c.effect.in_(["REDUCE", "CLOSE"]),
                trade_intents.c.details["closes"].astext.contains(str(entry_id)),
            )
            .order_by(trade_intents.c.created_at.desc())
        )
    )


def gap_key(intent_id: object) -> str:
    """The incident key of one position's gapped stop (one alert per gap, S42)."""
    return f"{GAP_PREFIX}{intent_id}"


class ProtectionManager(ExecutionBase):
    async def manage_positions(self) -> int:
        async with self.db.connect() as conn:
            intents = list(
                await conn.execute(
                    select(trade_intents).where(
                        trade_intents.c.status.in_(["EXECUTING", "EXECUTED"]),
                        trade_intents.c.id.notin_(select(post_trade_reviews.c.intent_id)),
                    )
                )
            )
        actions = 0
        errors = StepErrors()
        for intent in intents:  # one position's failure must not leave the others unprotected
            try:
                actions += await self._manage_intent(intent)
            except Exception as exc:
                errors.note("manage_intent", exc, intent_id=str(intent.id))
        # A gapped position closed meanwhile (its stop filled when the price came back, or an
        # exit sold it) is no longer managed: its alert and the exit it proposed end here.
        try:
            managed = {str(i.id) for i in intents}
            async with self.db.connect() as conn:
                gaps = [
                    r.dedup_key
                    for r in await conn.execute(
                        select(incidents.c.dedup_key).where(
                            incidents.c.dedup_key.like(f"{GAP_PREFIX}%"),
                            incidents.c.status != "RESOLVED",
                        )
                    )
                ]
                targets = [
                    r.dedup_key
                    for r in await conn.execute(
                        select(incidents.c.dedup_key).where(
                            incidents.c.dedup_key.like(f"{TP_PREFIX}%"),
                            incidents.c.status != "RESOLVED",
                        )
                    )
                ]
        except Exception as exc:
            errors.note("gap_over", exc)
            gaps, targets, managed = [], [], set()
        try:  # a trade that holds nothing any more: its stop alarms end (S88)
            async with self.db.connect() as conn:
                stale = [
                    r.dedup_key
                    for prefix in STOP_ALARMS
                    for r in await conn.execute(
                        select(incidents.c.dedup_key).where(
                            incidents.c.dedup_key.like(f"{prefix}%"),
                            incidents.c.status != "RESOLVED",
                        )
                    )
                ]
        except Exception as exc:
            errors.note("stop_alarms", exc)
            stale = []
        for key in stale:
            if key.rsplit(":", 1)[1] in managed:
                continue  # still managed: it ends when a stop is active again
            try:
                await self._stop_alarm_over(key, self.clock.now())
            except Exception as exc:
                errors.note("stop_alarm_over", exc)
        for key in targets:  # a position closed meanwhile: its take-profit alarm ends (S85)
            if key.removeprefix(TP_PREFIX) in managed:
                continue
            try:
                await self._take_profit_over(
                    key.removeprefix(TP_PREFIX), self.clock.now(), "the position was closed"
                )
            except Exception as exc:
                errors.note("take_profit_over", exc)
        for key in gaps:
            intent_id = key.removeprefix(GAP_PREFIX)
            if intent_id in managed:
                continue
            try:  # one position's alert never stops the others'
                await self._gap_over(intent_id, self.clock.now(), "the position was closed")
            except Exception as exc:
                errors.note("gap_over", exc, intent_id=intent_id)
        errors.raise_first()
        return actions

    async def _manage_intent(self, intent: Any) -> int:
        plan = ManagementPlan.model_validate(intent.management_plan)
        now = self.clock.now()
        async with self.db.connect() as conn:
            _, policy = await active_policy(conn)
            intent_orders = await orders.list_orders(conn, intent_id=intent.id)
            held = await self._intent_held(conn, intent.id)
            inst = await md.instrument_by_id(conn, intent.instrument_id)
            quote = await md.latest_quote(conn, inst.id, now)
            account = await get_account(conn, account_id=intent.account_id)
        if not self.brokers.serves(account):
            return 0  # no adapter for this account here: nothing is decided or recorded for it
        held = sellable(held, inst.lot_size)  # a remainder below one lot cannot be protected
        entry = next((o for o in intent_orders if o.purpose == "ENTRY"), None)
        if entry is None:
            return 0
        actions = 0
        # 1. Cancel an unfilled entry after the approved time limit.
        if (
            plan.cancel_unfilled_after_seconds
            and entry.status in (S.ACKNOWLEDGED, S.PARTIALLY_FILLED)
            and now >= entry.created_at + timedelta(seconds=plan.cancel_unfilled_after_seconds)
        ):
            await self.request_cancel(
                entry.id, reason="approved plan: cancel unfilled entry after time limit"
            )
            actions += 1
        if held <= ZERO:
            await self._gap_over(intent.id, now, "the position no longer holds anything")
            await self._take_profit_over(str(intent.id), now, "the position is closed")
            return actions
        if any(o.purpose == "EXIT" and o.status in ACTIVE_STATES for o in intent_orders):
            # An approved exit is replacing this position's protection; if it ends unfilled,
            # this check no longer holds and the stop is placed again on the next cycle.
            return actions
        # 1b. A close HFM itself records for this entry (S86): the owner closed the trade by
        # hand in MT5, or HFM closed it at its take-profit. Booked now as the trade's exit,
        # before anything is decided about its stop: the stop-loss left with the position, and
        # a new one could only be refused (the owner's hand close of 2026-11-11: three refused
        # stops and a false CRITICAL until the next reconciliation). The next cycle decides
        # with the books as they then are (a part still open keeps or gets its stop).
        booked = await self._book_venue_closes(account, entry.client_order_id)
        if booked:
            return actions + booked
        stops = [o for o in intent_orders if o.purpose == "STOP_LOSS"]
        tps = [o for o in intent_orders if o.purpose == "TAKE_PROFIT"]
        active_stops = [o for o in stops if o.status in ACTIVE_STATES]
        active_tps = [o for o in tps if o.status in ACTIVE_STATES]
        fresh_bid = quote.bid if quote.label is DataLabel.DELAYED else None
        fresh_ask = quote.ask if quote.label is DataLabel.DELAYED else None
        short = intent.side == "SELL"  # a short (S66): its stop above, its target below
        # The gap guard (S42): a stop-limit whose limit is above the bid cannot fill.
        if fresh_bid is not None and not short:
            try:  # never at the cost of this position's other protection
                actions += await self._gap_guard(intent, account, inst, active_stops, quote, now)
            except Exception as exc:
                log.error("gap_guard_failed", intent_id=str(intent.id), error=type(exc).__name__)
        # 2a. A take-profit HFM holds (S83, bridge 7): HFM closes the position at the target
        # itself, so the robot never sends its own; once HFM has taken it, nothing is sent and
        # the reconciliation books HFM's deal as the exit. Unknown: the robot watches it below.
        venue_tp = await self._venue_take_profit(account, entry, plan)
        await self._take_profit_alert(intent, account, venue_tp, now)
        if venue_tp == "TAKEN":  # HFM's deal was booked in 1b, or will be once it is reported
            return actions
        # 2. Take profit (monitored): cancel stops first, then an IOC limit sell at the target.
        # Where the stop lives on the position (HFM's MT5) the sell closes the position with its
        # stop still on, as an exit does: never a moment without a stop (S63).
        keeps_stop = self.brokers.for_account(account).capabilities().stops_per_position
        reached = (
            (
                fresh_ask is not None and fresh_ask <= plan.take_profit_price
                if short
                else fresh_bid is not None and fresh_bid >= plan.take_profit_price
            )
            if plan.take_profit_price and venue_tp in ("NONE", "UNSUPPORTED")
            else False
        )
        if reached and plan.take_profit_price is not None:
            if active_stops and not keeps_stop:
                for s in active_stops:
                    if s.status is not S.CANCEL_PENDING:
                        await self.request_cancel(
                            s.id, reason="approved plan: take-profit target reached"
                        )
                        actions += 1
            elif not active_tps:
                await self._place_protective(
                    intent,
                    account,
                    inst,
                    "TAKE_PROFIT",
                    held,
                    limit=plan.take_profit_price,
                    stop=None,
                    tif="IOC",
                    n=len(tps) + 1,
                    closes=(str(intent.id),) if keeps_stop else (),
                )
                actions += 1
            return actions
        # 3. Protective stop for any unprotected filled quantity.
        if plan.stop_loss is not None and not active_tps:
            protected = sum((o.remaining for o in active_stops), ZERO)
            delta = held - protected
            rejected = sum(1 for o in stops if o.status is S.REJECTED)
            if delta > ZERO:
                if rejected > policy.protective_retry_max:
                    return (
                        actions  # retry budget exhausted: escalated already, never widen the stop
                    )
                # A venue with one stop-loss per position (HFM) keeps one stop for the whole
                # entry, and gives every stop-loss deal to our newest stop order. So the new stop
                # covers the whole holding and replaces the older ones in our books.
                shared = self.brokers.for_account(account).capabilities().stops_per_position
                if shared and any(o.status not in REPLACEABLE for o in active_stops):
                    return actions  # an older stop is still on its way: wait for its answer
                quantity = held if shared else delta
                notional = quantity * plan.stop_loss.limit_price
                if inst.min_notional is not None and notional < inst.min_notional:
                    async with self.db.transaction() as conn:
                        await self._set_protection(conn, intent, "DEGRADED", now)
                        await raise_incident(
                            conn,
                            dedup_key=f"trading.unprotected_remainder:{intent.id}",
                            category=Category.TRADING,
                            alert=Alert(
                                title=f"{intent.trade_code}: {delta} unprotected "
                                "(below venue minimum)",
                                severity="WARNING",
                                what_happened=f"{delta} of the position has no protective stop "
                                "because a stop for it would be below the venue's minimum size.",
                                verified="The rest of the position is protected by active stops.",
                                uncertain="How the price moves before the remainder is closed "
                                "or protected.",
                                risk_now=f"Losses on {delta} are not limited by any order.",
                                action_taken="Protection marked DEGRADED; the stop level was "
                                "not changed.",
                                owner_action="Decide whether to close the small remainder with "
                                "an approved exit, or accept it unprotected.",
                                account=account_ref(
                                    account.alias, account.kind, account.broker_adapter
                                ),
                            ),
                            now=now,
                            detail={"delta": delta},
                        )
                    return actions
                await self._place_protective(
                    intent,
                    account,
                    inst,
                    "STOP_LOSS",
                    quantity,
                    limit=plan.stop_loss.limit_price,
                    stop=plan.stop_loss.stop_price,
                    tif="GTC",
                    n=len(stops) + 1,
                    supersede=tuple(o.id for o in active_stops) if shared else (),
                )
                actions += 1
        return actions

    async def _gap_guard(
        self,
        intent: Any,
        account: Account,
        inst: md.InstrumentView,
        active_stops: list[OrderRow],
        quote: md.QuoteView,
        now: datetime,
    ) -> int:
        """The gap guard (S42, ADR-036): a stop-limit sells only at its limit or better, so a
        stop whose limit is above the bid cannot fill until the price comes back. Act when one
        is; stand down once every stop still in force is within reach of the price again."""
        if (
            self.brokers.for_account(account).capabilities().stop_fills_at_market
            or inst.venue_key in venue_terms.MARKET_STOP_VENUES  # paper as HFM's MT5 (S54)
        ):
            return 0  # a stop that sells at market fills in a gap, only worse
        assert quote.bid is not None
        live_stops = [
            s for s in active_stops if s.status is not S.CANCEL_PENDING and s.stop_price is not None
        ]
        gapped = [s for s in live_stops if s.limit_price > quote.bid]
        if gapped:
            return await self._stop_gapped(intent, account, inst, gapped[0], quote, now)
        if not live_stops:
            return 0  # a stop being cancelled or placed again: nothing to judge yet
        key = gap_key(intent.id)
        async with self.db.transaction() as conn:
            if await is_open(conn, key):
                why = f"the price came back above the stop's limit (bid {quote.bid})"
                await self._set_protection(conn, intent, "ACTIVE", now)
                await self._end_guard_exits(conn, intent.id, now, why, only_if_sold=False)
                await resolve_by_key(conn, dedup_key=key, now=now, resolution=why)
        return 0

    async def _stop_gapped(
        self,
        intent: Any,
        account: Account,
        inst: md.InstrumentView,
        stop: OrderRow,
        quote: md.QuoteView,
        now: datetime,
    ) -> int:
        """Tell the owner once and put an exit before them, never widening the stop (V6 §1822).
        An exit already waiting for this position (the owner's own, or an earlier one) is the
        one to decide on; if the guard's exit ended unsold (rejected, expired, not filled) while
        the gap lasts, another is proposed, at most once an hour. Returns 1 when it acted."""
        bid = quote.bid
        assert bid is not None
        key = gap_key(intent.id)
        async with self.db.transaction() as conn:
            # Whatever else happens: a stop that cannot fill is no protection (a stop placed
            # again would otherwise count as ACTIVE).
            await self._set_protection(conn, intent, "DEGRADED", now)
            told = await is_open(conn, key)
            exits = await _exits_for(conn, intent.id)
            waiting = next((e for e in exits if e.status in WAITING_EXIT), None)
            if told and waiting is not None:
                return 0  # the owner has an exit to decide on
            guard = next((e for e in exits if e.created_by == GAP_CREATED_BY), None)
            if told and guard is not None and now - guard.created_at < REPROPOSE_AFTER:
                return 0  # its exit ended unsold: another in an hour at most
            exit_code, exit_note = None, ""
            if waiting is not None:
                exit_code = waiting.trade_code
            else:
                try:
                    proposal = await propose_close(
                        conn,
                        account=account,
                        inst=inst,
                        reason=(
                            f"the price fell through the stop of {intent.trade_code}: it sells "
                            f"only at {stop.limit_price} or better and the bid is {bid}"
                        ),
                        actor=GAP_ACTOR,
                        now=now,
                        origin="gap guard",
                    )
                    exit_code = proposal.trade_code if proposal.status == "PROPOSED" else None
                    if exit_code is None:
                        exit_note = "; ".join(proposal.problems)[:300]
                except ValidationFailedError as exc:  # no fresh price, nothing left to sell
                    exit_note = exc.message
            real = account.kind not in SIMULATED_ACCOUNT_KINDS
            age = "" if quote.age_seconds is None else f", {quote.age_seconds:.0f}s old"
            price = f"{bid} ({quote.label.value} from {quote.source_key}{age})"
            ask = (
                f"Approve the exit {exit_code} to sell now at about {bid}, or keep the position "
                "and its stop, which fills only if the price comes back."
                if exit_code
                else f"No exit could be proposed ({exit_note}). Close the position from the "
                "portfolio page if you want out."
            )
            await raise_incident(
                conn,
                dedup_key=key,
                category=Category.TRADING,
                alert=Alert(
                    title=f"{intent.trade_code}: the price fell through the stop",
                    severity="CRITICAL" if real else "WARNING",
                    what_happened=f"The bid, {price}, is below the stop's limit "
                    f"({stop.limit_price}). A stop-limit sells only at its limit or better, so "
                    "this one cannot fill unless the price comes back.",
                    verified=f"The stop order {stop.client_order_id} is still open as far as "
                    "this system knows.",
                    uncertain="Whether the price comes back above the limit.",
                    risk_now=("Real money: " if real else "")
                    + "the loss on this position is no longer capped by its stop.",
                    action_taken="Protection marked DEGRADED; the stop was left as approved "
                    "(never widened by the system)"
                    + (f"; exit {exit_code} is waiting for you." if exit_code else "."),
                    owner_action=ask,
                    account=account_ref(account.alias, account.kind, account.broker_adapter),
                ),
                now=now,
                detail={
                    "stop": stop.client_order_id,
                    "limit": stop.limit_price,
                    "bid": bid,
                    "bid_label": quote.label.value,
                    "exit": exit_code,
                },
            )
        return 1

    async def _end_guard_exits(
        self, conn: AsyncConnection, entry_id: Any, now: datetime, why: str, *, only_if_sold: bool
    ) -> int:
        """End the exits the guard proposed for this entry and nobody needs any more (never an
        exit the owner made). With ``only_if_sold``, only those whose entries all hold nothing:
        an exit that still closes another holding entry stays."""
        ended = 0
        for e in await _exits_for(conn, entry_id):
            if e.created_by != GAP_CREATED_BY or e.status not in (
                "PROPOSED",
                "REQUIRES_REAPPROVAL",
            ):
                continue
            closes = [uuid.UUID(str(c)) for c in (e.details or {}).get("closes", [])]
            if only_if_sold and any([await entry_available(conn, c) > ZERO for c in closes]):
                continue
            if await set_intent_status(
                conn,
                e.id,
                "BLOCKED",
                f"not needed: {why}",
                now,
                expected=("PROPOSED", "REQUIRES_REAPPROVAL"),
            ):
                ended += 1
                await record(
                    conn,
                    action="trade.gap_exit_ended",
                    actor=GAP_ACTOR,
                    occurred_at=now,
                    entity_type="trade_intent",
                    entity_id=e.id,
                    payload={"trade_code": e.trade_code, "reason": why},
                )
        return ended

    async def _gap_over(self, intent_id: Any, now: datetime, why: str) -> None:
        """A gapped position that holds nothing any more: resolve its alert, and end the exit
        the guard proposed for it once nothing it would sell is held."""
        key = gap_key(intent_id)
        async with self.db.transaction() as conn:
            if not await is_open(conn, key):
                return
            await self._end_guard_exits(conn, intent_id, now, why, only_if_sold=True)
            await resolve_by_key(conn, dedup_key=key, now=now, resolution=why)

    async def _place_protective(
        self,
        intent: Any,
        account: Account,
        inst: md.InstrumentView,
        purpose: str,
        quantity: Decimal,
        *,
        limit: Decimal,
        stop: Decimal | None,
        tif: str,
        n: int,
        supersede: tuple[uuid.UUID, ...] = (),
        closes: tuple[str, ...] = (),
    ) -> Outcome:
        """``closes``: the entries a take-profit closes with their stop still on (HFM): the
        reduce-only check (R17) then does not count that stop as a second sell of the same
        quantity, as for an approved exit; whichever fills first closes the position."""
        now = self.clock.now()
        suffix = "SL" if purpose == "STOP_LOSS" else "TP"
        client_order_id = f"ct-{intent.trade_code}-{suffix}{n}"
        async with self.db.transaction() as conn:
            authority = await get_state(conn, lock="share")
            policy_version, policy = await active_policy(conn)
            approval_id = (
                await conn.execute(
                    select(approvals.c.id).where(
                        approvals.c.intent_id == intent.id, approvals.c.status == "CONSUMED"
                    )
                )
            ).scalar_one_or_none()
            if approval_id is None:
                return Outcome(
                    "BLOCKED", "no consumed approval authorises management of this intent"
                )
            lot = inst.lot_size or Decimal("0.00000001")
            qty = (quantity // lot) * lot
            # Protection takes the other side of the entry: a sell for a long, a buy for a short.
            side = "BUY" if intent.side == "SELL" else "SELL"
            request = OrderRequest(
                account_alias=account.alias,
                instrument_key=inst.canonical_key,
                side=side,
                effect=Effect.REDUCE,
                quantity=qty,
                order_type="LIMIT",
                limit_price=limit,
                time_in_force=tif,
                price_band_low=limit,
                price_band_high=limit,
                max_slippage_bps=ZERO,
                closes=closes,
            )
            # The stops this one replaces end first, so the reduce-only check (R17) counts the
            # new stop alone; if any check fails they are kept as they were.
            replaced = await conn.begin_nested()
            ended = [await self._supersede(conn, i, client_order_id, now) for i in supersede]
            ctx = await build_context(
                conn,
                account=account,
                inst=inst,
                request=request,
                stage=Stage.PROTECTIVE,
                mode=authority.mode,
                policy_version=policy_version,
                now=now,
            )
            decision = evaluate(ctx, policy)
            if decision.passed:
                await replaced.commit()
                for old in ended:
                    if old is not None:
                        await self._after_terminal(conn, old, now)
            else:
                await replaced.rollback()
            await record_risk_run(
                conn,
                ctx=ctx,
                decision=decision,
                account_id=account.id,
                intent_id=intent.id,
                order_id=None,
                now=now,
            )
            if not decision.passed:
                await self._set_protection(conn, intent, "DEGRADED", now)
                await raise_incident(
                    conn,
                    dedup_key=f"trading.protection_blocked:{intent.id}",
                    category=Category.TRADING,
                    alert=Alert(
                        title=f"{intent.trade_code}: protective order blocked by risk checks",
                        severity="CRITICAL",
                        what_happened="The approved protective order could not be placed: "
                        f"{decision.plain_summary()}",
                        verified="The position exists; the protective order was not sent.",
                        uncertain="Whether the blocking condition (data, account state, mode) "
                        "clears by itself.",
                        risk_now="The position is NOT protected by an active stop.",
                        action_taken="Protection marked DEGRADED; it is retried each cycle "
                        "while the plan allows; the stop is never widened.",
                        owner_action="Check the failed check on the order page; consider an "
                        "approved exit if the condition persists.",
                        account=account_ref(account.alias, account.kind, account.broker_adapter),
                    ),
                    now=now,
                    detail={"summary": decision.plain_summary()},
                )
                return Outcome("BLOCKED", decision.plain_summary())
            order_id = await orders.create(
                conn,
                now=now,
                detail={"plan": purpose},
                values={
                    "intent_id": intent.id,
                    "approval_id": approval_id,
                    "purpose": purpose,
                    "account_id": account.id,
                    "instrument_id": inst.id,
                    "client_order_id": client_order_id,
                    "side": side,
                    "order_type": "STOP_LIMIT" if stop is not None else "LIMIT",
                    "quantity": qty,
                    "limit_price": limit,
                    "stop_price": stop,
                    "time_in_force": tif,
                    "expires_at": None,
                    "authority": "PROTECTIVE_MANAGEMENT",
                    "reduce_only": True,
                    "is_simulated": account.kind in SIMULATED_ACCOUNT_KINDS,
                },
            )
            o = await orders.get_order(conn, order_id)
            o = await orders.transition(conn, o, S.VALIDATED, source=Source.LOCAL, now=now)
            await orders.transition(
                conn,
                o,
                S.AUTHORIZED,
                source=Source.LOCAL,
                now=now,
                detail={"authorised_by": "approved management plan", "approval_id": approval_id},
            )
            if purpose == "STOP_LOSS":
                await self._set_protection(conn, intent, "PENDING", now)
            await record(
                conn,
                action="trade.protective_authorized",
                actor=ACTOR,
                occurred_at=now,
                entity_type="order",
                entity_id=order_id,
                payload={
                    "intent_id": intent.id,
                    "purpose": purpose,
                    "quantity": qty,
                    **await account_audit(conn, intent.account_id),
                },
            )
        outcome = await self._transmit(
            order_id, account, intent_operation=Operation.REDUCE_EXPOSURE
        )
        if purpose == "STOP_LOSS" and outcome.code == "ACKNOWLEDGED":
            async with self.db.transaction() as conn:
                await self._set_protection(conn, intent, "ACTIVE", self.clock.now())
                for prefix in STOP_ALARMS[:2]:  # the stop is in place again (S88)
                    await resolve_by_key(
                        conn,
                        dedup_key=f"{prefix}{intent.id}",
                        now=self.clock.now(),
                        resolution=f"a protective stop is active again ({client_order_id})",
                    )
        return outcome

    async def _stop_alarm_over(self, key: str, now: datetime) -> None:
        """End a stop alarm of a trade that is no longer managed, once its books say it holds
        nothing (checked here, not assumed). Anything still held keeps its alarm."""
        try:
            intent_id = uuid.UUID(key.rsplit(":", 1)[1])
        except ValueError:
            return
        async with self.db.transaction() as conn:
            if await self._intent_held(conn, intent_id) > ZERO:
                return
            await resolve_by_key(
                conn,
                dedup_key=key,
                now=now,
                resolution="the trade is closed: nothing of it is held any more",
            )

    async def _supersede(
        self, conn: AsyncConnection, order_id: uuid.UUID, by: str, now: datetime
    ) -> OrderRow | None:
        """An older stop of an entry on a venue with one stop-loss per position: the venue's
        stop-loss stays where it is, now held by the new stop order, so nothing is sent."""
        old = await orders.get_order(conn, order_id, lock=True)
        if old.status not in REPLACEABLE:
            return None
        return await orders.transition(
            conn,
            old,
            S.CANCELLED,
            source=Source.RECONCILE,
            now=now,
            detail={"reason": f"superseded by {by}: one stop-loss covers the whole entry"},
        )

    async def _set_protection(
        self, conn: AsyncConnection, intent: Any, state: str, now: datetime
    ) -> None:
        await conn.execute(
            update(positions)
            .where(
                positions.c.account_id == intent.account_id,
                positions.c.instrument_id == intent.instrument_id,
            )
            .values(protection_state=state, updated_at=now)
        )

    async def _venue_take_profit(self, account: Account, entry: OrderRow, plan: Any) -> str:
        """Whether the venue itself holds this entry's take-profit: HELD, TAKEN, NONE (a
        position is open there without it), PENDING (no position in its report yet) or
        UNSUPPORTED (it cannot hold one, or cannot be asked now: the robot watches the target)."""
        broker = self.brokers.for_account(account)
        state = getattr(broker, "take_profit_state", None)
        if plan.take_profit_price is None or state is None:
            return "UNSUPPORTED"
        if not broker.capabilities().attached_take_profit:
            return "UNSUPPORTED"
        try:
            found: str = await state(entry.client_order_id, plan.take_profit_price)
        except Exception as exc:  # unknown: the robot keeps watching the target itself
            log.warning(
                "venue_take_profit_unknown", order=entry.client_order_id, error=type(exc).__name__
            )
            return "UNSUPPORTED"
        return found

    async def _take_profit_alert(
        self, intent: Any, account: Account, state: str, now: datetime
    ) -> None:
        """The owner's rule (2026-11-11): HFM must hold both protections of every trade. A
        position open at HFM without the take-profit it was sent with is a CRITICAL incident
        (the gold robot then opens nothing new until the owner resumes it); the robot watches
        the target meanwhile, and the stop-loss is checked on its own. Ended once HFM holds it
        or has closed the position."""
        key = f"{TP_PREFIX}{intent.id}"
        if state == "NONE":
            async with self.db.transaction() as conn:
                await raise_incident(
                    conn,
                    dedup_key=key,
                    category=Category.TRADING,
                    alert=Alert(
                        title=f"{intent.trade_code}: HFM does not hold the take-profit",
                        severity="CRITICAL",
                        what_happened="The position is open at HFM, but its take-profit there "
                        "is missing or is not the planned one.",
                        verified="HFM's own position record, from the bridge's report.",
                        uncertain="Why HFM does not show it: it was sent inside the order.",
                        risk_now="If this server were down, the trade would not close at its "
                        "target. Its stop-loss is not affected by this.",
                        action_taken="The robot watches the target itself and closes at "
                        "market there; the gold robot opens no new trade until you resume it.",
                        owner_action="Look at the position in MT5 (the T/P column). Resume "
                        "the robot on My Gold Auto-Trader when you have.",
                        account=account_ref(account.alias, account.kind, account.broker_adapter),
                    ),
                    now=now,
                    detail={"intent_id": str(intent.id)},
                    context={"trade_code": intent.trade_code},
                )
        elif state in ("HELD", "TAKEN"):
            await self._take_profit_over(
                str(intent.id),
                now,
                "HFM holds the take-profit" if state == "HELD" else "HFM closed it at the target",
            )

    async def _take_profit_over(self, intent_id: str, now: datetime, why: str) -> None:
        key = f"{TP_PREFIX}{intent_id}"
        async with self.db.connect() as conn:
            if not await is_open(conn, key):
                return
        async with self.db.transaction() as conn:
            await resolve_by_key(conn, dedup_key=key, now=now, resolution=why)

    async def _venue_closes(self, account: Account, entry_coid: str) -> list[HandClose]:
        closes = getattr(self.brokers.for_account(account), "hand_closes", None)
        if closes is None:
            return []
        found: list[HandClose] = await closes(account.id)
        return [hc for hc in found if hc.entry_coid == entry_coid]

    async def _book_venue_closes(self, account: Account, entry_coid: str) -> int:
        """HFM's own deals that closed this entry's position (by hand in MT5, or at its
        take-profit) are booked as the trade's exit now, not only at the next reconciliation
        (the same booking, once: S71's). How many were new to the books."""
        from cosmic.trading.reconcile import adopt_venue_close  # it builds on this package

        try:
            found = await self._venue_closes(account, entry_coid)
        except Exception as exc:  # cannot be read now: the reconciliation books it later
            log.warning("venue_close_unbooked", order=entry_coid, error=type(exc).__name__)
            return 0
        booked = 0
        for hc in found:
            booked += int(await adopt_venue_close(self.db, self.clock, account, hc))
        return booked

    async def _protection_failed(
        self, conn: AsyncConnection, o: OrderRow, reason: str, now: datetime
    ) -> None:
        intent = await get_intent(conn, o.intent_id)
        account = await get_account(conn, account_id=o.account_id)
        await self._set_protection(conn, intent, "DEGRADED", now)
        await raise_incident(
            conn,
            dedup_key=f"trading.protection_degraded:{o.intent_id}",
            category=Category.TRADING,
            alert=Alert(
                title=f"{intent.trade_code}: protective order failed",
                severity="CRITICAL",
                what_happened=f"Protective order {o.client_order_id} failed: {reason}.",
                verified="The position is NOT protected by an active stop.",
                uncertain="Whether the next retry will be accepted by the venue.",
                risk_now="Losses on this position are not limited by any order.",
                action_taken="Protection marked DEGRADED; a bounded number of identical retries; "
                "the stop level is never widened.",
                owner_action="Watch the order page; if retries are exhausted, decide on an "
                "approved exit or re-proposal with a new stop.",
                account=account_ref(account.alias, account.kind, account.broker_adapter),
            ),
            now=now,
            detail={"order": o.client_order_id, "reason": reason},
            notify_kind="protection_degraded",
            context={"trade_code": intent.trade_code},
        )
