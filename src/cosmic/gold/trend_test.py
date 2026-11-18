"""The TEST robot (S98; the owner's choice of 2026-11-17, "Cent demo, 2 USD a trade"): the forward
test of ``cosmic.gold.trend_buy`` on the second HFM demo account (terminal 2), as the gold lab
measured it, on virtual money.

- **Once per 15-minute candle and mandate** (``gold_hfm_decisions``), at the first fresh HFM price
  after the candle closed: the rule's reading on HFM's own 15-minute and 1-hour candles (the
  primary terminal's, the same market).
- **BUY only**, one trade at a time: the stop-loss (2 × ATR14 of the 15-minute candles) and the
  take-profit (twice that) both travel with the order and are held by HFM; after 24 hours the
  robot closes what is still open.
- **Size:** the most ounces, in the account's own volume steps (read from its own terminal's
  report), whose loss at the stop with the slippage allowance and the fee both ways stays within
  the mandate's loss per trade (the owner's 2 USD). A step too big for that (a standard account's
  1 ounce) means no trade, said once a day: the test needs a Cent account.
- **The owner's rules** (``gold.controls``): the daily loss limit (6 USD) is never passed by one
  more trade, and three losses in a row pause it until the owner resumes.
- **Speed (S87):** the order is sent in the same step, within a second of the decision, or not
  at all; only while that terminal's fast lane is up.
- **No AI second opinion:** the test is the rule as it was measured.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.db.schema_trading import bridge_fast
from cosmic.gold import controls, daily, hfm_demo, hfm_selftest, sim, trend_buy
from cosmic.market import data as md
from cosmic.market import hfm_candles
from cosmic.notify.outbox import notify_owners
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.brokers.mt5_bridge import BY_ADAPTER, FAST_VERSION, bridge_version
from cosmic.trading.policy import active_policy
from cosmic.trading.proposals import Candidate

RULE = mandates.GOLD_TREND_BUY
M15_CANDLES, H1_CANDLES = 500, 800  # what the lab's engine check used: identical signals
SLIPPAGE = Decimal("0.10")  # the entry's limit above the ask, counted in the loss
LABEL = "TEST"


async def reading_now(
    conn: AsyncConnection, now: datetime, *, sell: bool = False
) -> trend_buy.Reading:
    """The rule on HFM's candles now; its SELL mirror too only when asked (Robot A, S106)."""
    return trend_buy.read(
        await hfm_candles.candles(conn, "15m", M15_CANDLES),
        await hfm_candles.candles(conn, "1h", H1_CANDLES),
        now,
        sell=sell,
    )


async def test_mandates(conn: AsyncConnection, now: datetime) -> list[mandates.Mandate]:
    return [m for m in await mandates.active_mandates(conn, now) if m.strategy_key == RULE]


async def decide(db: Database, now: datetime) -> list[str]:
    """For each active TEST mandate, once per new 15-minute candle."""
    out: list[str] = []
    async with db.connect() as conn:
        found = await test_mandates(conn, now)
    for m in found:
        async with db.transaction() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"gold-hfm:{m.id}"},
            )
            out.append(await _decide_one(conn, m, now))
    return [x for x in out if x]


def ounce_step(report: dict[str, Any], symbol: str = hfm_candles.SYMBOL) -> Decimal | None:
    """The account's smallest step in ounces, from its own terminal's report: the volume step
    times the contract size. None when the report does not say."""
    spec = (report.get("symbols") or {}).get(symbol) or {}
    try:
        step = Decimal(str(spec["volume_step"])) * Decimal(str(spec["contract_size"]))
    except (KeyError, ArithmeticError, ValueError):
        return None
    return step if step > 0 else None


async def lane_problem(conn: AsyncConnection, account: Account, now: datetime) -> str | None:
    """Why an order could not leave for this account's terminal within a second now: its
    bridge before version 8, or its fast lane silent or slow (the 1-minute robot's rule, S87,
    for the terminal this robot trades on)."""
    bridge = BY_ADAPTER.get(account.broker_adapter or "")
    if bridge is None:
        return "this account has no HFM terminal"
    version = bridge_version(await hfm_selftest._report(conn, account.id))
    if version < FAST_VERSION:
        return f"the bridge of terminal 2 is version {version}; the fast path needs {FAST_VERSION}"
    row = (
        await conn.execute(
            select(bridge_fast.c.received_at, bridge_fast.c.info).where(
                bridge_fast.c.bridge == bridge.name
            )
        )
    ).first()
    if row is None:
        return "the fast path of terminal 2 has not connected yet"
    age = now - row.received_at
    if age > hfm_demo.FAST_UP:
        return f"the fast path of terminal 2 was last heard {age.total_seconds():.1f} s ago"
    rtt = (row.info or {}).get("rtt_ms")
    if isinstance(rtt, int) and rtt > hfm_demo.FAST_RTT_MS:
        return f"the way to terminal 2 is slow (a round trip of {rtt} ms)"
    return None


