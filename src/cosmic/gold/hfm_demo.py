"""My Gold Auto-Trader in HFM Demo mode (S64, ADR-037 addendum): the same reading of HFM's
candles as the simulation, sent to the owner's HFM demo account through the existing order path.
No new engine and no new authority: a proposal is made under the owner's mandate (rule
``gold_reading``), the mandate approves it (Automatic) or the owner is asked in the portal (Ask
me first), the risk checks run before it is sent, the bridge sends it with its stop-loss, and the
reconciliation compares the books with HFM's.

- **BUY** when the reading says BUY: HFM's whole ounces (0.01 lot = 1 ounce), sized so the loss at
  the stop, with the risk policy's fee assumption counted twice, stays within the mandate's loss a
  trade. When even 1 ounce would lose more, it waits and says from which amount HFM's smallest
  trade fits at that risk (the owner, 2026-11-04: never a raised amount or risk).
- **SELL** (S66): a genuine short, sold at HFM's bid with its stop-loss above and its
  take-profit below, when the risk policy allows shorts on HFM (the owner's "buy and sell") and
  the bridge is version 5; otherwise it waits and says why.
- **The rule's exit:** a long is closed when the hourly trend turns down, a short when it turns
  up, with the stop-loss still on until HFM has closed it (S63).
- Once per 1-minute candle and mandate (``gold_hfm_decisions``), so a restart never proposes
  twice. Virtual money only: the mandate is granted only on the HFM demo account.

**The 1-minute rule (S85, rule ``gold_m1``; the owner's directive of 2026-11-11)** runs in the
same engine and replaces the reading above for robots started since:
- at each 1-minute candle the model's probability that a buy, and that a short, reaches its
  take-profit before its stop-loss (``gold.probability``, stored by ``gold.predictions``); no
  reliable estimate is NO TRADE;
- a setup (the 5- and 15-minute trends agree, the 1-minute momentum turns with them) is decided
  by the owner's rules: 50 % or more opens under the mandate (Automatic); below that it opens
  only under an approval the owner gave in advance for that one trade; below 30 % such a trade
  is a PRACTICE trade, said so everywhere;
- one fixed shape: stop-loss 1.90 and take-profit 4.00 an ounce from the price, entered now
  within 0.10 or not at all; whole ounces within the mandate's loss a trade, or none;
- the stop-loss and the take-profit both travel in the entry order and HFM holds them (bridge
  7); with an older bridge it opens nothing. No rule exit: HFM's stop or target closes it, or
  the robot's shutdown. A position whose protection HFM does not hold pauses the robot."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_gold import gold_hfm_decisions, gold_hfm_selftests
from cosmic.db.schema_trading import bridge_state
from cosmic.gold import analysis, controls, predictions, probability, second_opinion, sim
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.ops.incidents import is_open
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.brokers.mt5_bridge import ATTACHED_TP_VERSION, SHORTS_VERSION, bridge_version
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate, create_candidate, entry_available

ACTOR = Actor.worker("gold_trader")
GOLD = "CFD:XAU/USD@HFM"
MARKUP = Decimal("0.001")  # a buy is a marketable limit at the ask + 0.1 %, immediate or cancel
BAND = Decimal("0.005")
TTL = timedelta(minutes=5)  # an asked trade lapses, as in the simulation
WAITING_EXIT = ("PROPOSED", "REQUIRES_REAPPROVAL", "APPROVED", "EXECUTING")
NO_SHORTS = "WAIT — selling (a short) is off: you chose buying only"
OLD_BRIDGE = (
    "WAIT — the HFM bridge on the Windows computer must be version 5 to sell (open a short); it "
    "is version {version}"
)
NO_TP_BRIDGE = (
    "NO TRADE — the HFM bridge on the Windows computer must be version {need} so that HFM holds "
    "the take-profit with the stop-loss; it is version {version}"
)
# The incidents that mean HFM does not hold a position's protection (trading.execution).
UNPROTECTED = ("trading.protection_degraded:", "trading.take_profit_not_held:")


def _tick(value: Decimal, tick: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    return (value / tick).to_integral_value(rounding) * tick


def size(
    max_loss: Decimal,
    risk_pct: Decimal | None,
    money: Decimal | None,
    entry: Decimal,
    stop: Decimal,
    fee_bps: Decimal,
    lot: Decimal,
) -> tuple[Decimal, str | None]:
    """Whole lots of HFM's gold whose loss at the stop (with the fee both ways, as the risk check
    counts it) stays within ``max_loss``; or none, and why, in the owner's words. A buy's stop
    is below the entry, a short's above: the distance is the same arithmetic."""
    per_unit = abs(entry - stop) + 2 * entry * fee_bps / 10_000
    if per_unit <= 0 or max_loss <= 0:
        return ZERO, "no stop distance or no money to risk"
    qty = (max_loss / per_unit / lot).to_integral_value(ROUND_DOWN) * lot
    if qty >= lot:
        return qty, None
    smallest = (lot * per_unit).quantize(Decimal("0.01"), ROUND_CEILING)
    fits = (
        f" At {risk_pct.normalize():f} %, HFM's demo trades this from an amount of "
        f"{(smallest * 100 / risk_pct).quantize(Decimal('0.01'), ROUND_CEILING):,.2f}."
        if risk_pct
        else ""
    )
    of = f" ({risk_pct.normalize():f} % of {money:,.2f})" if risk_pct and money else ""
    return ZERO, (
        "WAIT — Your selected trading amount is too small for this opportunity at your chosen "
        f"risk level: HFM's smallest gold trade ({lot.normalize():f} ounce = 0.01 lot) would lose "
        f"about {smallest:,.2f} at its stop, more than the {max_loss:,.2f} you allow{of}.{fits}"
    )


