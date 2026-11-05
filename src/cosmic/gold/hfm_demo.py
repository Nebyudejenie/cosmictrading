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
- **SELL** is not sent: the order path holds long positions only (a genuine short needs its own
  checks); it waits and says so. The simulation trades sells.
- **The rule's exit:** a long position is closed when the hourly trend turns down, with its
  stop-loss still on until HFM has closed it (S63).
- Once per 1-minute candle and mandate (``gold_hfm_decisions``), so a restart never proposes
  twice. Virtual money only: the mandate is granted only on the HFM demo account."""

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
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.gold import analysis, sim
from cosmic.market import data as md
from cosmic.notify.outbox import notify_owners
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate, create_candidate, entry_available

ACTOR = Actor.worker("gold_trader")
GOLD = "CFD:XAU/USD@HFM"
MARKUP = Decimal("0.001")  # a buy is a marketable limit at the ask + 0.1 %, immediate or cancel
BAND = Decimal("0.005")
TTL = timedelta(minutes=5)  # an asked trade lapses, as in the simulation
WAITING_EXIT = ("PROPOSED", "REQUIRES_REAPPROVAL", "APPROVED", "EXECUTING")
NOT_YET_SHORT = (
    "WAIT — a sell (short) is not sent to the HFM demo yet: the order path holds long positions "
    "only. The simulation trades sells"
)


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
    counts it) stays within ``max_loss``; or none, and why, in the owner's words."""
    per_unit = (entry - stop) + 2 * entry * fee_bps / 10_000
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
        if m.strategy_key == mandates.GOLD_READING
    ]


async def _held_entries(
    conn: AsyncConnection, account: Account, instrument_id: uuid.UUID, mandate_ids: set[str]
) -> list[tuple[uuid.UUID, Decimal]]:
    """This rule's filled buys that still hold gold, oldest first."""
    rows = await conn.execute(
        select(trade_intents.c.id, trade_intents.c.details)
        .where(
            trade_intents.c.account_id == account.id,
            trade_intents.c.instrument_id == instrument_id,
            trade_intents.c.side == "BUY",
            trade_intents.c.status.in_(["EXECUTING", "EXECUTED"]),
        )
        .order_by(trade_intents.c.created_at)
    )
    out = []
    for r in rows:
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
    account = await get_account(conn, account_id=m.account_id)
    inst = await md.instrument_by_key(conn, GOLD)

    async def record() -> str:
        await conn.execute(
            gold_hfm_decisions.insert().values(
                id=uuid7(),
                mandate_id=m.id,
                bar=reading.bar,
                at=now,
                decision=reading.decision,
                action=action,
                reasons=reasons,
                bid=price.bid if price else None,
                ask=price.ask if price else None,
                ounces=ounces,
                stop_distance=reading.stop_distance,
                intent_id=intent_id,
            )
        )
        return f"{m.id}:{action}"

    if price is None or inst.tick_size is None:
        reasons.append("WAIT — no current HFM price")
        return await record()
    held = await _held_entries(conn, account, inst.id, {str(m.id)})
    in_flight = await _in_flight(conn, account, inst.id)
    # The rule's exit: the hourly trend has turned down under a long.
    if held and reading.frames["1h"].trend == analysis.DOWN and not in_flight:
        qty = sum((h for _, h in held), ZERO)
        candidate = _exit(account, inst.tick_size, price.bid, qty, [e for e, _ in held], reading)
        intent_id, why = await _propose(conn, m, candidate, now)
        action = "EXIT" if why is None else "REFUSED"
        reasons.append(
            f"closing {qty.normalize():f} ounce: the hourly trend turned down"
            + (f" (refused: {why})" if why else "")
        )
        return await record()
    if reading.decision == analysis.WAIT:
        return await record()
    if reading.decision == analysis.SELL:
        reasons.append(NOT_YET_SHORT)
        return await record()
    if in_flight:
        reasons.append("WAIT — a trade of this gold is already on its way")
        return await record()
    if len(held) >= m.max_open_positions:
        reasons.append(
            f"WAIT — {len(held)} trade(s) open: the most at once is {m.max_open_positions}"
        )
        return await record()
    assert reading.stop_distance is not None
    _, policy = await active_policy(conn)
    tick = inst.tick_size
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
            payload={"text": f"HFM DEMO: a BUY opportunity was not taken. {why}"},
            now=now,
            dedup_key=f"gold-hfm:{m.id}:too-small:{now:%Y-%m-%d}",
        )
        return await record()
    take_profit = _tick(price.ask + 2 * reading.stop_distance, tick)
    candidate = _entry(account, tick, price.bid, price.ask, entry, stop, take_profit, qty, reading)
    ounces = qty
    intent_id, refused = await _propose(conn, m, candidate, now)
    action = "PROPOSED" if refused is None else "REFUSED"
    reasons.append(
        f"BUY {qty.normalize():f} ounce at most {entry}, stop-loss {stop}, take-profit "
        f"{take_profit}: "
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
    conn: AsyncConnection, m: mandates.Mandate, candidate: Candidate, now: datetime
) -> tuple[uuid.UUID | None, str | None]:
    """The proposal through every risk check, then the mandate approves it (Automatic) or the
    owner is asked (Ask me first)."""
    result = await create_candidate(
        conn,
        candidate=candidate,
        actor=ACTOR,
        now=now,
        mandate_id=m.id,
        timeframe=mandates.HOURLY,
        ttl=TTL,
    )
    if result.status != "PROPOSED":
        return result.intent_id, "; ".join(result.problems)[:300] or result.status
    decide = mandates.ask if m.mode == "ASK" else mandates.approve
    approved, reason = await decide(conn, intent_id=result.intent_id, mandate=m, now=now)
    return result.intent_id, None if approved else reason


