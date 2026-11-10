"""The daily gold robot (S75, ADR-037 addendum): the Turtle breakout on HFM's own daily candles,
on the second HFM demo account (the owner's choice of 2026-11-10), as the test lab replayed it
(Run 4, trial 11, 2015–2026: 46 trades, +0.31 R a trade, t 0.77 — no proven edge; a forward
test on virtual money).

- **BUY** when a day closes above the highest high of the 55 days before it; **SELL** (a short)
  when it closes below their lowest low.
- **The rule's exit:** a long is closed when a day closes below the lowest low of the 20 days
  before it, a short when it closes above their highest high.
- **The stop-loss** is 2 × ATR(20) of the daily candles from the entry, held by HFM with the
  position. No take-profit: the exit rule lets a trend run.
- **Once per daily candle and mandate** (``gold_hfm_decisions``): decided at the first minute
  HFM's price is fresh after the candle closed (the lab fills at the next day's open), so a
  restart or the daily break never loses or repeats a decision.
- **Size:** HFM's whole ounces whose loss at the stop, with the fee both ways, fits the
  mandate's loss per trade (``hfm_demo.size``); otherwise it waits and says from which amount.
- No AI second opinion: the rule runs as it was tested.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.core.labels import DataLabel
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.gold import hfm_demo, hfm_selftest, sim
from cosmic.market import data as md
from cosmic.market import hfm_candles
from cosmic.market import indicators as ind
from cosmic.market.hfm_candles import Candle
from cosmic.notify.outbox import notify_owners
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.brokers.mt5_bridge import SHORTS_VERSION, bridge_version
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate

BUY, SELL, WAIT = "BUY", "SELL", "WAIT"
ENTRY_DAYS = 55  # the breakout: the days before today whose high / low a close must pass
EXIT_DAYS = 20  # the exit: the days before today whose low (long) / high (short) ends it
ATR_DAYS = 20
STOP_ATR = Decimal(2)
CANDLES = 300  # loaded: Wilder's ATR forgets its start (0.95 ** 280 < 1e-6)
ENOUGH = ENTRY_DAYS + 1
# The newest complete daily candle must have ended this recently (a weekend is about 2 days).
RECENT = timedelta(days=4)


@dataclass(frozen=True)
class DailyReading:
    """What the daily rule says after the last complete daily candle."""

    bar: datetime | None  # the open time of that candle
    decision: str
    reasons: list[str] = field(default_factory=list)
    close: Decimal | None = None
    entry_high: Decimal | None = None  # highest high of the 55 days before it
    entry_low: Decimal | None = None
    exit_high: Decimal | None = None  # highest high of the 20 days before it
    exit_low: Decimal | None = None
    atr: Decimal | None = None
    stop_distance: Decimal | None = None  # 2 × ATR(20)
    exit_long: bool = False  # a long is closed: the close is below the 20-day low
    exit_short: bool = False  # a short is closed: the close is above the 20-day high


def read(candles: Sequence[Candle], now: datetime) -> DailyReading:
    """The rule on complete daily candles, oldest first."""
    if len(candles) < ENOUGH:
        return DailyReading(
            None, WAIT, [f"D1: {len(candles)} of {ENOUGH} daily candles stored so far"]
        )
    last = candles[-1]
    if now - hfm_candles.ends(last.open_time, "1d") > RECENT:
        return DailyReading(
            last.open_time, WAIT, ["D1: no daily candle has closed lately (no fresh data)"]
        )
    before55, before20 = candles[-ENOUGH:-1], candles[-EXIT_DAYS - 1 : -1]
    entry_high = max(c.high for c in before55)
    entry_low = min(c.low for c in before55)
    exit_high = max(c.high for c in before20)
    exit_low = min(c.low for c in before20)
    atr_series = ind.atr(
        [float(c.high) for c in candles],
        [float(c.low) for c in candles],
        [float(c.close) for c in candles],
        ATR_DAYS,
    )
    raw = atr_series[-1]
    assert raw is not None  # ENOUGH candles carry more than ATR_DAYS true ranges
    atr = Decimal(str(round(raw, 4)))
    stop_distance = (STOP_ATR * atr).quantize(Decimal("0.01"))
    close = last.close
    day = f"{hfm_candles.ends(last.open_time, '1d'):%d %b}"
    levels = (
        f"the day to {day} closed at {close}; 55-day high {entry_high}, low {entry_low}; "
        f"20-day high {exit_high}, low {exit_low}; ATR(20) {atr:.2f}"
    )
    if close > entry_high:
        decision, why = BUY, f"BUY: it closed above the 55-day high ({levels})"
    elif close < entry_low:
        decision, why = SELL, f"SELL: it closed below the 55-day low ({levels})"
    else:
        decision, why = WAIT, f"WAIT: no 55-day breakout ({levels})"
    return DailyReading(
        last.open_time,
        decision,
        [why],
        close,
        entry_high,
        entry_low,
        exit_high,
        exit_low,
        atr,
        stop_distance,
        exit_long=close < exit_low,
        exit_short=close > exit_high,
    )


async def reading_now(conn: AsyncConnection, now: datetime) -> DailyReading:
    return read(await hfm_candles.candles(conn, "1d", CANDLES), now)


async def daily_mandates(conn: AsyncConnection, now: datetime) -> list[mandates.Mandate]:
    return [
        m
        for m in await mandates.active_mandates(conn, now)
        if m.strategy_key == mandates.GOLD_DAILY
    ]


async def decide(db: Database, now: datetime) -> list[str]:
    """For each active daily mandate, once per new daily candle."""
    out: list[str] = []
    async with db.connect() as conn:
        found = await daily_mandates(conn, now)
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
    reading = await reading_now(conn, now)
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
    if price is None:
        return ""  # not decided yet: at the first fresh price (the daily break, the weekend)
    account = await get_account(conn, account_id=m.account_id)
    inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
    if not await _checks_can_see(conn, inst.id, inst.venue_key, now):
        return ""  # one chance a day: never spent before the risk checks' data is fresh too
    reasons = list(reading.reasons)
    action, ounces, intent_id = "WAIT", None, None

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
                bid=price.bid,
                ask=price.ask,
                ounces=ounces,
                stop_distance=reading.stop_distance,
                intent_id=intent_id,
            )
        )
        return f"{m.id}:{action}"

    if inst.tick_size is None:
        reasons.append("WAIT — HFM's gold has no price step stored yet")
        return await record()
    longs = await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)}, "BUY")
    shorts = await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)}, "SELL")
    in_flight = await hfm_demo._in_flight(conn, account, inst.id)
    against = longs if reading.exit_long else shorts if reading.exit_short else []
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
        intent_id, why = await hfm_demo._propose(conn, m, candidate, now, timeframe=mandates.DAILY)
        action = "EXIT" if why is None else "REFUSED"
        level = f"20-day low {reading.exit_low}" if long else f"20-day high {reading.exit_high}"
        reasons.append(
            f"closing the {'buy' if long else 'short'} of {qty.normalize():f} ounce: the day "
            f"closed {'below' if long else 'above'} the {level}"
            + (f" (refused: {why})" if why else "")
        )
        return await record()
    if reading.decision == WAIT:
        return await record()
    short = reading.decision == SELL
    held = longs + shorts
    if len(held) >= m.max_open_positions:
        reasons.append(
            f"WAIT — {len(held)} trade(s) of this robot open: the most at once is "
            f"{m.max_open_positions}"
        )
        return await record()
    if short:
        _, policy = await active_policy(conn)
        if not policy.shorts_allowed(inst.venue_key, inst.asset_class):
            reasons.append(hfm_demo.NO_SHORTS)
            return await record()
        version = bridge_version(await hfm_selftest._report(conn, account.id))
        if version < SHORTS_VERSION:
            reasons.append(hfm_demo.OLD_BRIDGE.format(version=version))
            return await record()
    if in_flight:
        reasons.append("WAIT — a trade of this gold is already on its way")
        return await record()
    assert reading.stop_distance is not None
    _, policy = await active_policy(conn)
    tick = inst.tick_size
    if short:  # sold at the bid or better; the stop above
        entry = hfm_demo._tick(price.bid * (1 - hfm_demo.MARKUP), tick)
        stop = hfm_demo._tick(price.bid + reading.stop_distance, tick, ROUND_CEILING)
    else:
        entry = hfm_demo._tick(price.ask * (1 + hfm_demo.MARKUP), tick, ROUND_CEILING)
        stop = hfm_demo._tick(price.ask - reading.stop_distance, tick)
    lot = inst.lot_size or Decimal(1)
    qty, why = hfm_demo.size(
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
        await notify_owners(
            conn,
            kind="gold_sim",
            payload={"text": f"HFM DEMO 2 (daily): a {reading.decision} was not taken. {why}"},
            now=now,
            dedup_key=f"gold-daily:{m.id}:too-small:{reading.bar:%Y-%m-%d}",
        )
        return await record()
    candidate = _entry(account, tick, price.bid, price.ask, entry, stop, qty, reading, short=short)
    ounces = qty
    intent_id, refused = await hfm_demo._propose(conn, m, candidate, now, timeframe=mandates.DAILY)
    action = "PROPOSED" if refused is None else "REFUSED"
    reasons.append(
        f"{'SELL (short)' if short else 'BUY'} {qty.normalize():f} ounce at "
        f"{'least' if short else 'most'} {entry}, stop-loss {stop}, no take-profit: "
        + (
            "approved under your mandate and sent to HFM's demo"
            if refused is None
            else f"refused: {refused}"
        )
    )
    return await record()


async def _checks_can_see(
    conn: AsyncConnection, instrument_id: Any, venue_key: str, now: datetime
) -> bool:
    """Whether the stored HFM price and HFM's status, which the risk checks read (R09, R10), are
    fresh: they are stored every 15 and 30 seconds, so right after the daily break or the
    weekend they lag the bridge's live price. The daily robot decides once a day, so it waits
    for them rather than spend its decision on a refusal for stale data."""
    quote = await md.latest_quote(conn, instrument_id, now)
    status, label = await md.latest_venue_status(conn, venue_key, now)
    fresh = (DataLabel.LIVE, DataLabel.DELAYED)
    return quote.label in fresh and label in fresh and status == "OPERATIONAL"


def _evidence(reading: DailyReading, bid: Decimal, ask: Decimal) -> list[dict[str, Any]]:
    return [
        {"kind": "QUOTE", "ref": f"quote:{hfm_demo.GOLD}", "summary": f"HFM bid {bid} / ask {ask}"},
        {
            "kind": "INDICATOR",
            "ref": f"gold_daily:{reading.bar:%Y-%m-%d}" if reading.bar else "gold_daily",
            "summary": "; ".join(reading.reasons)[:500],
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
    qty: Decimal,
    reading: DailyReading,
    *,
    short: bool = False,
) -> Candidate:
    """A buy at the ask + 0.1 % at most, or a short at the bid − 0.1 % at least; immediate or
    cancel, its stop-loss held by HFM with the position, no take-profit."""
    why = "; ".join(reading.reasons)
    verb = "sell (open a short of)" if short else "buy"
    level = f"20-day high ({reading.exit_high})" if short else f"20-day low ({reading.exit_low})"
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": hfm_demo.GOLD,
            "side": "SELL" if short else "BUY",
            "effect": "OPEN",
            "quantity": qty,
            "limit_price": entry,
            "price_band_low": entry if short else hfm_demo._tick(bid * (1 - hfm_demo.BAND), tick),
            "price_band_high": (
                hfm_demo._tick(ask * (1 + hfm_demo.BAND), tick, ROUND_CEILING) if short else entry
            ),
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",  # never left resting at HFM
            "horizon": "days to months",
            "thesis": (
                "My daily gold robot, HFM Demo 2 (virtual money): the Turtle breakout on HFM's "
                f"own daily candles. {why}. Stop-loss {stop} (2 × the daily ATR "
                f"{'above the bid' if short else 'below the ask'}), held by HFM with the "
                f"position; no take-profit: it closes when a day closes beyond the {level}. "
                "A forward test, not a proven edge (the lab: t 0.77 over 2015–2026)."
            )[:4000],
            "plain_summary": (
                f"HFM demo 2 (daily robot): {verb} {qty.normalize():f} ounce of gold because "
                f"{why}. Stop-loss {stop}; closed when a day closes beyond the {level}. "
                "Forward test, not a proven edge."
            )[:600],
            "evidence": _evidence(reading, bid, ask),
            "counterarguments": [
                "Most breakouts fail: in the lab about 7 in 10 of these trades lost.",
                "The rule's profit came from a few long trends; it has no proven edge.",
            ],
            "red_team": (
                f"A breakout {'down' if short else 'up'} can reverse at once; gold can gap past "
                "the stop at the weekend opening or on news, and HFM then closes at the next "
                "price."
            ),
            "scenarios": [
                {
                    "name": "Trend",
                    "description": f"Gold keeps {'falling' if short else 'rising'}: held until a "
                    f"day closes beyond the {level}.",
                },
                {
                    "name": "Stop",
                    "description": f"Gold {'rises' if short else 'falls'} to {stop}: HFM closes "
                    "it there.",
                },
            ],
            "invalidation": [
                f"HFM's {'ask at or above' if short else 'bid at or below'} {stop}",
                f"A day closes beyond the {level}",
            ],
            "entry_conditions": (
                f"Limit sell at or above {entry} (the bid − 0.1 %), now or never"
                if short
                else f"Limit buy at or below {entry} (the ask + 0.1 %), now or never"
            ),
            "management_plan": {"stop_loss": {"stop_price": stop, "limit_price": stop}},
        }
    )


def _exit(
    account: Account,
    tick: Decimal,
    price: Decimal,
    qty: Decimal,
    closes: list[Any],
    reading: DailyReading,
    *,
    long: bool = True,
) -> Candidate:
    """The rule's exit: a sell at the bid − 0.1 % at least for a long, a buy at the ask + 0.1 %
    at most for a short; immediate or cancel, the stop on until HFM has closed it."""
    limit = (
        hfm_demo._tick(price * (1 - hfm_demo.MARKUP), tick)
        if long
        else hfm_demo._tick(price * (1 + hfm_demo.MARKUP), tick, ROUND_CEILING)
    )
    level = f"20-day low ({reading.exit_low})" if long else f"20-day high ({reading.exit_high})"
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": hfm_demo.GOLD,
            "side": "SELL" if long else "BUY",
            "effect": "CLOSE",
            "quantity": qty,
            "limit_price": limit,
            "price_band_low": hfm_demo._tick(price * (1 - hfm_demo.BAND), tick),
            "price_band_high": hfm_demo._tick(price * (1 + hfm_demo.BAND), tick, ROUND_CEILING),
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",
            "horizon": "now",
            "thesis": (
                f"My daily gold robot, HFM Demo 2: the day closed "
                f"{'below' if long else 'above'} the {level}, so the rule closes the "
                f"{'buy' if long else 'short'} it opened. Its stop-loss stays on until HFM has "
                "closed the position."
            ),
            "plain_summary": (
                f"HFM demo 2 (daily robot): {'sell' if long else 'buy back'} "
                f"{qty.normalize():f} ounce of gold: the day closed beyond the {level}."
            ),
            "evidence": _evidence(reading, price, price),
            "counterarguments": [
                f"The move may reverse soon after the {'sale' if long else 'purchase'}."
            ],
            "red_team": "Exiting on a 20-day level can close near a short-term turning point.",
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