async def gold_mandates(conn: AsyncConnection, now: datetime) -> list[mandates.Mandate]:
    return [
        m
        for m in await mandates.active_mandates(conn, now)
        if m.strategy_key in mandates.GOLD_HFM_RULES
    ]


async def _held_entries(
    conn: AsyncConnection,
    account: Account,
    instrument_id: uuid.UUID,
    mandate_ids: set[str],
    side: str | None = None,
) -> list[tuple[uuid.UUID, Decimal]]:
    """This rule's filled entries that still hold gold, oldest first: buys (``side`` BUY),
    shorts (SELL), or both (None)."""
    q = select(trade_intents.c.id, trade_intents.c.details).where(
        trade_intents.c.account_id == account.id,
        trade_intents.c.instrument_id == instrument_id,
        trade_intents.c.effect.in_(["OPEN", "INCREASE"]),
        trade_intents.c.status.in_(["EXECUTING", "EXECUTED"]),
    )
    if side is not None:
        q = q.where(trade_intents.c.side == side)
    out = []
    for r in await conn.execute(q.order_by(trade_intents.c.created_at)):
        if str((r.details or {}).get("mandate_id")) not in mandate_ids:
            continue
        held = await entry_available(conn, r.id)
        if held > 0:
            out.append((r.id, held))
    return out


async def _in_flight(conn: AsyncConnection, account: Account, instrument_id: uuid.UUID) -> bool:
    """A buy or a sell of this gold on its way (proposed, approved or executing)."""
    return (
        await conn.scalar(
            select(trade_intents.c.id)
            .where(
                trade_intents.c.account_id == account.id,
                trade_intents.c.instrument_id == instrument_id,
                trade_intents.c.status.in_(WAITING_EXIT),
            )
            .limit(1)
        )
    ) is not None


async def decide(db: Database, now: datetime) -> list[str]:
    """Once per new 1-minute candle, for each active gold-reading mandate."""
    out: list[str] = []
    async with db.connect() as conn:
        found = await gold_mandates(conn, now)
    for m in found:
        async with db.transaction() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"gold-hfm:{m.id}"},
            )
            out.append(await _decide_one(conn, m, now))
    return [x for x in out if x]