async def _opened_at(conn: AsyncConnection, intent_id: Any) -> datetime | None:
    return await conn.scalar(
        select(trade_intents.c.created_at).where(trade_intents.c.id == intent_id)
    )


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
        return ""  # decided at the first fresh price (the daily break, the weekend)
    account = await get_account(conn, account_id=m.account_id)
    inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
    if not await daily._checks_can_see(conn, inst.id, inst.venue_key, now):
        return ""
    reasons = [f"{LABEL} ({trend_buy.VERSION}): {reading.why}"]
    action, ounces, intent_id = "WAIT", None, None
    distance: Decimal | None = None

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
                stop_distance=distance,
                intent_id=intent_id,
            )
        )
        return f"{m.id}:{action}"

    if inst.tick_size is None:
        reasons.append("WAIT — HFM's gold has no price step stored yet")
        return await record()
    held = await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)}, "BUY")
    in_flight = await hfm_demo._in_flight(conn, account, inst.id)
    # Out after 24 hours: what is still open then is closed at the market.
    old = []
    for entry, qty in held:
        opened = await _opened_at(conn, entry)
        if opened is not None and now - opened >= trend_buy.HOLD:
            old.append((entry, qty))
    if old and not in_flight:
        qty = sum((q for _, q in old), ZERO)
        candidate = _exit(account, inst.tick_size, price.bid, qty, [e for e, _ in old], "24 hours")
        intent_id, refused = await hfm_demo._propose(conn, m, candidate, now)
        action = "EXIT" if refused is None else "REFUSED"
        reasons.append(
            f"closing the buy of {qty.normalize():f} ounce after 24 hours, as the test's rule "
            "says" + (f" (refused: {refused})" if refused else "")
        )
        return await record()
    # The owner's rules (daily loss, three losses in a row), as for the other robots.
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
        if ctl.status in controls.SHUT or ctl.status == controls.PAUSED:
            reasons.append(f"WAIT — {ctl.status.replace('_', ' ').lower()}: {ctl.status_reason}")
            return await record()
    if reading.decision != trend_buy.BUY:
        return await record()
    if held:
        reasons.append("WAIT — the test holds one trade at a time, and one is open")
        return await record()
    if in_flight:
        reasons.append("WAIT — a trade of this gold is already on its way")
        return await record()
    problem = await lane_problem(conn, account, now)
    if problem is not None:
        reasons.append(f"WAIT — an order could not leave within a second: {problem}")
        return await record()
    assert reading.atr is not None
    found = trend_buy.levels(reading.atr, price.bid, price.ask, inst.tick_size, SLIPPAGE)
    if isinstance(found, str):
        reasons.append(f"WAIT — {found}")
        return await record()
    distance = found.distance
    step = ounce_step(await hfm_selftest._report(conn, account.id))
    if step is None:
        reasons.append("WAIT — terminal 2 has not reported its gold's volume step yet")
        return await record()
    _, policy = await active_policy(conn)
    fee = 2 * found.limit * policy.fee_bps_for(inst.venue_key) / 10_000  # both ways, an ounce
    qty = trend_buy.ounces(m.max_loss_per_trade, found.distance, SLIPPAGE, fee, step)
    if qty <= 0:
        smallest = (step * (found.distance + SLIPPAGE + fee)).quantize(
            Decimal("0.01"), ROUND_CEILING
        )
        why = (
            f"WAIT — this account's smallest gold trade ({step.normalize():f} ounce) would lose "
            f"about {smallest:,.2f} at the stop, more than the {m.max_loss_per_trade:,.2f} a trade "
            "you allow: the test needs a Cent account"
        )
        reasons.append(why)
        await notify_owners(
            conn,
            kind="gold_sim",
            payload={"text": f"HFM DEMO 2 ({LABEL}): a BUY was not taken. {why}"},
            now=now,
            dedup_key=f"gold-test:{m.id}:too-small:{now:%Y-%m-%d}",
        )
        return await record()
    risk = (qty * (found.distance + SLIPPAGE + fee)).quantize(Decimal("0.01"), ROUND_CEILING)
    if res is not None:
        ceiling = controls.day_room(m, res, risk)
        if ceiling is not None:
            reasons.append(ceiling)
            return await record()
    candidate = _entry(account, price.bid, price.ask, found, qty, risk, reading)
    extra = {
        "rule": RULE,
        "model": trend_buy.VERSION,
        "test": True,
        "decided_at": now.isoformat(),
        "send_within_ms": int(hfm_demo.SEND_WITHIN.total_seconds() * 1000),
    }
    # The checks judge the trade by the price it was decided on (S87).
    await hfm_candles.record_quote(conn, now, price.bid, price.ask, price.at)
    intent_id, refused = await hfm_demo._propose(conn, m, candidate, now, extra, approve=True)
    action = "PROPOSED" if refused is None else "REFUSED"
    ounces = qty
    reasons.append(
        f"BUY {qty.normalize():f} ounce at most {found.limit}, stop-loss {found.stop}, "
        f"take-profit {found.take_profit}, both held by HFM; at most {risk:,.2f} to lose: "
        + ("sent to HFM's demo 2" if refused is None else f"refused: {refused}")
    )
    return await record()