def _evidence(reading: analysis.Reading, bid: Decimal, ask: Decimal) -> list[dict[str, Any]]:
    h1, m1 = reading.frames["1h"], reading.frames["1m"]
    return [
        {"kind": "QUOTE", "ref": f"quote:{GOLD}", "summary": f"HFM bid {bid} / ask {ask}"},
        {
            "kind": "INDICATOR",
            "ref": f"gold_reading:{reading.bar:%Y-%m-%dT%H:%M}",
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
) -> Candidate:
    why = "; ".join(reading.reasons)
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": GOLD,
            "side": "BUY",
            "effect": "OPEN",
            "quantity": qty,
            "limit_price": entry,
            "price_band_low": _tick(bid * (1 - BAND), tick),
            "price_band_high": entry,
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",  # never left resting at HFM
            "horizon": "minutes to days",
            "thesis": (
                f"My Gold Auto-Trader, HFM Demo mode (virtual money). The gold reading of HFM's "
                f"own candles says BUY: {why}. Stop-loss {stop} (1.5 × the hourly ATR below the "
                f"ask), held by HFM with the position; take-profit {take_profit} (twice the "
                "stop distance). A forward test, not a proven edge."
            )[:4000],
            "plain_summary": (
                f"HFM demo: buy {qty.normalize():f} ounce of gold because {why}. Stop-loss "
                f"{stop}, take-profit {take_profit}. Forward test, not a proven edge."
            )[:600],
            "evidence": _evidence(reading, bid, ask),
            "counterarguments": [
                "A 1-minute turn can fail at once; the hourly trend can end without warning.",
                "The rules were chosen, not fitted or proven on history.",
            ],
            "red_team": (
                "Momentum entries buy after a rise; gold can gap past the stop at the weekend "
                "opening or on news, and HFM then closes at the next price."
            ),
            "scenarios": [
                {"name": "Target", "description": f"Gold rises to {take_profit}: sold there."},
                {"name": "Stop", "description": f"Gold falls to {stop}: HFM closes it there."},
                {"name": "Turn", "description": "The hourly trend turns down: the rule sells."},
            ],
            "invalidation": [f"HFM's bid at or below {stop}", "The hourly trend turns down"],
            "entry_conditions": f"Limit buy at or below {entry} (the ask + 0.1 %), now or never",
            "management_plan": {
                "stop_loss": {"stop_price": stop, "limit_price": stop},
                "take_profit_price": take_profit,
            },
        }
    )


def _exit(
    account: Account,
    tick: Decimal,
    bid: Decimal,
    qty: Decimal,
    closes: list[uuid.UUID],
    reading: analysis.Reading,
) -> Candidate:
    limit = _tick(bid * (1 - MARKUP), tick)
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": GOLD,
            "side": "SELL",
            "effect": "CLOSE",
            "quantity": qty,
            "limit_price": limit,
            "price_band_low": _tick(bid * (1 - BAND), tick),
            "price_band_high": _tick(bid * (1 + BAND), tick, ROUND_CEILING),
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",
            "horizon": "now",
            "thesis": (
                "My Gold Auto-Trader, HFM Demo mode: the hourly trend of HFM's gold turned "
                "down, so the rule closes what it bought. Its stop-loss stays on until HFM has "
                "closed the position."
            ),
            "plain_summary": (
                f"HFM demo: sell {qty.normalize():f} ounce of gold: the hourly trend turned down."
            ),
            "evidence": _evidence(reading, bid, bid),
            "counterarguments": ["The fall may reverse soon after the sale."],
            "red_team": "Exiting on a trend turn can sell near a short-term low.",
            "scenarios": [{"name": "Close", "description": "Sold at the bid, the stop removed."}],
            "invalidation": ["Not applicable to an exit"],
            "entry_conditions": f"Limit sell at or above {limit} (the bid − 0.1 %), now or never",
            "closes": closes,
        }
    )
