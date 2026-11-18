"""Robot A, the TEST robot, in the simulation (S102; the owner's master specification of
2026-11-18). SIMULATED: nothing is sent to HFM.

Why here: no HFM demo the owner can open (Premium, Zero, Contest; myHF offered no Cent demo on
2026-11-18) trades less than 1 ounce of gold, and the TEST rule's stop (about 13 USD an ounce)
would then risk far more than the owner's 2 USD. Their specification: reject such a trade, never
force it, and keep a SIMULATION mode at 100 USD on HFM's prices. So:

- **The rule, unchanged** (``trend_buy``, the lab's best candidate, measured on 4 years of HFM's
  candles): it buys when the 4-hour and 1-hour trends are both up and the 15-minute momentum has
  just turned up; once per 15-minute candle, acted on at HFM's live price of that moment; the
  stop at 2 × ATR14 of the 15-minute candles below the fill, the target twice as far above, out
  after 24 hours. BUY only. No AI veto (the rule as it was measured); the AI's latest opinion
  is shown with each decision as information.
- **Its own account** (``gold_sim_accounts.rule``): 100 USD of simulated money, apart from the
  gold reading's simulation and from the 1-minute robot on the demo; one trade at a time.
- **The owner's risk:** 1 USD a trade preferred (1 % of 100), never more than 2; 4 USD a day at
  most, the open trade's loss included, counted on HFM's trading day (17:00 New York); a pause
  after three losses in a row; never more risk after a loss.
- **Before every trade** (``evaluate``): the probability of winning and of losing, measured on
  the rule's own 372 trades (not an AI's confidence); the expected reward, loss and value at this
  size; the reward to risk; the confidence; the reasons for and against; what the connected HFM
  demo would have needed for it. The owner's bands: 50 % or more with a positive expected value
  opens by itself, 30 % to 50 % asks the owner, below 30 % is rejected. The rule's 36.6 % asks.
- **The day's count** (``today``): trades completed against the owner's target of 10, the
  signals, and why the others did not trade. The rule finds about 0.36 a day; it is never forced.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import record as audit
from cosmic.core.errors import ConflictError
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.engine import Database
from cosmic.db.schema_gold import (
    gold_sim_accounts,
    gold_sim_answers,
    gold_sim_decisions,
    gold_sim_positions,
)
from cosmic.gold import hfm_demo, probability, second_opinion, sim, trend_buy, trend_test
from cosmic.market import data as md
from cosmic.market import hfm_candles
from cosmic.trading import mandates

RULE = mandates.GOLD_TREND_BUY
LABEL = "SIMULATED"
AMOUNT = Decimal(100)
RISK_PCT = Decimal(1)  # 1 USD a trade, the owner's preferred
MAX_RISK = Decimal(2)  # never more than 2 USD a trade
DAILY_LOSS = Decimal(4)
TARGET_TRADES = 10  # a day, when the market gives that many: never forced
CENT = Decimal("0.01")

CONFIDENCE = "LOW"


@dataclass(frozen=True)
class Record:
    """The rule's measured record for one kind of market (the lab's report of 2026-11-18 on
    trial 24's trades; a description of what happened, never a filter on the rule)."""

    name: str
    p_win: Decimal  # the target came first
    expected_r: Decimal  # a trade, after costs
    trades: int
    where: str


@dataclass(frozen=True)
class Side:
    """One side of the rule and its measured record (the lab's Run 8, trial 24, HFM XAUUSD from
    2022-10-31 to 2026-11-09, HFM's spread 0.35 and swap included; report of 2026-11-18). By
    volatility: the stop distance at the entry (2 × ATR14 of the 15-minute candles, what the
    robot sees before a trade) in thirds of the side's trades."""

    side: str
    version: str
    trades: int
    p_win: Decimal  # the target first
    p_loss: Decimal  # the stop first; the rest closed after 24 hours
    expected_r: Decimal  # a trade, in units of its risk
    t: Decimal
    per_day: Decimal  # its trades a weekday, on average
    calm_below: Decimal
    volatile_from: Decimal
    regimes: tuple[Record, Record, Record]
    recent: Record  # 2026
    worst_drawdown_r: Decimal
    worst_streak: int
    evidence: str

    def regime(self, distance: Decimal) -> Record:
        """Its record in markets as volatile as this one (by its stop distance now)."""
        if distance < self.calm_below:
            return self.regimes[0]
        if distance < self.volatile_from:
            return self.regimes[1]
        return self.regimes[2]


