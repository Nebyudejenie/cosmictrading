"""Robot A on its own HFM demo (S98, the TEST robot; S110, the owner's choice of 2026-11-20:
"real way, not simulation", and "real demo orders at 1 oz"): the gold lab's best candidate
(``cosmic.gold.trend_buy``, BUY and its SELL mirror) as real orders on the second HFM demo account
(terminal 2), with virtual money.

- **Once per 15-minute candle and mandate** (``gold_hfm_decisions``), at the first fresh HFM price
  after the candle closed: the rule's reading on HFM's own 15-minute and 1-hour candles (the
  primary terminal's, the same market).
- **One trade at a time, HFM's smallest trade and never more** (1 ounce on a standard account):
  the stop-loss (2 × ATR14 of the 15-minute candles) and the take-profit (twice as far) travel
  with the order and are held by HFM; after 24 hours the robot closes what is still open.
- **Its risk:** that smallest trade's loss at the stop, with the entry's allowance and the fee
  both ways, stays within the mandate's loss per trade (the owner's: at most 30 USD of the 100 USD
  demo; about 10 to 25 at 2026's volatility). A market too volatile for that waits.
- **The owner's bands**, on the rule's record in markets as volatile as this one
  (``trend_sim.SIDES``, by the stop distance): below 30 % it rejects; otherwise it opens by itself
  where that record made money after costs (the owner approved its trades once, at START: their
  section 5, automatic only with a positive expected value), and leaves the trade where it did
  not. Either way the owner is told, on Telegram, what it did and why. **On a demo in learning
  mode** (S124, ``gold.learning``, the owner's choice of 2026-11-28) it takes every signal by
  itself, below 30 % and without a positive record too; its money limits still hold.
- **The owner's rules** (``gold.controls``): the daily loss limit is never passed by one more
  trade, and three losses in a row pause it until the owner resumes (in learning mode both stop
  it for the rest of the day only). The account's own limits
  (the policy's ``account_limits``, set at START): a stop at 60 % below its best.
- **Speed (S87):** the order is sent in the same step, within a second of the decision, or not
  at all; only while that terminal's fast lane is up.
- **No AI second opinion:** the rule is tested as it was measured.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.audit.service import record as record_audit
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_execution import trade_intents, trading_mandates
from cosmic.db.schema_gold import gold_hfm_decisions
from cosmic.db.schema_trading import bridge_fast
from cosmic.gold import (
    controls,
    daily,
    hfm_demo,
    hfm_selftest,
    learning,
    probability,
    sim,
    trend_buy,
)
from cosmic.identity.service import Principal
from cosmic.market import data as md
from cosmic.market import hfm_candles
from cosmic.notify.outbox import notify_owners
from cosmic.trading import mandates
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.brokers.mt5_bridge import BY_ADAPTER, FAST_VERSION, bridge_version
from cosmic.trading.policy import AccountLimits, active_policy, change_policy
from cosmic.trading.proposals import Candidate

RULE = mandates.GOLD_TREND_BUY
M15_CANDLES, H1_CANDLES = 500, 800  # what the lab's engine check used: identical signals
# The entry's limit beyond the price, counted in the loss. 0.30 (S110; 0.10 before): the 1-minute
# robot's 0.10 left three sells unfilled in a falling market (2026-11-18), and on a stop of 10 to
# 25 USD an ounce 0.30 is about 1 to 3 % of the risk.
SLIPPAGE = Decimal("0.30")
LABEL = "Robot A"
STEPS = 1  # HFM's smallest trade, never more (the owner's "at 1 oz")
CENT = Decimal("0.01")
# What START grants (the owner's choice of 2026-11-20): 100 USD of the demo, at most 30 of it a
# trade, a day stops after one trade's worth lost, the robot stops for good after losing 60 in
# all, and the account's own stop 60 % below its best.
AMOUNT = Decimal(100)
RISK_PCT = mandates.GOLD_TREND_MAX_RISK  # 30 % of the amount: at most 30 USD a trade
DAILY_LOSS = Decimal(30)
TOTAL_LOSS = Decimal(60)
DRAWDOWN_PCT = Decimal(60)
OPEN_RISK_PCT = Decimal(75)  # one trade of up to 30 USD while the account is down to its stop


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
    """For each active mandate of Robot A on its demo, once per new 15-minute candle."""
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


def terms_for(alias: str) -> mandates.MandateTerms:
    """What START grants on Robot A's demo account (the owner's choice of 2026-11-20)."""
    return mandates.terms_from_money(
        account_alias=alias,
        money=AMOUNT,
        care="NORMAL",
        mode="AUTO",
        instruments=(hfm_demo.GOLD,),
        strategy_key=RULE,
        timeframe=mandates.HOURLY,
        days=mandates.UNTIL_STOPPED_DAYS,
        risk_pct=RISK_PCT,
        open_positions=1,
        daily_loss=DAILY_LOSS,
        total_loss=TOTAL_LOSS,
    )


async def own_limits(
    conn: AsyncConnection, *, principal: Principal, alias: str, now: datetime, settings: Settings
) -> bool:
    """That demo account's own loss limits in the risk policy (60 % below its best; one trade of
    up to 75 % of its equity at risk), with the owner's code; True when the policy changed."""
    version, policy = await active_policy(conn)
    own = AccountLimits(
        account=alias, max_drawdown_pct=DRAWDOWN_PCT, max_open_risk_pct=OPEN_RISK_PCT
    )
    if own in policy.account_limits:
        return False
    params = policy.model_dump()
    params["account_limits"] = [
        x.model_dump() for x in policy.account_limits if x.account != alias
    ] + [own.model_dump()]
    await change_policy(
        conn,
        principal=principal,
        new_params=params,
        reason=(
            f"Robot A on its own HFM demo ({alias}), the owner's choice of 2026-11-20 (real demo "
            f"orders at 1 oz): for that demo account alone, new trades stop {DRAWDOWN_PCT} % "
            f"below its best, and one trade of up to {OPEN_RISK_PCT} % of its equity may be at "
            "risk; every other account keeps the policy's own limits; with the owner's code"
        )[:500],
        now=now,
        settings=settings,
        expected_version=version,
    )
    return True


