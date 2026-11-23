"""What is verified about HFM's gold contract, and what is not (S115).

The calculator sizes trades on the contract MT5 reports. A reported figure is HFM's own terms for
that account, but it is a claim until HFM's own records bear it out. Each figure is therefore
labelled from the evidence, separately for each account (the demo on terminal 1 and the owner's
real account on terminal 3): a demo's conditions are never taken for the real account's.

- VERIFIED: HFM's own bookings or its own calculator confirm it (closed trades booked to the
  cent, the margin from MT5's order_calc_margin).
- MEASURED: measured from HFM's own data over a stated period (the spread, the commission).
- REPORTED: MT5 reports it for this account and nothing contradicts it, but nothing confirms it.
- CONTRADICTED: HFM's own bookings disagree with its stated terms.
- NOT REPORTED: not read from HFM at all yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.manual.calc import Commission, swap_per_lot, triple_night
from cosmic.manual.live import (
    BUY,
    ENTRY_IN,
    ENTRY_INOUT,
    ENTRY_OUT,
    ENTRY_OUT_BY,
    SELL,
    Deal,
    Quote,
    Spec,
)
from cosmic.market import hfm_candles

VERIFIED, MEASURED, REPORTED = "VERIFIED", "MEASURED", "REPORTED"
CONTRADICTED, NOT_REPORTED = "CONTRADICTED", "NOT REPORTED"
CENT = Decimal("0.01")
SPREAD_DAYS = 30
# MT5's ENUM_SYMBOL_CALC_MODE, the modes a gold CFD is offered in.
CALC_MODES = {
    0: "FOREX (contract × price ÷ leverage)",
    1: "FOREX without leverage",
    2: "FUTURES",
    3: "CFD (contract × price × margin rate)",
    4: "CFD with leverage (contract × price ÷ leverage)",
    5: "CFD on an index",
}


@dataclass(frozen=True)
class Fact:
    item: str
    value: str
    status: str
    evidence: str


@dataclass(frozen=True)
class Spreads:
    """HFM's spread over the stored quotes of the demo terminal (the only quotes kept)."""

    quotes: int
    median: Decimal
    p90: Decimal
    p99: Decimal
    widest: Decimal
    days: int


async def spreads(conn: AsyncConnection, *, days: int = SPREAD_DAYS) -> Spreads | None:
    row = (
        await conn.execute(
            text(
                "SELECT count(*) AS n, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY q.ask - q.bid) AS p50, "
                "percentile_cont(0.9) WITHIN GROUP (ORDER BY q.ask - q.bid) AS p90, "
                "percentile_cont(0.99) WITHIN GROUP (ORDER BY q.ask - q.bid) AS p99, "
                "max(q.ask - q.bid) AS widest "
                "FROM quotes q JOIN instruments i ON i.id = q.instrument_id "
                "JOIN sources s ON s.id = q.source_id "
                "WHERE i.canonical_key = :k AND s.key = :s AND q.ask IS NOT NULL "
                "AND q.bid IS NOT NULL AND q.source_ts > now() - make_interval(days => :d)"
            ),
            {"k": hfm_candles.INSTRUMENT, "s": hfm_candles.SOURCE, "d": days},
        )
    ).one()
    if not row.n:
        return None

    def cents(v: object) -> Decimal:
        return Decimal(str(v)).quantize(CENT)

    return Spreads(
        int(row.n), cents(row.p50), cents(row.p90), cents(row.p99), cents(row.widest), days
    )


@dataclass(frozen=True)
class Closed:
    """One closed position from HFM's deals, as booked."""

    side: str
    lots: Decimal
    entry: Decimal
    exit: Decimal
    profit: Decimal  # HFM's booked profit of the closing deals
    swap: Decimal  # HFM's booked swap, every deal of the position
    opened_server: datetime
    closed_server: datetime


def closed_positions(deals: Sequence[Deal]) -> list[Closed]:
    by: dict[int, list[Deal]] = {}
    for d in deals:
        if d.side is not None and d.position_id:
            by.setdefault(d.position_id, []).append(d)
    out = []
    for ds in by.values():
        ins = [d for d in ds if d.entry in (ENTRY_IN, ENTRY_INOUT)]
        outs = [d for d in ds if d.entry in (ENTRY_OUT, ENTRY_OUT_BY)]
        if len(ins) != 1 or not outs or ins[0].side is None:
            continue
        lots = sum((d.lots for d in outs), Decimal(0))
        if lots != ins[0].lots:
            continue  # partly closed: judged once it is whole
        exit_price = sum((d.price * d.lots for d in outs), Decimal(0)) / lots
        out.append(
            Closed(
                ins[0].side,
                lots,
                ins[0].price,
                exit_price,
                sum((d.profit for d in outs), Decimal(0)),
                sum((d.swap for d in ds), Decimal(0)),
                ins[0].server_at,
                max(d.server_at for d in outs),
            )
        )
    return out