def _regimes(
    calm_below: Decimal,
    volatile_from: Decimal,
    calm: tuple[Decimal, Decimal, int],
    normal: tuple[Decimal, Decimal, int],
    volatile: tuple[Decimal, Decimal, int],
) -> tuple[Record, Record, Record]:
    return (
        Record("calm", *calm, f"stop under {calm_below}"),
        Record("normal", *normal, f"stop {calm_below} to {volatile_from}"),
        Record("volatile", *volatile, f"stop {volatile_from} or more"),
    )


D_ = Decimal
BUY_SIDE = Side(
    side=trend_buy.BUY,
    version=trend_buy.VERSION,
    calm_below=D_("9.46"),
    volatile_from=D_("15.26"),
    regimes=_regimes(
        D_("9.46"),
        D_("15.26"),
        (D_("0.444"), D_("0.369"), 124),
        (D_("0.347"), D_("0.166"), 124),
        (D_("0.306"), D_("-0.002"), 124),
    ),
    trades=372,
    p_win=D_("0.366"),
    p_loss=D_("0.546"),
    expected_r=D_("0.177"),
    t=D_("2.41"),
    per_day=D_("0.36"),
    recent=Record("2026", D_("0.294"), D_("-0.11"), 102, "2026-01-01 to 2026-11-09"),
    worst_drawdown_r=D_("22.6"),
    worst_streak=9,
    evidence=(
        "the BUY side's 372 trades on HFM's gold, 2022-11 to 2026-11 (the lab's Run 8): the "
        "target first 36.6 % (36.3 % on 2025-2026, kept apart for testing), the stop first "
        "54.6 %, the rest closed after 24 hours; +0.177 R a trade after HFM's spread and swap "
        "(t 2.41)"
    ),
)
# The mirror (S106; the owner's choice of 2026-11-18, "Add the SELL side"): about break-even.
SELL_SIDE = Side(
    side=trend_buy.SELL,
    version=trend_buy.SELL_VERSION,
    calm_below=D_("12.16"),
    volatile_from=D_("17.47"),
    regimes=_regimes(
        D_("12.16"),
        D_("17.47"),
        (D_("0.324"), D_("0.059"), 74),
        (D_("0.267"), D_("-0.170"), 75),
        (D_("0.347"), D_("0.200"), 75),
    ),
    trades=224,
    p_win=D_("0.312"),
    p_loss=D_("0.616"),
    expected_r=D_("0.030"),
    t=D_("0.32"),
    per_day=D_("0.23"),
    recent=Record("2026", D_("0.326"), D_("0.046"), 138, "2026-01-01 to 2026-11-09"),
    worst_drawdown_r=D_("13.2"),
    worst_streak=8,
    evidence=(
        "the SELL side's 224 trades on HFM's gold, 2022-11 to 2026-11 (the lab's Run 8): the "
        "target first 31.2 % (32.8 % on 2025-2026), the stop first 61.6 %; +0.030 R a trade "
        "after HFM's spread (t 0.32): about break-even, no evidence of an edge"
    ),
)
SIDES = {trend_buy.BUY: BUY_SIDE, trend_buy.SELL: SELL_SIDE}
# The BUY side's figures under their earlier names (the pages, the tests, the reports).
TRADES, P_WIN, P_LOSS, EXPECTED_R = (
    BUY_SIDE.trades,
    BUY_SIDE.p_win,
    BUY_SIDE.p_loss,
    BUY_SIDE.expected_r,
)
REGIMES, RECENT = BUY_SIDE.regimes, BUY_SIDE.recent
CALM_BELOW, VOLATILE_FROM = BUY_SIDE.calm_below, BUY_SIDE.volatile_from
PER_DAY = BUY_SIDE.per_day + SELL_SIDE.per_day  # both sides: about 0.59 a weekday
EVIDENCE = BUY_SIDE.evidence


def regime(distance: Decimal, side: str = trend_buy.BUY) -> Record:
    """The rule's record (that side's) in markets as volatile as this one."""
    return SIDES[side].regime(distance)