async def _decide_one(conn: AsyncConnection, m: mandates.Mandate, now: datetime) -> str:
    current = await mandates.get_mandate(conn, m.id)
    if not current.active(now):
        return ""
    reading = await sim.reading_now(conn, now)
    if reading.bar is None:
        return ""
    done = await conn.scalar(
        select(gold_hfm_decisions.c.id).where(
            gold_hfm_decisions.c.mandate_id == m.id, gold_hfm_decisions.c.bar == reading.bar
        )
    )
    if done is not None:
        return ""
    price = await sim.price_now(conn, now)
    reasons = list(reading.reasons)
    action, ounces, intent_id = "WAIT", None, None
    decision, stop_distance = reading.decision, reading.stop_distance
    m1_rule = m.strategy_key == mandates.GOLD_M1
    if m1_rule:  # the 1-minute rule (S85) decides by its own estimate, not the hourly reading
        reasons, decision, stop_distance = [], analysis.WAIT, probability.MODEL.risk
    testing = await conn.scalar(  # the owner's self-test runs alone (S67)
        select(gold_hfm_selftests.c.id).where(
            gold_hfm_selftests.c.mandate_id == m.id, gold_hfm_selftests.c.finished_at.is_(None)
        )
    )
    account = await get_account(conn, account_id=m.account_id)
    inst = await md.instrument_by_key(conn, GOLD)

    async def record() -> str:
        await conn.execute(
            gold_hfm_decisions.insert().values(
                id=uuid7(),
                mandate_id=m.id,
                bar=reading.bar,
                at=now,
                decision=decision,
                action=action,
                reasons=reasons,
                bid=price.bid if price else None,
                ask=price.ask if price else None,
                ounces=ounces,
                stop_distance=stop_distance,
                intent_id=intent_id,
            )
        )
        return f"{m.id}:{action}"

    if price is None or inst.tick_size is None:
        reasons.append("WAIT — no current HFM price")
        return await record()
    if testing is not None:
        reasons.append("WAIT — the HFM demo self-test is running; trading starts after it")
        return await record()
    longs = await _held_entries(conn, account, inst.id, {str(m.id)}, "BUY")
    shorts = await _held_entries(conn, account, inst.id, {str(m.id)}, "SELL")
    held = longs + shorts
    in_flight = await _in_flight(conn, account, inst.id)
    # The rule's exit: the hourly trend has turned against the position (down under a long, up
    # over a short); the stop-loss stays on until HFM has closed it.
    trend = reading.frames["1h"].trend
    against = longs if trend == analysis.DOWN else shorts if trend == analysis.UP else []
    if m1_rule:  # no rule exit: HFM's stop-loss or take-profit closes a 1-minute trade
        against = []
        broken = await _unprotected(conn, held)
        running = await controls.get(conn, m.id)
        if broken is not None and running is not None and running.status == controls.RUNNING:
            await controls.set_status(
                conn, m.id, controls.PAUSED, broken, now=now, actor=controls.ACTOR
            )
    if against and not in_flight:
        qty = sum((h for _, h in against), ZERO)
        long = against is longs
        candidate = _exit(
            account,
            inst.tick_size,
            price.bid if long else price.ask,
            qty,
            [e for e, _ in against],
            reading,
            long=long,
        )
        intent_id, why = await _propose(conn, m, candidate, now)
        action = "EXIT" if why is None else "REFUSED"
        reasons.append(
            f"closing the {'buy' if long else 'short'} of {qty.normalize():f} ounce: the hourly "
            f"trend turned {trend.lower()}" + (f" (refused: {why})" if why else "")
        )
        return await record()
    # The owner's rules and goals (S79): a robot started since has controls; one without trades
    # as before.
    ctl = await controls.get(conn, m.id)
    res: controls.Results | None = None
    if ctl is not None:
        res = await controls.results(conn, m, ctl, now, held, price.bid, price.ask)
        verdict = controls.judge(m, ctl, res)
        if verdict is not None:
            await controls.set_status(
                conn, m.id, verdict[0], verdict[1], now=now, actor=controls.ACTOR
            )
            ctl = await controls.get(conn, m.id)
            assert ctl is not None
        if ctl.status in controls.SHUT:
            await controls.withdraw_entries(conn, m, ctl.status_reason, now)
            side = longs or shorts
            if side and not in_flight:
                qty = sum((h for _, h in side), ZERO)
                long = side is longs
                candidate = _exit(
                    account,
                    inst.tick_size,
                    price.bid if long else price.ask,
                    qty,
                    [e for e, _ in side],
                    reading,
                    long=long,
                    why=ctl.status_reason,
                )
                intent_id, refused = await _propose(conn, m, candidate, now)
                action = "EXIT" if refused is None else "REFUSED"
                reasons.append(
                    f"closing the {'buy' if long else 'short'} of {qty.normalize():f} ounce at "
                    f"market: {ctl.status_reason}" + (f" (refused: {refused})" if refused else "")
                )
                return await record()
            if not held and not in_flight:
                await controls.record_final(conn, m, res.session, now)
            reasons.append(
                f"WAIT — {ctl.status.replace('_', ' ').lower()}: {ctl.status_reason}; it trades "
                "again only when you resume it"
            )
            return await record()
        if ctl.status == controls.PAUSED:
            reasons.append(f"WAIT — paused: {ctl.status_reason}")
            return await record()
    if m1_rule:
        decision, action, ounces, intent_id = await _decide_m1(
            conn, m, account, inst, price, now, held, in_flight, res, reasons
        )
        return await record()
    if reading.decision == analysis.WAIT:
        return await record()
    _, policy = await active_policy(conn)
    short = reading.decision == analysis.SELL
    if short:
        version = await _bridge_version(conn)
        if not policy.shorts_allowed(inst.venue_key, inst.asset_class):
            reasons.append(NO_SHORTS)
            return await record()
        if version < SHORTS_VERSION:
            reasons.append(OLD_BRIDGE.format(version=version))
            return await record()
    if in_flight:
        reasons.append("WAIT — a trade of this gold is already on its way")
        return await record()
    stopped = await second_opinion.veto(conn, reading.decision, now)  # it may only stop (S68)
    if stopped is not None:
        reasons.append(stopped)
        return await record()
    if len(held) >= m.max_open_positions:
        reasons.append(
            f"WAIT — {len(held)} trade(s) open: the most at once is {m.max_open_positions}"
        )
        return await record()
    assert reading.stop_distance is not None
    tick = inst.tick_size
    if short:  # sold at the bid or better; the stop above, the target below
        entry = _tick(price.bid * (1 - MARKUP), tick)
        stop = _tick(price.bid + reading.stop_distance, tick, ROUND_CEILING)
    else:
        entry = _tick(price.ask * (1 + MARKUP), tick, ROUND_CEILING)
        stop = _tick(price.ask - reading.stop_distance, tick)
    lot = inst.lot_size or Decimal(1)
    qty, why = size(
        m.max_loss_per_trade,
        m.risk_pct,
        m.capital,
        entry,
        stop,
        policy.fee_bps_for(inst.venue_key),
        lot,
    )
    if why is not None:
        reasons.append(why)
        await notify_owners(  # once a day, not every minute
            conn,
            kind="gold_sim",
            payload={"text": f"HFM DEMO: a {reading.decision} opportunity was not taken. {why}"},
            now=now,
            dedup_key=f"gold-hfm:{m.id}:too-small:{now:%Y-%m-%d}",
        )
        return await record()
    if ctl is not None and res is not None:
        # The owner's rules (S79): the whole risk of the trade counts its slippage allowance
        # (the limit margin) and the fee both ways; the day ceiling holds; the target is the
        # owner's multiple of that whole risk (2: risk 2 USD for 4).
        per_unit = abs(entry - stop) + 2 * entry * policy.fee_bps_for(inst.venue_key) / 10_000
        ceiling = controls.day_room(m, res, (qty * per_unit).quantize(Decimal("0.01")))
        if ceiling is not None:
            reasons.append(ceiling)
            return await record()
        reach = ctl.take_profit_r * per_unit
        take_profit = (
            _tick(price.bid - reach, tick, ROUND_CEILING)
            if short
            else _tick(price.ask + reach, tick)
        )
    else:
        take_profit = (
            _tick(price.bid - 2 * reading.stop_distance, tick, ROUND_CEILING)
            if short
            else _tick(price.ask + 2 * reading.stop_distance, tick)
        )
    candidate = _entry(
        account, tick, price.bid, price.ask, entry, stop, take_profit, qty, reading, short=short
    )
    ounces = qty
    intent_id, refused = await _propose(conn, m, candidate, now)
    action = "PROPOSED" if refused is None else "REFUSED"
    reasons.append(
        f"{'SELL (short)' if short else 'BUY'} {qty.normalize():f} ounce at "
        f"{'least' if short else 'most'} {entry}, stop-loss {stop}, take-profit {take_profit}: "
        + (
            "approved under your mandate and sent to HFM's demo"
            if refused is None and m.mode == "AUTO"
            else "asked: approve it in the portal (Approvals)"
            if refused is None
            else f"refused: {refused}"
        )
    )
    return await record()