async def start(
    conn: AsyncConnection,
    *,
    account: Account,
    now: datetime,
    settings: Settings,
    principal: Principal | None = None,
    owner: uuid.UUID | None = None,
    actor: Actor | None = None,
) -> uuid.UUID:
    """START Robot A on its HFM demo account: its mandate (with the owner's code now, or from
    their approval in advance: ``owner`` and ``actor``), its rules beside it, and its simulation
    stopped (the owner: "real way, not simulation"). Robot B's account is never touched."""
    from cosmic.gold import trend_sim  # it reads this module: imported once both are loaded

    running = await conn.scalar(
        select(trading_mandates.c.id).where(
            trading_mandates.c.account_id == account.id,
            trading_mandates.c.status == "ACTIVE",
        )
    )
    terms = terms_for(account.alias)
    if principal is not None:
        granted = await mandates.grant(
            conn,
            principal=principal,
            terms=terms,
            now=now,
            settings=settings,
            replace=running is not None,
        )
        by = principal.actor
    else:
        assert owner is not None and actor is not None
        granted = await mandates.grant_approved_in_advance(
            conn,
            owner=owner,
            actor=actor,
            terms=terms,
            now=now,
            settings=settings,
            replace=running is not None,
        )
        by = actor
    await controls.create(
        conn,
        controls.Controls(
            granted, pause_after_losses=sim.PAUSE_AFTER_LOSSES, take_profit_r=Decimal(2)
        ),
        now=now,
        actor=by,
    )
    for sim_id in await sim.running_accounts(conn, trend_sim.RULE):
        await trend_sim.off(conn, sim_id, now)
        await record_audit(
            conn,
            action="gold_sim.test_robot_moved_to_demo",
            actor=by,
            occurred_at=now,
            entity_type="gold_sim_account",
            entity_id=sim_id,
            payload={"demo_account": account.alias, "mandate": str(granted)},
        )
    return granted


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


async def _tell(conn: AsyncConnection, key: str, message: str, now: datetime) -> None:
    await notify_owners(conn, kind="gold_sim", payload={"text": message}, now=now, dedup_key=key)


