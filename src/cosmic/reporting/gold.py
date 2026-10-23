"""The Gold page (ADR-032): everything about trading gold in one place, in plain numbers.

- the price now, its change over a day, a month and a year, and its fall from the high;
- the trend and the volatility (``market/regime.py``) and the trading session now;
- whether a gold trade fits the owner's money: one ounce's value and risk at its stop;
- the radar: the last two days and where the auto-trader would buy;
- why the auto-trader's rule lost or won on gold over the last year (``insight.diagnose``);
- gold's results in the rule competition (ADR-031);
- the benchmark: buying a fixed amount every month and holding (``insight.accumulate``);
- the owner's own closed gold trades.

Read-only. Simulated results are labelled; past results are no promise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.errors import NotFoundError
from cosmic.core.labels import DataLabel
from cosmic.core.money import BPS
from cosmic.market import data as md
from cosmic.market.regime import Regime, classify
from cosmic.reporting import autotrader
from cosmic.research.library import auto, compete_store, expect, insight, ta
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import Market
from cosmic.trading import mandates, venue_terms
from cosmic.trading.modes import friendly
from cosmic.trading.policy import active_policy

# (the instrument the auto-trader trades, the series its signal comes from), first found wins
GOLD_MARKETS: tuple[tuple[str, str], ...] = (
    ("CFD:XAU/USD@HFM", "SPOT:PAXG/USDT@BINANCE"),
    ("SPOT:PAXG/USDT@BINANCE", "SPOT:PAXG/USDT@BINANCE"),
)
R = 100.0  # the reference loss per trade for the research, in R
MONTHLY = 500.0  # the benchmark's monthly amount
SPREAD_WINDOW = timedelta(hours=24)
ASSUMED_SPREAD = Decimal("0.001")


@dataclass(frozen=True)
class Benchmark:
    years: int
    acc: insight.Accumulation


@dataclass
class GoldBoard:
    traded: str | None = None
    signal: str | None = None
    price: Decimal | None = None
    price_label: str = ""
    daily: Market | None = None  # the signal's daily bars, oldest first
    change_1d: float | None = None
    change_30d: float | None = None
    change_1y: float | None = None
    from_high_pct: float | None = None
    regime: Regime | None = None
    atr_pct: float | None = None  # 14-day ATR as a share of the price, now
    atr_usual_pct: float | None = None  # its median over the last year
    session: str = ""
    one_lot_value: Decimal | None = None
    one_lot_risk: Decimal | None = None  # on hourly bars, 3×ATR stop and fees
    minimum: dict[str, Decimal] = field(default_factory=dict)  # care level → least money
    chart: autotrader.Radar | None = None
    diagnosis: insight.Diagnosis | None = None
    research_window: tuple[datetime, datetime] | None = None
    competition: list[dict[str, Any]] = field(default_factory=list)
    competition_at: datetime | None = None
    benchmarks: list[Benchmark] = field(default_factory=list)
    my_trades: list[autotrader.ClosedTrade] = field(default_factory=list)
    costs: dict[str, Any] = field(default_factory=dict)


def _pct(a: float | None, b: float | None) -> float | None:
    return (a / b - 1) * 100 if a is not None and b else None


async def _daily(conn: AsyncConnection, key: str) -> Market | None:
    inst = await md.instrument_by_key(conn, key)
    bars = await md.complete_bars(conn, inst.id, "1d", limit=3_000)
    if len(bars) < 30:
        return None
    return Market(
        key,
        tuple(b.open_time for b in bars),
        tuple(float(b.open) for b in bars),
        tuple(float(b.high) for b in bars),
        tuple(float(b.low) for b in bars),
        tuple(float(b.close) for b in bars),
        tuple(float(b.volume) for b in bars),
    )


async def _market(conn: AsyncConnection) -> tuple[md.InstrumentView, str] | None:
    for traded, signal in GOLD_MARKETS:
        try:
            return await md.instrument_by_key(conn, traded), signal
        except NotFoundError:
            continue
    return None


async def build(conn: AsyncConnection, now: datetime) -> GoldBoard:
    g = GoldBoard(session=insight.session_of(now))
    found = await _market(conn)
    if found is None:
        return g
    inst, signal = found
    g.traded, g.signal = inst.canonical_key, signal
    q = await md.latest_quote(conn, inst.id, now)
    if q.bid is not None and q.ask is not None:
        g.price = (q.bid + q.ask) / 2
    g.price_label = q.label.value if isinstance(q.label, DataLabel) else str(q.label)
    g.daily = await _daily(conn, signal)
    if g.daily is not None:
        closes = g.daily.close
        g.change_1d = insight.change_pct(closes, 1)
        g.change_30d = insight.change_pct(closes, 30)
        g.change_1y = insight.change_pct(closes, 365)
        g.from_high_pct = _pct(closes[-1], max(closes))
        g.regime = classify(closes, vol_window=20, lookback=365)
        atr = ta.atr(g.daily.high, g.daily.low, closes, 14)
        series = [a / c * 100 for a, c in zip(atr[-365:], closes[-365:], strict=True) if a]
        if series:
            g.atr_pct = series[-1]
            g.atr_usual_pct = sorted(series)[len(series) // 2]
        first, last = g.daily.dates[0], g.daily.dates[-1]
        for years in (1, 3, 5):
            if not isinstance(first, datetime) or not isinstance(last, datetime):
                break
            since = last - timedelta(days=365 * years)
            if first <= since:
                acc = insight.accumulate(g.daily, MONTHLY, since)
                if acc is not None:
                    g.benchmarks.append(Benchmark(years, acc))
    if g.traded in mandates.AUTO_SIGNAL_SERIES:
        value, risk, hourly, _ = await autotrader.instrument_needs(conn, g.traded, now)
        g.one_lot_value, g.one_lot_risk = value, risk
        if value is not None and risk is not None:
            for name, (p, _) in mandates.CARE_LEVELS.items():
                g.minimum[name] = max(value, (risk * 100 / p).quantize(autotrader.CENT))
        spec = BY_KEY["turtle_20_10"]
        g.chart = autotrader.radar(spec, hourly, signal, None, g.price)
    await _research(conn, g, inst, now)
    run = await compete_store.latest_run(conn)
    if run is not None:
        g.competition = [e for e in run.entries if e["market"] == g.traded and e["tier"] != "N/A"]
        g.competition_at = run.created_at
    board = await autotrader.build(conn, now)
    g.my_trades = [t for t in board.closed if t.market == friendly(g.traded)]
    return g


async def _research(
    conn: AsyncConnection, g: GoldBoard, inst: md.InstrumentView, now: datetime
) -> None:
    """The auto-trader's rule on gold's last year of hourly bars, with and without costs."""
    assert g.signal is not None
    hourly = await auto.load_hourly(conn, g.signal, autotrader.EXPECT_BARS)
    if hourly is None or not isinstance(hourly.dates[0], datetime):
        return
    _, policy = await active_policy(conn)
    fee = float(policy.fee_bps_for(inst.venue_key) / BPS)
    measured = await md.median_spread(conn, inst.id, now, SPREAD_WINDOW)
    half = float(measured if measured is not None else ASSUMED_SPREAD) / 2
    swap = autotrader.swap_for(inst, hourly.close[-1])
    g.costs = {
        "fee_pct": fee * 100,
        "spread_pct": half * 200,
        "spread_measured": measured is not None,
        "slippage_pct": expect.SLIPPAGE * 100,
        "swap": swap,
    }
    start = max(now - timedelta(days=autotrader.EXPECT_DAYS), hourly.dates[0] + timedelta(days=2))
    if (now - start).days < autotrader.MIN_EXPECT_DAYS:
        return
    spec = BY_KEY["turtle_20_10"]
    limits = expect.Limits(per_trade=R, daily_stop=3 * R, trades_per_day=5, open_positions=1)
    signals = {g.traded or "": expect.signals_for(spec, hourly)}
    lot = 1e-9

    triple = autotrader.swap_triple(inst, swap)

    def replay(fee_: float, half_: float, slip: float, swap_: float) -> expect.Result:
        venue = expect.Venue(
            g.traded or "",
            hourly,
            lot,
            lot,
            fee_,
            half_,
            True,
            swap_,
            triple,
            weekend_closed=inst.base in venue_terms.METALS,
        )
        return expect.simulate([venue], limits, spec, start=start, slippage=slip, signals=signals)

    net = replay(fee, half, expect.SLIPPAGE, swap.long if swap else 0.0)
    gross = replay(0.0, 0.0, 0.0, 0.0)
    first = next((i for i, d in enumerate(hourly.dates) if d >= start), None)
    hold = _pct(hourly.close[-1], hourly.close[first]) if first is not None else None
    g.diagnosis = insight.diagnose(net, gross, per_trade=R, daily=g.daily, hold_pct=hold)
    g.research_window = (start, hourly.dates[-1])  # type: ignore[assignment]


def telegram_lines(g: GoldBoard) -> list[str]:
    """The /gold answer: a few lines a phone shows without scrolling."""
    if g.traded is None:
        return ["Gold is not configured here."]
    price = f"{g.price:,.2f}" if g.price is not None else "no fresh price"
    lines = [f"GOLD {price} ({g.price_label}) · {g.session} session now"]
    moves = [
        f"{label} {v:+.1f} %"
        for label, v in (("day", g.change_1d), ("30 days", g.change_30d), ("year", g.change_1y))
        if v is not None
    ]
    if moves:
        lines.append(" · ".join(moves))
    if g.regime is not None:
        lines.append(
            f"Trend: {g.regime.trend.replace('_', ' ').lower()} · volatility "
            f"{g.regime.volatility.replace('_VOL', '').lower()}"
        )
    if g.one_lot_risk is not None and g.one_lot_value is not None:
        lines.append(
            f"One ounce ties up {g.one_lot_value:,.2f} and risks about {g.one_lot_risk:,.2f} at "
            "the auto-trader's stop."
        )
    if g.diagnosis is not None and g.diagnosis.findings:
        lines.append("Why the rule lost or won: " + g.diagnosis.findings[0])
    lines.append("SIMULATED research · past results are no promise. More: portal → Gold.")
    return lines