def _evidence(reading: trend_buy.Reading, bid: Decimal, ask: Decimal) -> list[dict[str, Any]]:
    rsi = (
        f"{reading.rsi_before:.1f} → {reading.rsi:.1f}"
        if reading.rsi is not None and reading.rsi_before is not None
        else "—"
    )
    return [
        {"kind": "QUOTE", "ref": f"quote:{hfm_demo.GOLD}", "summary": f"HFM bid {bid} / ask {ask}"},
        {
            "kind": "INDICATOR",
            "ref": f"{trend_buy.VERSION}:{reading.bar:%Y-%m-%d %H:%M}" if reading.bar else RULE,
            "summary": (
                f"4-hour trend {reading.h4}, 1-hour trend {reading.h1}, 15-minute RSI {rsi}: "
                f"{reading.why}. {trend_buy.EVIDENCE}"
            )[:500],
            "source_ts": reading.bar,
        },
    ]


def _entry(
    account: Account,
    bid: Decimal,
    ask: Decimal,
    lv: trend_buy.Levels,
    qty: Decimal,
    risk: Decimal,
    reading: trend_buy.Reading,
) -> Candidate:
    """A buy at the limit or better, now or never; its stop-loss and take-profit with it."""
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": hfm_demo.GOLD,
            "side": "BUY",
            "effect": "OPEN",
            "quantity": qty,
            "limit_price": lv.limit,
            "price_band_low": lv.stop,
            "price_band_high": lv.limit,
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",
            "horizon": "hours (at most 24)",
            "thesis": (
                f"{LABEL} robot, HFM demo 2 (virtual money): a forward test of the gold lab's "
                f"best candidate. {reading.why}. Stop-loss {lv.stop} (2 × the 15-minute ATR "
                f"below the ask) and take-profit {lv.take_profit} (twice as far), both held by "
                "HFM; closed after 24 hours at the latest. Not a proven edge: "
                f"{trend_buy.EVIDENCE}."
            )[:4000],
            "plain_summary": (
                f"HFM demo 2 ({LABEL}): buy {qty.normalize():f} ounce of gold in the 4-hour and "
                f"1-hour uptrend. Stop {lv.stop}, target {lv.take_profit}, at most {risk:,.2f} "
                "to lose. A forward test, not a proven edge."
            )[:600],
            "evidence": _evidence(reading, bid, ask),
            "counterarguments": [
                "In the lab the target came first only about 37 times in 100.",
                "The whole rule did not beat random timing; 2026 was slightly negative.",
            ],
            "red_team": (
                "An uptrend can end at any moment; gold can gap past the stop on news, and HFM "
                "then closes at the next price."
            ),
            "scenarios": [
                {"name": "Target", "description": f"Gold reaches {lv.take_profit}: HFM closes."},
                {"name": "Stop", "description": f"Gold falls to {lv.stop}: HFM closes."},
                {"name": "Time", "description": "Neither in 24 hours: the robot closes it."},
            ],
            "invalidation": [f"HFM's bid at or below {lv.stop}"],
            "entry_conditions": f"Limit buy at or below {lv.limit} (the ask + {SLIPPAGE}), now "
            "or never",
            "management_plan": {
                "stop_loss": {"stop_price": lv.stop, "limit_price": lv.stop},
                "take_profit_price": lv.take_profit,
            },
        }
    )


def _exit(
    account: Account,
    tick: Decimal,
    bid: Decimal,
    qty: Decimal,
    closes: Sequence[Any],
    why: str,
) -> Candidate:
    """The time exit: a sell at the bid − 0.1 % at least, now or never; the stop stays on until
    HFM has closed the position."""
    limit = hfm_demo._tick(bid * (1 - hfm_demo.MARKUP), tick)
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": hfm_demo.GOLD,
            "side": "SELL",
            "effect": "CLOSE",
            "quantity": qty,
            "limit_price": limit,
            "price_band_low": hfm_demo._tick(bid * (1 - hfm_demo.BAND), tick),
            "price_band_high": hfm_demo._tick(bid * (1 + hfm_demo.BAND), tick, ROUND_CEILING),
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",
            "horizon": "now",
            "thesis": f"{LABEL} robot, HFM demo 2: the trade has been open {why}, the longest "
            "the test's rule holds it, so it is closed. Its stop stays on until HFM has closed it.",
            "plain_summary": f"HFM demo 2 ({LABEL}): sell {qty.normalize():f} ounce of gold after "
            f"{why}.",
            "evidence": [
                {"kind": "QUOTE", "ref": f"quote:{hfm_demo.GOLD}", "summary": f"HFM bid {bid}"}
            ],
            "counterarguments": ["The trade might still have reached its target later."],
            "red_team": "Closing on time can close just before a move to the target.",
            "scenarios": [{"name": "Close", "description": "Closed at HFM's price."}],
            "invalidation": ["Not applicable to an exit"],
            "entry_conditions": f"Limit sell at or above {limit} (the bid − 0.1 %), now or never",
            "closes": list(closes),
        }
    )
