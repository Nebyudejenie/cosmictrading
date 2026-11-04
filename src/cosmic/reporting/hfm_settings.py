"""Your gold auto-trader: you set the amount, the system sizes the gold (S60).

The owner (2026-11-04): "I decide the amount. The system calculates the position size." Two
modes, kept apart:

- **Internal simulation** (``hfm-sim``, SIMULATED): a paper account on HFM's own gold prices
  with an HFM Cent account's rules (from 0.01 ounce, in 0.01 steps), its costs and overnight
  swap. Small amounts trade here. Nothing is sent to HFM.
- **HFM demo** (``hfm-demo``): HFM's own demo account and rules. The smallest trade is
  0.01 lot = 1 ounce. When the amount is too small for it, the preview says why, with what would
  fit, and the simulation stays available.

The preview is computed by the auto-trader's own sizing (``auto.size_entry``) from today's price
and volatility, so what the owner sees before starting is what the auto-trader would do now. Gold
is held on margin (``mandates.CFD_EXPOSURE``): each trade is sized by the loss at its stop, and
never holds more than ten times the amount.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import ConflictError, NotFoundError
from cosmic.core.labels import DataLabel
from cosmic.core.money import BPS, ZERO
from cosmic.db.schema_trading import bridge_state, positions
from cosmic.market import data as md
from cosmic.research.library import auto, ta
from cosmic.trading import mandates, venue_terms
from cosmic.trading.accounts import Account, get_account
from cosmic.trading.brokers import mt5_bridge
from cosmic.trading.context_builder import account_state
from cosmic.trading.paper_accounts import open_paper_account
from cosmic.trading.policy import active_policy
from cosmic.trading.risk.context import AccountState

SIMULATION, HFM_DEMO = "SIMULATION", "HFM_DEMO"
MODES = {
    SIMULATION: "Internal simulation: HFM's gold prices, from 0.01 ounce, nothing sent to HFM",
    HFM_DEMO: "HFM demo: HFM's own demo account; its smallest trade is 0.01 lot = 1 ounce",
}
ACCOUNTS = {SIMULATION: "hfm-sim", HFM_DEMO: "hfm-demo"}
GOLD = {SIMULATION: "CFD:XAU/USD@HFM_CENT", HFM_DEMO: "CFD:XAU/USD@HFM"}
SIM_VENUE = "HFM_CENT"
SIM_CAPITAL = Decimal(10_000)  # the simulated account's virtual money, as a demo has its own
RULE, TIMEFRAME = "turtle_20_10", mandates.HOURLY  # the rule and bars of the HFM auto-trader
CENT = Decimal("0.01")
SIMULATED_ADAPTER = "paper"


@dataclass(frozen=True)
class Choice:
    """What the owner chose on the settings page."""

    mode: str = SIMULATION
    amount: Decimal = Decimal(100)
    risk_pct: Decimal = Decimal(2)
    daily_loss: Decimal | None = None  # None: three trades' worth
    open_positions: int = 1
    automatic: bool = True  # False: each trade waits for the owner's approval


@dataclass(frozen=True)
class Preview:
    choice: Choice
    account: str
    account_exists: bool
    balance: Decimal | None = None  # the account's money: HFM's balance, or the simulated cash
    free_margin: Decimal | None = None  # HFM's free margin, or the simulated cash
    leverage: Decimal | None = None  # HFM's, from its report
    max_loss: Decimal = ZERO
    daily_loss: Decimal = ZERO
    total_loss: Decimal = ZERO
    cap: Decimal = ZERO  # the most gold may be worth: the amount times CFD_EXPOSURE
    bid: Decimal | None = None
    ask: Decimal | None = None
    price_label: str = ""
    contract: Decimal | None = None  # ounces in one lot
    smallest: Decimal | None = None  # ounces
    step: Decimal | None = None  # ounces
    atr: Decimal | None = None  # ATR(20) of the hourly bars the rule reads
    entry: Decimal = ZERO
    stop: Decimal = ZERO
    stop_distance: Decimal = ZERO  # per ounce
    stop_pct: Decimal = ZERO
    ounces: Decimal = ZERO
    lots: Decimal | None = None
    value: Decimal = ZERO
    margin: Decimal | None = None
    spread_cost: Decimal = ZERO
    fees: Decimal = ZERO  # commission on both sides, from the risk policy's venue fee
    swap_night: Decimal | None = None  # what one night costs (negative) or earns, held long
    loss_at_stop: Decimal = ZERO  # with fees; the spread is in the entry and the stop
    smallest_loss: Decimal | None = None  # the smallest trade's loss at the stop
    ok: bool = False
    why: str = ""
    fix: list[str] = field(default_factory=list)


def _q(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places))


def _up(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_CEILING)


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"


def new_simulation_state(now: datetime) -> AccountState:
    """The simulated account as it opens: its virtual money, nothing held (for a preview before
    it exists)."""
    return AccountState(
        account_id="new",
        alias=ACCOUNTS[SIMULATION],
        kind="PAPER",
        status="ACTIVE",
        venue_key=SIM_VENUE,
        reconciliation_status="OK",
        last_reconciled_at=now,
        cash_available=SIM_CAPITAL,
        positions=(),
        open_orders=(),
        unknown_orders=0,
        equity=SIM_CAPITAL,
        high_water_mark=SIM_CAPITAL,
        realized_pnl_today=ZERO,
        unrealized_pnl=ZERO,
        loss_streak=0,
    )


async def _hfm_account(conn: AsyncConnection) -> dict[str, Any]:
    snap = await conn.scalar(
        select(bridge_state.c.snapshot).where(bridge_state.c.bridge == mt5_bridge.BRIDGE)
    )
    return dict((snap or {}).get("account") or {})


def _d(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except ArithmeticError:
        return None


async def preview(conn: AsyncConnection, now: datetime, choice: Choice) -> Preview:
    """What the auto-trader would buy now with ``choice``, every figure shown, or why nothing."""
    alias, key = ACCOUNTS[choice.mode], GOLD[choice.mode]
    try:
        account: Account | None = await get_account(conn, alias=alias)
    except NotFoundError:
        account = None
    out = Preview(choice, alias, account is not None)
    try:
        terms = mandates.terms_from_money(
            account_alias=alias,
            money=choice.amount,
            care="NORMAL",
            mode="AUTO",
            instruments=(key,),
            risk_pct=choice.risk_pct,
            open_positions=choice.open_positions,
            daily_loss=choice.daily_loss,
        )
    except (ConflictError, ValueError) as exc:  # the bounds, in the owner's words
        return _with(out, why=str(exc))
    hfm = await _hfm_account(conn)
    leverage = _d(hfm.get("leverage"))
    if account is None and choice.mode == HFM_DEMO:
        return _with(out, why="no HFM demo account is set up here")
    state = (
        await account_state(conn, account, now)
        if account is not None
        else new_simulation_state(now)
    )
    if choice.mode == HFM_DEMO:
        balance, free_margin = _d(hfm.get("balance")), _d(hfm.get("margin_free"))
    else:
        balance = free_margin = state.cash_available
    inst = await md.instrument_by_key(conn, key)
    q = await md.latest_quote(conn, inst.id, now)
    market = await auto.load_bars(conn, mandates.AUTO_SIGNAL_SERIES[key], TIMEFRAME)
    atr_value = ta.atr(market.high, market.low, market.close, 20)[-1] if market else None
    meta = inst.metadata or {}
    contract = _d(meta.get("contract_size"))
    if choice.mode == SIMULATION and contract is not None:
        contract = contract / 100  # a Cent lot is a hundredth of a standard lot
    held = Decimal(
        str(
            (
                await conn.scalar(
                    select(
                        func.coalesce(func.sum(positions.c.quantity * positions.c.avg_cost), 0)
                    ).where(positions.c.account_id == account.id, positions.c.quantity > 0)
                )
            )
            if account is not None
            else 0
        )
    )
    cap = choice.amount * mandates.exposure((key,))
    out = _with(
        out,
        balance=balance,
        free_margin=free_margin,
        leverage=leverage,
        max_loss=terms.max_loss_per_trade,
        daily_loss=terms.daily_loss_limit,
        total_loss=terms.total_limit,
        cap=cap,
        bid=q.bid,
        ask=q.ask,
        price_label=q.label.value,
        contract=contract,
        smallest=inst.min_qty or inst.lot_size,
        step=inst.lot_size,
        atr=_q(Decimal(str(atr_value))) if atr_value else None,
    )
    if q.label not in (DataLabel.LIVE, DataLabel.DELAYED) or q.ask is None or q.bid is None:
        return _with(out, why=f"there is no current gold price from HFM ({q.label.value})")
    sized = await auto.size_entry(
        conn,
        state=state,
        adapter=account.broker_adapter if account is not None else SIMULATED_ADAPTER,
        inst=inst,
        ask=q.ask,
        atr_value=atr_value,
        max_loss=terms.max_loss_per_trade,
        money_free=cap - held,
        times=mandates.exposure((key,)),
    )
    _, policy = await active_policy(conn)
    fee_bps = policy.fee_bps_for(inst.venue_key)
    smallest = inst.min_qty or inst.lot_size
    out = _with(
        out,
        entry=sized.entry,
        stop=sized.stop,
        stop_distance=sized.entry - sized.stop,
        stop_pct=_q((sized.entry - sized.stop) / sized.entry * 100) if sized.entry else ZERO,
        smallest_loss=_q(smallest * sized.per_unit) if smallest and sized.per_unit else None,
    )
    qty = sized.qty
    if qty > ZERO:
        value = qty * sized.entry
        swap = venue_terms.swap_per_night(meta, float(sized.entry))
        out = _with(
            out,
            ok=True,
            ounces=qty,
            lots=(qty / contract).quantize(Decimal("0.0001"), ROUND_DOWN) if contract else None,
            value=_q(value),
            margin=_q(value / leverage) if leverage else None,
            spread_cost=_q(qty * (q.ask - q.bid)),
            fees=_q(2 * value * fee_bps / BPS),
            swap_night=_q(value * Decimal(str(swap.long))) if swap is not None else None,
            loss_at_stop=_q(qty * sized.per_unit),
        )
        if choice.mode == HFM_DEMO and out.margin and free_margin and out.margin > free_margin:
            return _with(
                out,
                ok=False,
                why=f"HFM would want {out.margin:,.2f} of margin; it has {free_margin:,.2f} free",
            )
        return out
    fixes = _fixes(out, choice, sized)
    why = sized.refusal or "no trade fits"
    if fixes and fixes[0].startswith("At "):  # the amount is what is too small
        why = (
            "WAIT — Your selected trading amount is too small for this opportunity at your chosen "
            f"risk level: {why}"
        )
    return _with(out, why=why, fix=fixes)


def _fixes(out: Preview, choice: Choice, sized: auto.EntrySize) -> list[str]:
    """From which amount this opportunity would trade at the owner's own risk; the risk is never
    suggested to change (the owner, 2026-11-04)."""
    smallest, loss = out.smallest, out.smallest_loss
    if smallest is None or loss is None or loss <= 0:
        return []
    amounts: list[Decimal] = []
    if loss > out.max_loss:
        amounts.append(_up(loss * 100 / choice.risk_pct))
    if smallest * sized.entry > out.cap:
        amounts.append(_up(smallest * sized.entry / mandates.CFD_EXPOSURE))
    tips = (
        [f"At {choice.risk_pct.normalize():f} %, it trades from an amount of {max(amounts):,.2f}"]
        if amounts
        else []
    )
    if choice.mode == HFM_DEMO:
        tips.append(
            "The internal simulation trades from 0.01 ounce on HFM's prices (about "
            f"{_q(out.stop_distance / 100):,.2f} at the stop, before costs)"
        )
    return tips


def _with(p: Preview, **changes: Any) -> Preview:
    return replace(p, **changes)


async def current(conn: AsyncConnection, now: datetime) -> tuple[Choice, mandates.Mandate | None]:
    """The gold auto-trader running now (simulation or HFM demo) and its settings; the defaults
    when none runs."""
    for mode in (HFM_DEMO, SIMULATION):
        try:
            account = await get_account(conn, alias=ACCOUNTS[mode])
        except NotFoundError:
            continue
        m = next(
            (x for x in await mandates.active_mandates(conn, now) if x.account_id == account.id),
            None,
        )
        if m is not None and m.capital is not None and GOLD[mode] in m.instruments:
            return (
                Choice(
                    mode=mode,
                    amount=m.capital,
                    risk_pct=m.risk_pct or Decimal(1),
                    # None: three trades' worth, recalculated when the amount changes
                    daily_loss=None
                    if m.daily_loss_limit == m.max_loss_per_trade * mandates.DAILY_STOP_TRADES
                    else m.daily_loss_limit,
                    open_positions=m.max_open_positions,
                    automatic=m.mode == "AUTO",
                ),
                m,
            )
    return Choice(), None


async def start(
    conn: AsyncConnection,
    *,
    principal: Any,
    choice: Choice,
    now: datetime,
    settings: Any,
    broker: Any,
) -> uuid.UUID:
    """Start (or change) the gold auto-trader with the owner's choice, with their code: opens the
    simulated account the first time, and stops the other mode's gold auto-trader, so one runs."""
    alias, key = ACCOUNTS[choice.mode], GOLD[choice.mode]
    if choice.mode == SIMULATION:
        try:
            await get_account(conn, alias=alias)
        except NotFoundError:
            await open_paper_account(
                conn,
                broker=broker,
                alias=alias,
                initial_capital=SIM_CAPITAL,
                now=now,
                actor=principal.actor,
                venue_key=SIM_VENUE,
                base_currency="USD",
            )
    terms = mandates.terms_from_money(
        account_alias=alias,
        money=choice.amount,
        care="NORMAL",
        mode="AUTO" if choice.automatic else "ASK",
        instruments=(key,),
        strategy_key=RULE,
        timeframe=TIMEFRAME,
        risk_pct=choice.risk_pct,
        open_positions=choice.open_positions,
        daily_loss=choice.daily_loss,
    )
    other = SIMULATION if choice.mode == HFM_DEMO else HFM_DEMO
    try:
        other_account: Account | None = await get_account(conn, alias=ACCOUNTS[other])
    except NotFoundError:
        other_account = None
    if other_account is not None:
        for m in await mandates.active_mandates(conn, now):
            if m.account_id == other_account.id:
                await mandates.end(
                    conn,
                    mandate_id=m.id,
                    status="REVOKED",
                    by=principal.actor,
                    reason=f"the owner moved the gold auto-trader to {MODES[choice.mode]}",
                    now=now,
                )
    return await mandates.grant(
        conn, principal=principal, terms=terms, now=now, settings=settings, replace=True
    )