def settings() -> sim.Settings:
    """The owner's rules for Robot A: 100 USD, 1 USD a trade, 4 USD a day, one trade at a time,
    BUY and SELL (S106); each trade asks the owner unless the bands, or the owner's approval in
    advance, let it open by itself."""
    return sim.Settings(
        amount=AMOUNT,
        risk_pct=RISK_PCT,
        daily_loss=DAILY_LOSS,
        max_open_risk_pct=MAX_RISK,  # one trade's risk at once
        max_drawdown_pct=Decimal(20),
        max_positions=1,
        take_profit_r=Decimal(2),
        allow_sell=True,
        permission="ASK",
    )


def band(p_win: Decimal, ev: Decimal) -> str:
    """The owner's decision rules: AUTO (50 % or more, and a positive expected value after
    costs), ASK (30 % to below 50 %), or REJECT (below 30 %)."""
    if p_win >= probability.AUTO_AT and ev > 0:
        return probability.AUTO
    if p_win >= probability.ASK_AT:
        return probability.ASK
    return probability.REJECT


async def start(conn: AsyncConnection, *, owner: uuid.UUID, now: datetime) -> uuid.UUID:
    """A new simulated account for Robot A; a TEST simulation running before stops, its records
    kept. The gold reading's simulation and the robots on the demo are not touched."""
    return await sim.create(
        conn,
        name=f"TEST robot {now:%d %b %H:%M}",
        starting_balance=AMOUNT,
        settings=settings(),
        owner=owner,
        now=now,
        rule=RULE,
    )


async def off(conn: AsyncConnection, account_id: uuid.UUID, now: datetime) -> None:
    """The owner switches it off: an open simulated trade closes at HFM's price now (or the next
    one), and nothing more opens."""
    await sim.close_all(conn, account_id, now, "switched off by the owner")
    await sim.set_running(conn, account_id, False, "switched off by the owner", now)


def _bar_due(now: datetime) -> datetime:
    """The 15-minute candle that closed last."""
    return hfm_candles.open_of(now, "15m") - timedelta(minutes=15)


async def decide(db: Database, now: datetime) -> list[str]:
    """Once per 15-minute candle and running Robot A account, at the first fresh HFM price."""
    out: list[str] = []
    async with db.connect() as conn:
        ids = await sim.running_accounts(conn, RULE)
        due = _bar_due(now)
        waiting = [
            a
            for a in ids
            if await conn.scalar(
                select(gold_sim_decisions.c.id).where(
                    gold_sim_decisions.c.account_id == a, gold_sim_decisions.c.bar >= due
                )
            )
            is None
        ]
    if not waiting:
        return out  # nothing to decide: the candles are not read every second for nothing
    async with db.transaction() as conn:
        price = await sim.price_now(conn, now)
        if price is None:
            return out  # decided at the first fresh price (the daily break, the weekend)
        reading = await trend_test.reading_now(conn, now, sell=True)
        if reading.bar is None:
            return out
        for account_id in waiting:
            await sim._lock(conn, account_id)
            if await sim.rule_of(conn, account_id) != RULE or (
                await conn.scalar(
                    select(func.count())
                    .select_from(gold_sim_decisions)
                    .where(
                        gold_sim_decisions.c.account_id == account_id,
                        gold_sim_decisions.c.bar == reading.bar,
                    )
                )
            ):
                continue
            if account_id not in await sim.running_accounts(conn, RULE):
                continue  # stopped meanwhile
            out.append(await _decide_one(conn, account_id, reading, price, now))
    return out