async def _propose(
    conn: AsyncConnection,
    m: mandates.Mandate,
    candidate: Candidate,
    now: datetime,
    extra: dict[str, Any] | None = None,
    *,
    timeframe: mandates.Timeframe = mandates.HOURLY,
    approve: bool | None = None,
) -> tuple[uuid.UUID | None, str | None]:
    """The proposal through every risk check, then the mandate approves it (Automatic) or the
    owner is asked (Ask me first). ``extra``: recorded on the intent (the self-test's marker).
    ``timeframe``: the rule's bars (the daily robot's are daily, S75). ``approve``: the 1-minute
    rule's own decision (S85): True when the trade is automatic by the owner's rules or the
    owner approved it in advance; the mandate's limits are checked all the same."""
    result = await create_candidate(
        conn,
        candidate=candidate,
        actor=ACTOR,
        now=now,
        mandate_id=m.id,
        timeframe=timeframe,
        ttl=TTL,
        extra_details=extra,
    )
    if result.status != "PROPOSED":
        return result.intent_id, "; ".join(result.problems)[:300] or result.status
    asks = m.mode == "ASK" if approve is None else not approve
    decide = mandates.ask if asks else mandates.approve
    approved, reason = await decide(conn, intent_id=result.intent_id, mandate=m, now=now)
    return result.intent_id, None if approved else reason