async def _decide_one(conn: AsyncConnection, m: mandates.Mandate, now: datetime) -> str:
    # trend_sim reads this module (its demo check): imported here, once both are loaded.
    from cosmic.gold import trend_sim

    current = await mandates.get_mandate(conn, m.id)
    if not current.active(now):
        return ""
    reading = await reading_now(conn, now, sell=True)
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
    reasons = [f"{LABEL} ({trend_buy.VERSION} and {trend_buy.SELL_VERSION}): {reading.why}"]
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
    longs = await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)}, "BUY")
    shorts = await hfm_demo._held_entries(conn, account, inst.id, {str(m.id)}, "SELL")
    held = longs + shorts
    in_flight = await hfm_demo._in_flight(conn, account, inst.id)
    # Out after 24 hours: what is still open then is closed at the market (the rule's exit).
    for entries, long in ((longs, True), (shorts, False)):
        old = []
        for entry, qty in entries:
            opened = await _opened_at(conn, entry)
            if opened is not None and now - opened >= trend_buy.HOLD:
                old.append((entry, qty))
        if old and not in_flight:
            qty = sum((q for _, q in old), ZERO)
            candidate = _exit(
                account,
                inst.tick_size,
                price.bid if long else price.ask,
                qty,
                [e for e, _ in old],
                "24 hours",
                long=long,
            )
            intent_id, refused = await hfm_demo._propose(conn, m, candidate, now)
            action = "EXIT" if refused is None else "REFUSED"
            reasons.append(
                f"closing the {'buy' if long else 'sell'} of {qty.normalize():f} ounce after 24 "
                "hours, as the rule says" + (f" (refused: {refused})" if refused else "")
            )
            return await record()
    # The owner's rules (daily loss, three losses in a row), as for the other robots.
    ctl = await controls.get(conn, m.id)
    res: controls.Results | None = None
    learn = learning.on(account)  # S124: a demo trades every signal, stopped for the day at most
    if ctl is not None:
        res = await controls.results(conn, m, ctl, now, held, price.bid, price.ask)
        verdict = controls.judge(m, ctl, res, learning=learn)
        if verdict is not None:
            await controls.set_status(
                conn, m.id, verdict[0], verdict[1], now=now, actor=controls.ACTOR
            )
            ctl = await controls.get(conn, m.id)
            assert ctl is not None
        if ctl.status in controls.SHUT or ctl.status == controls.PAUSED:
            reasons.append(f"WAIT — {ctl.status.replace('_', ' ').lower()}: {ctl.status_reason}")
            return await record()
        stopped_today = controls.day_stop(m, ctl, res) if learn else None
        if stopped_today is not None:
            reasons.append(stopped_today)
            return await record()
    side = reading.decision
    if side not in trend_sim.SIDES:
        return await record()
    if held:
        reasons.append("WAIT — Robot A holds one trade at a time, and one is open")
        return await record()
    if in_flight:
        reasons.append("WAIT — a trade of this gold is already on its way")
        return await record()
    _, policy = await active_policy(conn)
    if side == trend_buy.SELL and not policy.shorts_allowed(inst.venue_key, inst.asset_class):
        reasons.append(hfm_demo.NO_SHORTS)
        return await record()
    problem = await lane_problem(conn, account, now)
    if problem is not None:
        reasons.append(f"WAIT — an order could not leave within a second: {problem}")
        return await record()
    assert reading.atr is not None
    found = trend_buy.levels(reading.atr, price.bid, price.ask, inst.tick_size, SLIPPAGE, side)
    if isinstance(found, str):
        reasons.append(f"WAIT — {found}")
        return await record()
    distance = found.distance
    step = ounce_step(await hfm_selftest._report(conn, account.id))
    if step is None:
        reasons.append("WAIT — terminal 2 has not reported its gold's volume step yet")
        return await record()
    fee = 2 * found.limit * policy.fee_bps_for(inst.venue_key) / 10_000  # both ways, an ounce
    qty = step * STEPS
    risk = (qty * (found.distance + SLIPPAGE + fee)).quantize(CENT, ROUND_CEILING)
    rec = trend_sim.SIDES[side]
    now_r = rec.regime(found.distance)
    band_ = trend_sim.band(now_r.p_win, now_r.expected_r)
    ev = (now_r.expected_r * risk).quantize(CENT)
    weighed = (
        f"{side} {qty.normalize():f} oz: in {now_r.name} markets like this one ({now_r.where}) "
        f"its {side} side won {now_r.p_win * 100:.1f} % and made {now_r.expected_r:+} R a trade "
        f"({now_r.trades} trades), {ev:+,.2f} USD expected on {risk:,.2f} at risk; stop "
        f"{found.stop}, target {found.take_profit}, 1:2"
    )
    stamp = f"{reading.bar:%Y-%m-%d %H:%M}"
    if risk > m.max_loss_per_trade:
        why = (
            f"HFM's smallest gold trade ({qty.normalize():f} ounce) would lose about {risk:,.2f} "
            f"at the stop, more than the {m.max_loss_per_trade:,.2f} a trade you allow: gold is "
            "too volatile for it now"
        )
        reasons.append(f"WAIT — {why}")
        await _tell(
            conn,
            f"gold-test:{m.id}:too-big:{stamp}",
            f"HFM DEMO 2 ({LABEL}): a {side} was not taken. {why}.",
            now,
        )
        return await record()
    if band_ == probability.REJECT and not learn:
        reasons.append(f"WAIT — REJECTED, below your 30 %: {weighed}")
        await _tell(
            conn,
            f"gold-test:{m.id}:rejected:{stamp}",
            f"HFM DEMO 2 ({LABEL}): a {side} signal REJECTED, below your 30 %: {weighed}.",
            now,
        )
        return await record()
    if now_r.expected_r <= 0 and not learn:
        reasons.append(
            f"WAIT — not taken: {weighed}. Your rule opens a trade by itself only with a "
            "positive expected value"
        )
        await _tell(
            conn,
            f"gold-test:{m.id}:not-taken:{stamp}",
            f"HFM DEMO 2 ({LABEL}): a {side} signal NOT TAKEN: {weighed}. In such markets its "
            "record made no money, and your rule opens a trade by itself only with a positive "
            "expected value.",
            now,
        )
        return await record()
    if res is not None:
        ceiling = controls.day_room(m, res, risk)
        if ceiling is not None:
            reasons.append(ceiling)
            return await record()
    candidate = _entry(account, price.bid, price.ask, found, qty, risk, reading, side, weighed)
    extra = {
        "rule": RULE,
        "model": trend_buy.VERSION if side == trend_buy.BUY else trend_buy.SELL_VERSION,
        "test": True,
        "regime": now_r.name,
        "p_win": str(now_r.p_win),
        "expected_r": str(now_r.expected_r),
        "decided_at": now.isoformat(),
        "send_within_ms": int(hfm_demo.SEND_WITHIN.total_seconds() * 1000),
        "learning": learn,
    }
    # The checks judge the trade by the price it was decided on (S87).
    await hfm_candles.record_quote(conn, now, price.bid, price.ask, price.at)
    intent_id, refused = await hfm_demo._propose(conn, m, candidate, now, extra, approve=True)
    action = "PROPOSED" if refused is None else "REFUSED"
    ounces = qty
    sent = "sent to HFM's demo 2" + (
        " by learning mode (your choice of 2026-11-28: every signal on the demo, by itself)"
        if learn
        else ""
    )
    reasons.append(f"{weighed}: " + (sent if refused is None else f"refused: {refused}"))
    if refused is not None:
        await _tell(
            conn,
            f"gold-test:{m.id}:refused:{stamp}",
            f"HFM DEMO 2 ({LABEL}): a {side} signal was refused before it was sent: {refused}.",
            now,
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
    side: str = trend_buy.BUY,
    weighed: str = "",
) -> Candidate:
    """A buy (or a sell, a short) at the limit or better, now or never; its stop-loss and
    take-profit with it."""
    buy = side == trend_buy.BUY
    trend = "uptrend" if buy else "downtrend"
    return Candidate.model_validate(
        {
            "account_alias": account.alias,
            "instrument_key": hfm_demo.GOLD,
            "side": side,
            "effect": "OPEN",
            "quantity": qty,
            "limit_price": lv.limit,
            "price_band_low": lv.stop if buy else lv.limit,
            "price_band_high": lv.limit if buy else lv.stop,
            "max_slippage_bps": Decimal(15),
            "time_in_force": "IOC",
            "horizon": "hours (at most 24)",
            "thesis": (
                f"{LABEL}, HFM demo 2 (virtual money): a forward test of the gold lab's best "
                f"candidate. {reading.why}. Stop-loss {lv.stop} (2 × the 15-minute ATR "
                f"{'below the ask' if buy else 'above the bid'}) and take-profit {lv.take_profit} "
                "(twice as far), both held by HFM; closed after 24 hours at the latest. "
                f"{weighed}. Not a proven edge: {trend_buy.EVIDENCE}."
            )[:4000],
            "plain_summary": (
                f"HFM demo 2 ({LABEL}): {'buy' if buy else 'sell'} {qty.normalize():f} ounce of "
                f"gold in the 4-hour and 1-hour {trend}. Stop {lv.stop}, target {lv.take_profit}, "
                f"at most {risk:,.2f} to lose. A forward test, not a proven edge."
            )[:600],
            "evidence": _evidence(reading, bid, ask),
            "counterarguments": [
                "In the lab the target came first only about 31 to 37 times in 100.",
                "The rule did not beat random timing; its BUY side lost in 2026.",
            ],
            "red_team": (
                f"A{'n up' if buy else ' down'}trend can end at any moment; gold can gap past the "
                "stop on news, and HFM then closes at the next price."
            ),
            "scenarios": [
                {"name": "Target", "description": f"Gold reaches {lv.take_profit}: HFM closes."},
                {"name": "Stop", "description": f"Gold reaches {lv.stop}: HFM closes."},
                {"name": "Time", "description": "Neither in 24 hours: the robot closes it."},
            ],
            "invalidation": [
                f"HFM's bid at or below {lv.stop}" if buy else f"HFM's ask at or above {lv.stop}"
            ],
            "entry_conditions": (
                f"Limit buy at or below {lv.limit} (the ask + {SLIPPAGE}), now or never"
                if buy
                else f"Limit sell at or above {lv.limit} (the bid − {SLIPPAGE}), now or never"
            ),
            "management_plan": {
                "stop_loss": {"stop_price": lv.stop, "limit_price": lv.stop},
                "take_profit_price": lv.take_profit,
            },
        }
    )