async def evaluate(
    conn: AsyncConnection,
    reading: trend_buy.Reading,
    price: sim.Price,
    sz: sim.Size | None,
    distance: Decimal,
    now: datetime,
) -> dict[str, Any]:
    """What is weighed before a trade (the owner's section 5), recorded with the decision."""
    rec = SIDES[reading.decision]
    up = reading.decision == trend_buy.BUY
    ounces = sz.ounces if sz is not None else ZERO
    per_ounce = sz.per_ounce if sz is not None else distance + sim.costs(price.spread)
    risk = (ounces * per_ounce).quantize(CENT)
    reward = (ounces * 2 * distance).quantize(CENT)
    now_r = rec.regime(distance)  # calibrated on markets like this one (the owner's section 5)
    ev = (now_r.expected_r * risk).quantize(CENT)
    verdict = band(now_r.p_win, now_r.expected_r)
    momentum = (
        f"{reading.rsi_before:.1f} → {reading.rsi:.1f}"
        if reading.rsi is not None and reading.rsi_before is not None
        else "crossed 50"
    )
    supporting = [
        f"the 4-hour trend is {reading.h4} and the 1-hour {reading.h1}",
        f"the 15-minute momentum has just turned {'up' if up else 'down'}: RSI(14) {momentum}, "
        f"the close {'above' if up else 'below'} its 21-candle average",
        f"over 4 years its {rec.side} side made {rec.expected_r:+} R a trade ({rec.trades} trades)",
    ]
    recent = rec.recent
    against = [
        f"in {now_r.name} markets like this one ({now_r.where}) it won {now_r.p_win * 100:.1f} % "
        f"and made {now_r.expected_r:+} R a trade ({now_r.trades} trades)",
        f"in {recent.name} it won {recent.p_win * 100:.1f} % and made {recent.expected_r:+} R a "
        f"trade ({recent.trades} trades); its worst run: {rec.worst_streak} losses in a row, "
        f"{rec.worst_drawdown_r} USD down at 1 USD a trade",
        "it wins below 50 %: by your rules it asks you, unless you approved its trades in advance",
        f"a candidate, not a proven edge: its {rec.side} side's t is {rec.t} (the lab's bar 2.2)"
        + ("; about break-even over 4 years" if not up else ""),
        f"both sides together find about {PER_DAY} trades a day: your target of {TARGET_TRADES} "
        "cannot come from this rule without forcing trades",
    ]
    opinion = await second_opinion.latest(conn)
    if opinion is not None and now - opinion.at <= second_opinion.FRESH:
        line = (
            f"the AI's second opinion ({opinion.model_id}, {opinion.at:%H:%M} UTC) says "
            f"{opinion.recommendation}: {opinion.why} (information only: the rule is tested as "
            "it was measured)"
        )
        (supporting if opinion.recommendation == rec.side else against).append(line)
    return {
        "mode": LABEL,
        "rule": rec.version,
        "side": rec.side,
        "p_win": str(rec.p_win),
        "p_loss": str(rec.p_loss),
        "p_time_exit": str(1 - rec.p_win - rec.p_loss),
        "expected_r": str(rec.expected_r),
        "regime": {
            "name": now_r.name,
            "where": now_r.where,
            "p_win": str(now_r.p_win),
            "expected_r": str(now_r.expected_r),
            "trades": now_r.trades,
        },
        "recent": {
            "name": recent.name,
            "p_win": str(recent.p_win),
            "expected_r": str(recent.expected_r),
            "trades": recent.trades,
        },
        "risk_usd": str(risk),
        "reward_usd": str(reward),
        "expected_loss_usd": str((rec.p_loss * risk).quantize(CENT)),
        "expected_reward_usd": str((rec.p_win * reward).quantize(CENT)),
        "ev_usd": str(ev),
        "reward_to_risk": "1:2",
        "confidence": CONFIDENCE,
        "band": verdict,
        "for": supporting,
        "against": against,
        "demo": await demo_check(conn, price, distance),
        "evidence": rec.evidence,
    }


async def price_step(conn: AsyncConnection) -> Decimal | None:
    """Gold's price step, from the connected terminal's own symbol specification (the owner's
    section 3), else the instrument's stored one."""
    row = await sim._report(conn)
    spec = ((row.snapshot or {}).get("symbols") or {}).get(hfm_candles.SYMBOL) if row else None
    for key in ("tick_size", "point"):
        try:
            step = Decimal(str((spec or {})[key]))
        except (KeyError, ArithmeticError, ValueError):
            continue
        if step > 0:
            return step
    inst = await md.instrument_by_key(conn, hfm_demo.GOLD)
    stored: Decimal | None = inst.tick_size
    return stored