async def _unprotected(conn: AsyncConnection, held: list[tuple[uuid.UUID, Decimal]]) -> str | None:
    """Why this robot must stop opening trades: HFM does not hold the stop-loss or the
    take-profit of one of its open trades (an open incident of the execution's protection
    step, which reads HFM's own position record). None when every one is protected."""
    for intent_id, _ in held:
        for prefix in UNPROTECTED:
            if await is_open(conn, f"{prefix}{intent_id}"):
                code = await conn.scalar(
                    select(trade_intents.c.trade_code).where(trade_intents.c.id == intent_id)
                )
                what = "stop-loss" if "degraded" in prefix else "take-profit"
                return (
                    f"HFM does not hold the {what} of {code}: no new trade until you have "
                    "looked at it and resumed the robot"
                )
    return None


async def _decide_m1(
    conn: AsyncConnection,
    m: mandates.Mandate,
    account: Account,
    inst: Any,
    price: sim.Price,
    now: datetime,
    held: list[tuple[uuid.UUID, Decimal]],
    in_flight: bool,
    res: controls.Results | None,
    reasons: list[str],
) -> tuple[str, str, Decimal | None, uuid.UUID | None]:
    """The 1-minute rule at this candle: (decision, action, ounces, intent). Every reason is
    appended to ``reasons``, in the owner's words."""
    model = probability.MODEL
    pred = await predictions.ensure(conn, now, model)
    if pred.p_buy is None or pred.p_sell is None:
        reasons.append(f"NO TRADE — no reliable estimate: {pred.why}")
        return analysis.WAIT, "WAIT", None, None
    reasons.append(
        "Probability that the take-profit comes before the stop-loss: "
        f"BUY {pred.p_buy:.1%}, SELL {pred.p_sell:.1%} (model {model.version}; break-even "
        f"{model.break_even:.1%}; M15 {pred.features.get('m15')}, M5 {pred.features.get('m5')})"
    )
    if pred.setup == probability.WAIT:
        reasons.append(
            "WAIT — no 1-minute setup on this candle: the 5- and 15-minute trends must agree "
            "and the 1-minute momentum turn with them"
        )
        return analysis.WAIT, "WAIT", None, None
    side = pred.setup
    p = pred.of(side)
    assert p is not None
    short = side == analysis.SELL

    def no(why: str) -> tuple[str, str, Decimal | None, uuid.UUID | None]:
        reasons.append(why)
        return side, "WAIT", None, None

    _, policy = await active_policy(conn)
    if short and not policy.shorts_allowed(inst.venue_key, inst.asset_class):
        return no(NO_SHORTS)
    version = await _bridge_version(conn)
    if version < ATTACHED_TP_VERSION:
        return no(NO_TP_BRIDGE.format(need=ATTACHED_TP_VERSION, version=version))
    if in_flight:
        return no("WAIT — a trade of this gold is already on its way")
    if len(held) >= m.max_open_positions:
        return no(f"WAIT — {len(held)} trade(s) open: the most at once is {m.max_open_positions}")
    stopped = await second_opinion.veto(conn, side, now)  # the AI may only stop a trade (S68)
    if stopped is not None:
        return no(stopped)
    found = probability.levels(model, side, price.bid, price.ask, inst.tick_size)
    if isinstance(found, str):
        return no(f"NO TRADE — {found}")
    fee_bps = policy.fee_bps_for(inst.venue_key)
    fee = (2 * found.limit * fee_bps / 10_000).quantize(Decimal("0.01"), ROUND_CEILING)
    if fee > model.max_fee:
        return no(
            f"NO TRADE — the commission counted for HFM ({fee:.2f} an ounce, both ways) is more "
            "than the estimate was measured with"
        )
    qty, why = size(
        m.max_loss_per_trade,
        m.risk_pct,
        m.capital,
        found.limit,
        found.stop,
        fee_bps,
        inst.lot_size or Decimal(1),
    )
    if why is not None:
        await notify_owners(  # once a day, not every minute
            conn,
            kind="gold_sim",
            payload={"text": f"HFM DEMO: a {side} setup was not taken. {why}"},
            now=now,
            dedup_key=f"gold-hfm:{m.id}:too-small:{now:%Y-%m-%d}",
        )
        return no(why)
    eco = probability.economics(model, qty, fee, p)
    if res is not None:
        ceiling = controls.day_room(m, res, eco.max_loss)
        if ceiling is not None:
            return no(ceiling)
    # The owner's decision rules (2026-11-11): 50 % or more opens by itself; anything below
    # opens only under their own approval, given in advance for this one trade.
    band = probability.band(p)
    ticket = None
    if not (band == probability.AUTO and m.mode == "AUTO"):
        ticket = await predictions.waiting(conn, m.id, now)
        if ticket is None and band == probability.REJECT:
            return no(
                f"REJECTED — {side} at {p:.1%} is below your 30 % threshold: not opened. A "
                "practice trade opens only after you approve the next one on the dashboard"
            )
        if ticket is None:
            await notify_owners(  # at most once an hour
                conn,
                kind="gold_sim",
                payload={
                    "text": f"HFM DEMO: a {side} setup at {p:.1%} needed your approval and none "
                    "was waiting. Approve the next trade on the dashboard to take the next one."
                },
                now=now,
                dedup_key=f"gold-hfm:{m.id}:ask:{now:%Y-%m-%d-%H}",
            )
            return no(
                f"ASK — {side} at {p:.1%} needs your approval, and none is waiting: approve the "
                "next trade on the dashboard to take the next one"
            )
    practice = band == probability.REJECT
    candidate = _m1_entry(account, price, found, qty, eco, pred, model, practice=practice)
    extra = {
        "rule": mandates.GOLD_M1,
        "model": model.version,
        "probability": str(p),
        "band": band,
        "practice": practice,
        "expected": str(eco.expected),
        "ticket": str(ticket.id) if ticket is not None else None,
    }
    intent_id, refused = await _propose(conn, m, candidate, now, extra, approve=True)
    if ticket is not None:
        how = predictions.USED if refused is None else predictions.REFUSED
        await predictions.end(conn, ticket.id, how, now, intent_id)
    reasons.append(
        f"{'PRACTICE ' if practice else ''}{'SELL (short)' if short else 'BUY'} "
        f"{qty.normalize():f} ounce at {'least' if short else 'most'} {found.limit}, stop-loss "
        f"{found.stop}, take-profit {found.take_profit}, both held by HFM; probability {p:.1%} "
        f"that the take-profit comes first ({1 - p:.1%} the stop-loss); at most {eco.max_loss} "
        f"to lose, {eco.net_at_target} to win (risk to reward 1:{eco.reward_to_risk.normalize():f}"
        f"); such a trade's average result {eco.expected:+.2f}: "
        + (
            f"refused: {refused}"
            if refused is not None
            else "sent to HFM's demo under the approval you gave in advance"
            if ticket is not None
            else "opened by your rule (50 % or more)"
        )
    )
    return side, "PROPOSED" if refused is None else "REFUSED", qty, intent_id