def _exit(
    account: Account,
    tick: Decimal,
    price: Decimal,
    qty: Decimal,
    closes: Sequence[Any],
    why: str,
    *,
    long: bool = True,
) -> Candidate:
    """The time exit: for a buy a sell at the bid − 0.1 % at least, for a sell (a short) a buy
    at the ask + 0.1 % at most; now or never; the stop stays on until HFM has closed it."""
    limit = (
        hfm_demo._tick(price * (1 - hfm_demo.MARKUP), tick)
        if long
        else hfm_demo._tick(price * (1 + hfm_demo.MARKUP), tick, ROUND_CEILING)
    )
    what = "buy" if long else "sell"
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
            "thesis": f"{LABEL}, HFM demo 2: the {what} has been open {why}, the longest the "
            "rule holds it, so it is closed. Its stop stays on until HFM has closed it.",
            "plain_summary": f"HFM demo 2 ({LABEL}): {'sell' if long else 'buy back'} "
            f"{qty.normalize():f} ounce of gold after {why}.",
            "evidence": [
                {
                    "kind": "QUOTE",
                    "ref": f"quote:{hfm_demo.GOLD}",
                    "summary": f"HFM {'bid' if long else 'ask'} {price}",
                }
            ],
            "counterarguments": ["The trade might still have reached its target later."],
            "red_team": "Closing on time can close just before a move to the target.",
            "scenarios": [{"name": "Close", "description": "Closed at HFM's price."}],
            "invalidation": ["Not applicable to an exit"],
            "entry_conditions": (
                f"Limit sell at or above {limit} (the bid − 0.1 %), now or never"
                if long
                else f"Limit buy at or below {limit} (the ask + 0.1 %), now or never"
            ),
            "closes": list(closes),
        }
    )