async def demo_check(conn: AsyncConnection, price: sim.Price, distance: Decimal) -> dict[str, str]:
    """What the connected HFM demo would need for this trade, from its own terminal's report:
    its smallest step in ounces and what that step would risk (the owner's sections 3 and 6: a
    step that cannot fit 2 USD means the trade is refused there, never forced)."""
    row = await sim._report(conn)
    step = trend_test.ounce_step((row.snapshot or {}) if row is not None else {})
    if step is None:
        return {"verdict": "the connected demo's gold specification is not reported"}
    per_ounce = distance + sim.costs(price.spread)
    at_step = (step * per_ounce).quantize(CENT)
    fits = trend_buy.ounces(MAX_RISK, distance, sim.costs(price.spread), ZERO, step)
    if fits > 0:
        return {
            "step_oz": str(step.normalize()),
            "risk_at_step_usd": str(at_step),
            "verdict": f"the connected demo could take {fits.normalize():f} oz within "
            f"{MAX_RISK:.2f} USD",
        }
    return {
        "step_oz": str(step.normalize()),
        "risk_at_step_usd": str(at_step),
        "verdict": f"refused on the connected HFM demo: its smallest trade ({step.normalize():f} "
        f"oz) would risk {at_step} USD, more than your {MAX_RISK:.2f}",
    }


async def _decide_one(
    conn: AsyncConnection,
    account_id: uuid.UUID,
    reading: trend_buy.Reading,
    price: sim.Price,
    now: datetime,
) -> str:
    s = await sim.settings_of(conn, account_id)
    if s is not None:
        s = await _sell_added(conn, account_id, s, now)
    decision_id = uuid7()
    reasons = [f"TEST ({trend_buy.VERSION} and {trend_buy.SELL_VERSION}): {reading.why}"]
    action, ounces, opened = "WAIT", None, None
    distance: Decimal | None = None
    evaluation: dict[str, Any] | None = None

    async def record() -> str:
        await conn.execute(
            pg_insert(gold_sim_decisions)
            .values(
                id=decision_id,
                account_id=account_id,
                bar=reading.bar,
                at=now,
                decision=reading.decision,
                action=action,
                reasons=reasons,
                bid=price.bid,
                ask=price.ask,
                ounces=ounces,
                stop_distance=distance,
                position_id=opened,
                evaluation=evaluation,
            )
            .on_conflict_do_nothing(index_elements=["account_id", "bar"])
        )
        return f"{account_id}:{action}"

    # Out after 24 hours: what is still open then closes at HFM's price (the rule's exit).
    for p in await sim.positions(conn, account_id):
        exit_price = price.bid if p.side == trend_buy.BUY else price.ask
        if now - p.entry_at >= trend_buy.HOLD and (
            await sim.close(conn, p, exit_price, "RULE", now) is not None
        ):
            reasons.append(
                f"closed the {p.side} of {p.ounces.normalize():f} oz after 24 hours, as the rule "
                "says"
            )
    if s is None or reading.decision not in SIDES:
        return await record()
    side = reading.decision
    rec = SIDES[side]
    tick = await price_step(conn)
    st = await sim.state(conn, account_id, price, now)
    why = sim.limits_hold(s, st, side) or await sim.halted(conn)
    if why is None and (tick is None or reading.atr is None):
        why = "HFM's gold has no price step or volatility reported yet"
    found: trend_buy.Levels | str | None = None
    sz: sim.Size | None = None
    if why is None:
        assert tick is not None and reading.atr is not None
        found = trend_buy.levels(reading.atr, price.bid, price.ask, tick, ZERO, side)
        if isinstance(found, str):
            why = found
        else:
            distance = found.distance
            entry = price.ask if side == trend_buy.BUY else price.bid
            sz = sim.size(s, st, distance, entry, price.leverage, sim.costs(price.spread))
            why = sz.why_not or sim.day_room(s, st, sz)
            if why is None and sz.ounces * sz.per_ounce > MAX_RISK:
                why = f"this trade would risk more than {MAX_RISK:.2f} USD"  # never, at 1 %
            evaluation = await evaluate(conn, reading, price, sz, distance, now)
    if why is not None or sz is None or distance is None or evaluation is None:
        reasons.append(f"WAIT — {why}")
        return await record()
    ounces = sz.ounces
    plan = sim._plan(side, sz, distance, price, s, 2 * distance)
    now_r = rec.regime(distance)
    summary = (
        f"{LABEL} · Robot A (TEST): {plan.text}. Win {now_r.p_win * 100:.1f} % in "
        f"{now_r.name} markets like this one ({rec.p_win * 100:.1f} % over its {side} side's "
        f"{rec.trades} trades; {rec.recent.p_win * 100:.1f} % in {rec.recent.name}); expected "
        f"value {evaluation['ev_usd']} USD; reward to risk 1:2; confidence {CONFIDENCE}. "
        f"Demo: {evaluation['demo']['verdict']}."
    )
    if evaluation["band"] == probability.REJECT:
        action = "REJECTED"
        reasons.append(f"REJECTED — below 30 %: {summary}")
        return await record()
    asks = asks_why(evaluation, s)
    if asks is not None:
        action = "ASKED"
        reasons.append(
            f"ASKED you: {summary}{asks} Approve or reject it on the gold page within 5 minutes."
        )
        await record()
        await sim._tell(
            conn,
            f"gold-sim:{decision_id}:asked",
            f"MARKET OPPORTUNITY — {summary} Why: {reading.why}. Approve or reject it in the "
            "portal, on the gold page, within 5 minutes.",
            now,
        )
        return f"{account_id}:ASKED"
    opened = uuid7()
    action = "OPENED"
    by_itself = (
        " Opened by itself: you approved Robot A's trades in advance"
        if evaluation["band"] != probability.AUTO
        else ""
    )
    reasons.append(summary + by_itself)
    await record()
    await sim._open(conn, account_id, decision_id, opened, plan, sz, price, now)
    await sim._tell(conn, f"gold-sim:{opened}:opened", f"OPENED — {summary}{by_itself}.", now)
    return f"{account_id}:{side}"