def _m1_entry(
    account: Account,
    price: sim.Price,
    lv: probability.Levels,
    qty: Decimal,
    eco: probability.Economics,
    pred: probability.Prediction,
    model: probability.Model,
    *,
    practice: bool,
) -> Candidate:
    """The 1-minute rule's entry: now within 0.10 of the price or not at all, its stop-loss and
    take-profit held by HFM with the position."""
    short = lv.side == analysis.SELL
    verb = "sell (open a short of)" if short else "buy"
    label = (
        f"PRACTICE trade, below your 30 % threshold (probability {eco.probability:.1%} that the "
        f"take-profit comes first, {1 - eco.probability:.1%} the stop-loss): "
        if practice
        else f"Probability {eco.probability:.1%} that the take-profit comes first, "
        f"{1 - eco.probability:.1%} the stop-loss: "
    )
    numbers = (
        f"at most {eco.max_loss} to lose (spread and slippage included), {eco.net_at_target} "
        f"to win; the average result of such a trade is {eco.expected:+.2f}"
    )
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": GOLD,
            "side": lv.side,
            "effect": "OPEN",
            "quantity": qty,
            "limit_price": lv.limit,
            "price_band_low": lv.band_low,
            "price_band_high": lv.band_high,
            "max_slippage_bps": Decimal(1),
            "time_in_force": "IOC",  # never left resting at HFM
            "horizon": "minutes",
            "thesis": (
                f"My Gold Auto-Trader, HFM Demo (virtual money), the 1-minute rule. {label}the "
                f"15- and 5-minute trends are {pred.features.get('m15')} and "
                f"{pred.features.get('m5')} and the 1-minute momentum turned with them. Model "
                f"{model.version}, measured on HFM's own candles: {model.evidence}. Stop-loss "
                f"{lv.stop} and take-profit {lv.take_profit}, both held by HFM; {numbers}."
            )[:4000],
            "plain_summary": (
                f"HFM demo: {verb} {qty.normalize():f} ounce of gold. {label}stop-loss "
                f"{lv.stop}, take-profit {lv.take_profit}; {numbers}."
            )[:600],
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{GOLD}",
                    "summary": f"HFM bid {price.bid} / ask {price.ask}",
                    "source_ts": price.at,
                },
                {
                    "kind": "NOTE",
                    "ref": f"model:{model.version}",
                    "summary": f"probability {eco.probability:.1%}; break-even "
                    f"{model.break_even:.1%}; {model.evidence}"[:500],
                },
            ],
            "counterarguments": [
                f"At {eco.probability:.1%} the take-profit comes first less often than the "
                f"{model.break_even:.1%} a 1:2 trade needs: on average such a trade loses.",
                "HFM's spread is about a sixth of this trade's whole risk.",
            ],
            "red_team": (
                "A 1-minute trade is decided by noise as much as by trend; gold can jump past "
                "the stop on news or at the weekend opening, and HFM then closes at the next "
                "price, which loses more than planned."
            ),
            "scenarios": [
                {
                    "name": "Target",
                    "description": f"Gold reaches {lv.take_profit}: HFM closes it there.",
                },
                {"name": "Stop", "description": f"Gold reaches {lv.stop}: HFM closes it there."},
            ],
            "invalidation": [
                f"HFM's {'ask at or above' if short else 'bid at or below'} {lv.stop}"
            ],
            "entry_conditions": (
                f"Limit sell at or above {lv.limit} (the bid − {model.slippage}), now or never"
                if short
                else f"Limit buy at or below {lv.limit} (the ask + {model.slippage}), now or never"
            ),
            "management_plan": {
                "stop_loss": {"stop_price": lv.stop, "limit_price": lv.stop},
                "take_profit_price": lv.take_profit,
            },
        }
    )