def nights(opened: datetime, closed: datetime, triple: int | None) -> int:
    """HFM's rollovers between two times on its own clock: one each server midnight from Monday
    to Friday, three on the triple night (MT5: 0 Sunday … 6 Saturday)."""
    n, day = 0, opened.date()
    while day < closed.date():
        mt5_day = (day.weekday() + 1) % 7  # Python's Monday 0 → MT5's 1
        if mt5_day not in (0, 6):
            n += 3 if triple is not None and mt5_day == triple else 1
        day += timedelta(days=1)
    return n


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _money(v: Decimal) -> str:
    return f"{v.quantize(CENT):,}"


def facts(
    spec: Spec | None,
    *,
    deals: Sequence[Deal],
    commission: Commission,
    currency: str | None,
    leverage: int | None,
    quote: Quote | None,
    spread_history: Spreads | None,
    stop_out: Decimal | None = None,
    stop_out_mode: str | None = None,
    margin_call: Decimal | None = None,
) -> list[Fact]:
    """Each figure the calculator uses, its value on this account and what proves it."""
    if spec is None:
        return [Fact("HFM's gold contract", "—", NOT_REPORTED, "the terminal has not reported it")]
    closed = closed_positions(deals)
    vpp = spec.value_per_price
    out: list[Fact] = []

    # Contract size and the value of a price move, against HFM's booked profit.
    worst = Decimal(0)
    for c in closed:
        sign = 1 if c.side == BUY else -1
        worst = max(worst, abs((c.exit - c.entry) * c.lots * vpp * sign - c.profit))
    size = f"{spec.contract_size.normalize():f} oz a lot"
    if closed and worst < CENT:
        out.append(
            Fact(
                "Contract size",
                size,
                VERIFIED,
                f"HFM's booked profit on {len(closed)} closed trade{_s(len(closed))} equals "
                "(exit − entry) × "
                f"lots × {vpp.normalize():f} to the cent",
            )
        )
    elif closed:
        out.append(
            Fact(
                "Contract size",
                size,
                CONTRADICTED,
                f"HFM's booked profit differs by up to {_money(worst)} from (exit − entry) × lots "
                f"× {vpp.normalize():f}",
            )
        )
    else:
        out.append(Fact("Contract size", size, REPORTED, "no closed trade yet to confirm it"))
    move = f"{_money(vpp)} {currency or ''} a lot for 1.00 of price".replace("  ", " ")
    consistent = spec.contract_size * spec.tick_size == spec.tick_value
    if spec.profit_1lot_1 is not None:
        same = abs(spec.profit_1lot_1 - vpp) < CENT
        out.append(
            Fact(
                "Value of a price move",
                move,
                VERIFIED if same else CONTRADICTED,
                f"HFM's own profit calculator gives {_money(spec.profit_1lot_1)} for 1 lot and "
                "a 1.00 rise",
            )
        )
    else:
        out.append(
            Fact(
                "Value of a price move",
                move,
                VERIFIED if closed and worst < CENT else REPORTED,
                f"tick {spec.tick_size.normalize():f} worth {spec.tick_value.normalize():f} a lot"
                + ("; consistent with the contract size" if consistent else "; NOT consistent")
                + ("; confirmed by the booked profits above" if closed and worst < CENT else ""),
            )
        )
    grid = all(
        d.lots >= spec.volume_min and (d.lots - spec.volume_min) % spec.volume_step == 0
        for d in deals
        if d.side is not None
    )
    trades = sum(1 for d in deals if d.side is not None and d.entry == ENTRY_IN)
    out.append(
        Fact(
            "Lot sizes",
            f"{spec.volume_min.normalize():f} to {spec.volume_max.normalize():f}, steps of "
            f"{spec.volume_step.normalize():f}",
            REPORTED,
            f"as MT5 reports them; HFM filled {trades} orders on this grid"
            if trades and grid
            else "as MT5 reports them; no order filled yet",
        )
    )
    out.append(
        Fact("Account currency", currency or "—", REPORTED if currency else NOT_REPORTED, "MT5")
    )

    # Margin: HFM's own calculator, or only the formula.
    mode = CALC_MODES.get(spec.calc_mode, f"mode {spec.calc_mode}") if spec.calc_mode else None
    lev = f"1:{leverage}" if leverage else "unknown"
    own = spec.margin_per_lot(BUY, spec.margin_buy_at) if spec.margin_buy_at else None
    if own is not None and spec.margin_buy_at is not None:
        formula = spec.contract_size * spec.margin_buy_at / Decimal(leverage) if leverage else None
        agree = formula is not None and abs(formula - own) <= own / 100
        out.append(
            Fact(
                "Margin and leverage",
                f"leverage {lev}; {_money(own)} a lot at {spec.margin_buy_at}",
                VERIFIED,
                "HFM's own margin calculator"
                + (f", margin mode {mode}" if mode else "")
                + (
                    "; the leverage formula agrees"
                    if agree
                    else f"; the leverage formula would give {_money(formula)}: HFM's is used"
                    if formula is not None
                    else ""
                ),
            )
        )
    else:
        out.append(
            Fact(
                "Margin and leverage",
                f"leverage {lev}",
                REPORTED if leverage else NOT_REPORTED,
                "the margin is only estimated (contract × price ÷ leverage): HFM's own margin "
                "figure and margin mode are read by bridge 13, not yet installed here",
            )
        )
    if spread_history is not None:
        now = f"; {quote.spread} now" if quote is not None else ""
        out.append(
            Fact(
                "Spread",
                f"median {spread_history.median}{now}",
                MEASURED,
                f"{spread_history.quotes:,} of HFM's quotes over {spread_history.days} days on the "
                f"demo: 90 % at or below {spread_history.p90}, 99 % at or below "
                f"{spread_history.p99}, widest {spread_history.widest}. Quotes are kept every few "
                "seconds, so brief spikes between them are not seen",
            )
        )
    elif quote is not None:
        out.append(Fact("Spread", f"{quote.spread} now", REPORTED, "only the price now is kept"))
    else:
        out.append(Fact("Spread", "—", NOT_REPORTED, "no price reported"))
    if commission.per_lot is None:
        out.append(
            Fact("Commission", "—", NOT_REPORTED, "measured from this account's own deals: none")
        )
    else:
        out.append(
            Fact(
                "Commission",
                f"{_money(commission.per_lot)} a lot, both sides",
                MEASURED,
                f"from {commission.deals} of HFM's own deals on this account only; another "
                "account type can be charged differently",
            )
        )

    # Swap, against HFM's bookings of positions held over its rollover.
    held = [(c, nights(c.opened_server, c.closed_server, spec.swap_rollover3days)) for c in closed]
    held = [(c, n) for c, n in held if n > 0]
    for side, label in ((BUY, "Swap, BUY"), (SELL, "Swap, SELL")):
        rate = swap_per_lot(spec, side, quote.mid if quote is not None else Decimal(0), currency)
        stated = "not convertible" if rate is None else f"{_money(rate)} a lot a night"
        mine = [(c, n) for c, n in held if c.side == side]
        if rate is None:
            out.append(Fact(label, stated, NOT_REPORTED, "swap terms missing or not in money"))
            continue
        if not mine:
            out.append(
                Fact(label, stated, REPORTED, "HFM's terms; no position held overnight to confirm")
            )
            continue
        booked = sum((c.swap for c, _ in mine), Decimal(0))
        expected = sum((rate * c.lots * n for c, n in mine), Decimal(0))
        n_nights = sum(n for _, n in mine)
        if abs(booked - expected) < CENT:
            out.append(
                Fact(
                    label,
                    stated,
                    VERIFIED,
                    f"HFM booked {_money(booked)} on {len(mine)} positions over {n_nights} nights, "
                    "as its terms give",
                )
            )
        else:
            out.append(
                Fact(
                    label,
                    stated,
                    CONTRADICTED,
                    f"HFM booked {_money(booked)} on {len(mine)} overnight positions "
                    f"({n_nights} nights) where its terms give {_money(expected)}. The calculator "
                    "still counts the stated cost, the cautious figure",
                )
            )
    triple = triple_night(spec)
    out.append(
        Fact(
            "Triple-swap night",
            triple or "—",
            REPORTED if triple else NOT_REPORTED,
            "HFM's terms (three nights charged for the weekend)",
        )
    )
    stops = spec.stops_level * spec.point
    out.append(
        Fact(
            "Nearest stop-loss allowed",
            "no minimum" if not spec.stops_level else f"{stops} from the price",
            REPORTED,
            "MT5's stops level for this account",
        )
    )
    if spec.freeze_level is None:
        out.append(Fact("Freeze level", "—", NOT_REPORTED, "read by bridge 13"))
    else:
        out.append(
            Fact(
                "Freeze level",
                "none" if not spec.freeze_level else f"{spec.freeze_level * spec.point}",
                REPORTED,
                "MT5: how near the price an order or a stop may no longer be changed",
            )
        )
    if stop_out is None:
        out.append(Fact("Stop-out level", "—", NOT_REPORTED, "read by bridge 13"))
    else:
        unit = " %" if stop_out_mode == "PERCENT" else f" {currency or ''}"
        call = f"; margin call at {margin_call.normalize():f}{unit}" if margin_call else ""
        out.append(
            Fact(
                "Stop-out level",
                f"{stop_out.normalize():f}{unit}",
                REPORTED,
                f"HFM closes positions itself at this margin level{call}",
            )
        )
    return out