async def _sell_added(
    conn: AsyncConnection, account_id: uuid.UUID, s: sim.Settings, now: datetime
) -> sim.Settings:
    """The owner's choice of 2026-11-18 ("Add the SELL side"): Robot A's account started before
    it sells too, from its next decision; recorded once (a new settings row, audited)."""
    if s.allow_sell:
        return s
    owner: uuid.UUID | None = await conn.scalar(
        select(gold_sim_accounts.c.created_by).where(gold_sim_accounts.c.id == account_id)
    )
    if owner is None:
        return s
    changed = replace(s, allow_sell=True)
    await sim.save_settings(conn, account_id, changed, owner, now)
    await audit(
        conn,
        action="gold_sim.test_robot_sell_added",
        actor=sim.ACTOR,
        occurred_at=now,
        entity_type="gold_sim_account",
        entity_id=account_id,
        payload={"allow_sell": True, "why": "the owner's choice of 2026-11-18: add the SELL side"},
    )
    return changed


def asks_why(evaluation: dict[str, Any], s: sim.Settings) -> str | None:
    """Why this trade waits for the owner's answer, or None when it opens by itself.

    The owner's bands decide (50 % or more with a positive expected value: by itself; 30 % to
    50 %: asks). Since 2026-11-18 the owner may approve Robot A's trades once, in advance
    (``permission`` AUTO, with their code): its trades then open by itself, but only where its
    record in markets like this one has a positive expected value (their section 5: automatic
    only with a positive expected value after costs). Where it made nothing or lost, it asks."""
    band_ = evaluation["band"]
    if band_ == probability.AUTO:
        return None
    if s.permission != "AUTO":
        return ""
    regime_ = evaluation["regime"]
    if Decimal(regime_["expected_r"]) > 0:
        return None
    return (
        f" It asks although you approved its trades in advance: in {regime_['name']} markets "
        f"like this one it made {regime_['expected_r']} R a trade, and your rule opens a trade "
        "by itself only with a positive expected value."
    )


async def set_permission(
    conn: AsyncConnection, account_id: uuid.UUID, auto: bool, owner: uuid.UUID, now: datetime
) -> sim.Settings:
    """The owner's standing answer for Robot A: AUTO (its trades open by itself, approved once
    in advance) or ASK (each trade asks). Only the permission changes: the money, the risk a
    trade, the day's limit and every other rule stay as they were."""
    if await sim.rule_of(conn, account_id) != RULE:
        raise ConflictError("that is not Robot A's account")
    current = await sim.settings_of(conn, account_id)
    if current is None:
        raise ConflictError("Robot A has no settings")
    wanted = "AUTO" if auto else "ASK"
    if current.permission == wanted:
        return current
    changed = replace(current, permission=wanted)
    await sim.save_settings(conn, account_id, changed, owner, now)
    return changed