def _evidence(reading: analysis.Reading, bid: Decimal, ask: Decimal) -> list[dict[str, Any]]:
    h1, m1 = reading.frames["1h"], reading.frames["1m"]
    return [
        {"kind": "QUOTE", "ref": f"quote:{GOLD}", "summary": f"HFM bid {bid} / ask {ask}"},
        {
            "kind": "INDICATOR",
            "ref": f"gold_reading:{reading.bar:%Y-%m-%dT%H:%M}" if reading.bar else "gold_reading",
            "summary": (
                f"H1 trend {h1.trend}, ATR(14) {h1.atr:.2f}; M1 RSI(14) {m1.rsi:.1f}"
                if h1.atr is not None and m1.rsi is not None
                else f"H1 trend {h1.trend}"
            )[:500],
            "source_ts": reading.bar,
        },
    ]


def _entry(
    account: Account,
    tick: Decimal,
    bid: Decimal,
    ask: Decimal,
    entry: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    qty: Decimal,
    reading: analysis.Reading,
    *,
    short: bool = False,
) -> Candidate:
    """A buy at the ask + 0.1 % at most, or (S66) a short at the bid − 0.1 % at least; either
    immediate or cancel, its stop-loss held by HFM with the position."""
    why = "; ".join(reading.reasons)
    verb = "sell (open a short of)" if short else "buy"
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": GOLD,
            "side": "SELL" if short else "BUY",
            "effect": "OPEN",
            "quantity": qty,
            "limit_price": entry,
            "price_band_low": entry if short else _tick(bid * (1 - BAND), tick),
            "price_band_high": _tick(ask * (1 + BAND), tick, ROUND_CEILING) if short else entry,
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",  # never left resting at HFM
            "horizon": "minutes to days",
            "thesis": (
                f"My Gold Auto-Trader, HFM Demo mode (virtual money). The gold reading of HFM's "
                f"own candles says {reading.decision}: {why}. Stop-loss {stop} (1.5 × the hourly "
                f"ATR {'above the bid' if short else 'below the ask'}), held by HFM with the "
                f"position; take-profit {take_profit} (twice the stop distance). A forward test, "
                "not a proven edge."
            )[:4000],
            "plain_summary": (
                f"HFM demo: {verb} {qty.normalize():f} ounce of gold because {why}. Stop-loss "
                f"{stop}, take-profit {take_profit}. Forward test, not a proven edge."
            )[:600],
            "evidence": _evidence(reading, bid, ask),
            "counterarguments": [
                "A 1-minute turn can fail at once; the hourly trend can end without warning.",
                "The rules were chosen, not fitted or proven on history.",
            ],
            "red_team": (
                f"Momentum entries {'sell after a fall' if short else 'buy after a rise'}; gold "
                "can gap past the stop at the weekend opening or on news, and HFM then closes at "
                "the next price."
            ),
            "scenarios": [
                {
                    "name": "Target",
                    "description": f"Gold {'falls' if short else 'rises'} to {take_profit}: "
                    "closed there.",
                },
                {
                    "name": "Stop",
                    "description": f"Gold {'rises' if short else 'falls'} to {stop}: HFM closes "
                    "it there.",
                },
                {
                    "name": "Turn",
                    "description": f"The hourly trend turns {'up' if short else 'down'}: the "
                    "rule closes it.",
                },
            ],
            "invalidation": [
                f"HFM's {'ask at or above' if short else 'bid at or below'} {stop}",
                f"The hourly trend turns {'up' if short else 'down'}",
            ],
            "entry_conditions": (
                f"Limit sell at or above {entry} (the bid − 0.1 %), now or never"
                if short
                else f"Limit buy at or below {entry} (the ask + 0.1 %), now or never"
            ),
            "management_plan": {
                "stop_loss": {"stop_price": stop, "limit_price": stop},
                "take_profit_price": take_profit,
            },
        }
    )


def _exit(
    account: Account,
    tick: Decimal,
    price: Decimal,
    qty: Decimal,
    closes: list[uuid.UUID],
    reading: analysis.Reading,
    *,
    long: bool = True,
    why: str | None = None,
) -> Candidate:
    """The rule's exit: a sell at the bid − 0.1 % at least for a long, a buy at the ask + 0.1 %
    at most for a short (S66); immediate or cancel, the stop on until HFM has closed it.
    ``why``: a robot control's shutdown (S79) instead of the hourly trend's turn."""
    limit = (
        _tick(price * (1 - MARKUP), tick)
        if long
        else _tick(price * (1 + MARKUP), tick, ROUND_CEILING)
    )
    turned = "down" if long else "up"
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": GOLD,
            "side": "SELL" if long else "BUY",
            "effect": "CLOSE",
            "quantity": qty,
            "limit_price": limit,
            "price_band_low": _tick(price * (1 - BAND), tick),
            "price_band_high": _tick(price * (1 + BAND), tick, ROUND_CEILING),
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",
            "horizon": "now",
            "thesis": (
                f"My Gold Auto-Trader, HFM Demo mode: {why}, so the robot closes the "
                f"{'buy' if long else 'short'} it opened, at market. Its stop-loss stays on until "
                "HFM has closed the position."
                if why
                else f"My Gold Auto-Trader, HFM Demo mode: the hourly trend of HFM's gold turned "
                f"{turned}, so the rule closes the {'buy' if long else 'short'} it opened. Its "
                "stop-loss stays on until HFM has closed the position."
            ),
            "plain_summary": (
                f"HFM demo: {'sell' if long else 'buy back'} {qty.normalize():f} ounce of gold: "
                + (why if why else f"the hourly trend turned {turned}")
                + "."
            ),
            "evidence": _evidence(reading, price, price),
            "counterarguments": [
                f"The move may reverse soon after the {'sale' if long else 'purchase'}."
            ],
            "red_team": (
                f"Exiting on a trend turn can close near a short-term {'low' if long else 'high'}."
            ),
            "scenarios": [
                {"name": "Close", "description": "Closed at HFM's price, the stop removed with it."}
            ],
            "invalidation": ["Not applicable to an exit"],
            "entry_conditions": (
                f"Limit sell at or above {limit} (the bid − 0.1 %), now or never"
                if long
                else f"Limit buy at or below {limit} (the ask + 0.1 %), now or never"
            ),
            "closes": closes,
        }
    )


async def _bridge_version(conn: AsyncConnection) -> int:
    snap = await conn.scalar(
        select(bridge_state.c.snapshot).where(bridge_state.c.bridge == sim.BRIDGE)
    )
    return bridge_version(snap or {})