# ---- the rolling record (the owner's section 8; S104) -----------------------------------------

HIGH_WIN = Decimal("0.80")  # the owner's threshold for the high-performance mode
HIGH_MIN = 50  # completed trades it needs first


@dataclass(frozen=True)
class Window:
    """The last ``size`` completed trades (fewer while it has fewer), swap included."""

    size: int
    trades: int
    net: Decimal = ZERO
    win_rate: Decimal | None = None
    profit_factor: Decimal | None = None  # None: no losing trade yet
    expectancy: Decimal | None = None  # USD a trade
    expectancy_r: Decimal | None = None  # in units of each trade's risk at its stop
    max_drawdown: Decimal = ZERO  # USD, from the best of the running result


def window(closed: list[sim.Position], size: int) -> Window:
    """``closed``: newest first, as ``sim.positions`` lists them."""
    last = [p for p in closed if p.result is not None][:size][::-1]  # oldest first
    if not last:
        return Window(size, 0)
    nets = [p.result + p.swap for p in last if p.result is not None]
    risks = [p.ounces * abs(p.entry_price - p.stop_price) for p in last]
    won = sum((x for x in nets if x > 0), ZERO)
    lost = -sum((x for x in nets if x < 0), ZERO)
    running = peak = drawdown = ZERO
    for x in nets:
        running += x
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    rs = [x / r for x, r in zip(nets, risks, strict=True) if r > 0]
    n = len(nets)
    return Window(
        size,
        n,
        sum(nets, ZERO).quantize(CENT),
        (Decimal(sum(1 for x in nets if x > 0)) / n).quantize(Decimal("0.001")),
        (won / lost).quantize(CENT) if lost > 0 else None,
        (sum(nets, ZERO) / n).quantize(CENT),
        (sum(rs, ZERO) / len(rs)).quantize(Decimal("0.001")) if rs else None,
        drawdown.quantize(CENT),
    )


@dataclass(frozen=True)
class Performance:
    last20: Window
    last50: Window

    @property
    def mode(self) -> str:
        """Section 8 in the owner's words: the mode, and why. It never raises the risk, and the
        rule decides once per 15-minute candle, so there is no more frequency to give."""
        w = self.last50
        if w.trades < HIGH_MIN:
            return (
                f"NORMAL: the high-performance mode needs {HIGH_MIN} completed trades with a win "
                f"rate above {HIGH_WIN * 100:.0f} %; {w.trades} so far"
            )
        assert w.win_rate is not None and w.expectancy is not None
        if w.expectancy <= 0:
            return (
                f"CAUTION: over the last {w.trades} trades the expected value is "
                f"{w.expectancy:+} USD a trade; every trade still asks you, three losses in a row "
                "pause it, and a 20 % drop from its best stops it"
            )
        if w.win_rate > HIGH_WIN:
            return (
                f"HIGH PERFORMANCE: {w.win_rate * 100:.1f} % won over the last {w.trades} trades, "
                "with a positive expected value; the risk a trade stays the same, and the rule "
                "already decides at every 15-minute candle"
            )
        return (
            f"NORMAL: {w.win_rate * 100:.1f} % won over the last {w.trades} trades (the "
            f"high-performance mode needs above {HIGH_WIN * 100:.0f} %); a 1:2 rule makes money "
            "from about 34 %"
        )


async def performance(conn: AsyncConnection, account_id: uuid.UUID) -> Performance:
    closed = await sim.positions(conn, account_id, status="CLOSED", limit=HIGH_MIN)
    return Performance(window(closed, 20), window(closed, HIGH_MIN))


# ---- what the owner sees ----------------------------------------------------------------------


@dataclass(frozen=True)
class Today:
    """The HFM trading day so far (from 17:00 New York): trades completed against the target,
    the rule's signals, and why the others did not trade."""

    since: datetime
    completed: int
    signals: int
    asked: int
    opened: int
    result: Decimal
    missed: list[tuple[str, int]] = field(default_factory=list)  # (why, how many)
    target: int = TARGET_TRADES

    @property
    def short_of_target(self) -> str | None:
        if self.completed >= self.target:
            return None
        return (
            f"{self.completed} of {self.target}: the TEST rule gave {self.signals} signal(s) "
            f"today; its two sides find about {PER_DAY} a day over 4 years, and a trade is never "
            "forced to reach the target (your section 7)"
        )


async def today(conn: AsyncConnection, account_id: uuid.UUID, now: datetime) -> Today:
    since = sim.trading_day(now)
    completed = int(
        await conn.scalar(
            select(func.count())
            .select_from(gold_sim_positions)
            .where(
                gold_sim_positions.c.account_id == account_id,
                gold_sim_positions.c.status == "CLOSED",
                gold_sim_positions.c.exit_at >= since,
            )
        )
        or 0
    )
    rows = list(
        await conn.execute(
            select(gold_sim_decisions.c.action, gold_sim_decisions.c.reasons).where(
                gold_sim_decisions.c.account_id == account_id,
                gold_sim_decisions.c.at >= since,
                gold_sim_decisions.c.decision.in_((trend_buy.BUY, trend_buy.SELL)),
            )
        )
    )
    missed: dict[str, int] = {}
    for r in rows:
        if r.action == "WAIT":
            why = str(r.reasons[-1]).removeprefix("WAIT — ")
            missed[why] = missed.get(why, 0) + 1
    st = await sim.state(conn, account_id, None, now)
    return Today(
        since,
        completed,
        len(rows),
        sum(1 for r in rows if r.action == "ASKED"),
        sum(1 for r in rows if r.action == "OPENED"),
        st.today,
        sorted(missed.items(), key=lambda x: -x[1]),
    )


@dataclass
class Panel:
    """Robot A's simulation on the gold page."""

    account: Any | None  # its account row (the latest), None before it was ever started
    state: sim.State | None = None
    settings: sim.Settings | None = None
    today: Today | None = None
    open: list[sim.Position] = field(default_factory=list)
    decisions: list[Any] = field(default_factory=list)
    asked: Any | None = None  # the trade waiting for the owner's answer, if any
    reading: trend_buy.Reading | None = None
    performance: Performance | None = None  # the last 20 and 50 completed trades (S104)

    @property
    def records(self) -> list[tuple[Side, Record]]:
        """Each side's record in markets as volatile as now (by the stop it would use now)."""
        if self.reading is None or self.reading.atr is None:
            return []
        distance = trend_buy.STOP_ATR * Decimal(str(self.reading.atr))
        return [(side, side.regime(distance)) for side in SIDES.values()]

    @property
    def chances(self) -> list[tuple[Side, Record | None]]:
        """Each side, with its record in markets like now when the reading says how volatile."""
        now = {side.side: rec for side, rec in self.records}
        return [(side, now.get(side.side)) for side in SIDES.values()]


async def panel(conn: AsyncConnection, now: datetime) -> Panel:
    row = (
        await conn.execute(
            select(gold_sim_accounts)
            .where(gold_sim_accounts.c.rule == RULE)
            .order_by(desc(gold_sim_accounts.c.created_at))
            .limit(1)
        )
    ).first()
    out = Panel(row, reading=await trend_test.reading_now(conn, now))
    if row is None:
        return out
    price = await sim.price_now(conn, now)
    out.state = await sim.state(conn, row.id, price, now)
    out.settings = await sim.settings_of(conn, row.id)
    out.today = await today(conn, row.id, now)
    out.open = await sim.positions(conn, row.id)
    out.performance = await performance(conn, row.id)
    out.decisions = list(
        await conn.execute(
            select(gold_sim_decisions)
            .where(gold_sim_decisions.c.account_id == row.id)
            .order_by(desc(gold_sim_decisions.c.bar))
            .limit(8)
        )
    )
    waiting = next((d for d in out.decisions if d.action == "ASKED"), None)
    if waiting is not None and now - waiting.at <= sim.ASK_VALID:
        answered = await conn.scalar(
            select(gold_sim_answers.c.answer).where(gold_sim_answers.c.decision_id == waiting.id)
        )
        out.asked = waiting if answered is None else None
    return out
