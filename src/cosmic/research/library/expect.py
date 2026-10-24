"""What to expect: the demo auto-trader's rule replayed on past hourly bars (ADR-030).

Before the owner puts money on the auto-trader, this replays what it would have done over the
stored history of its signal series, with the owner's limits, and sums it up in plain numbers:
how often it traded and won, the typical win and loss, the worst losing streak and the deepest
drop. It follows the live auto-trader (``auto.py``, ``cosmic.trading.mandates``):

- The rule's own state (``spec.fn``) decides. A flat → long change at a bar's close is an
  entry, a long → flat change an exit. After a stop-out it waits for the next flat → long.
- A decision at the close of bar t fills at the open of bar t+1, the price at that moment.
  Buys pay the ask, sells get the bid.
- The protective stop is 3×ATR(20) of the decision bar below the entry limit (ask + 0.1 %).
  Every later bar is checked. When the bid touches the stop, it fills there; when a bar
  opens below it (a gap), it fills at that open, which loses more.
- The size is whole lots: the loss at the stop, with fees on both sides, stays within the
  per-trade limit. A market whose smallest trade risks more is skipped, never resized.
- The mandate's limits apply: entries a day, open positions, the daily loss stop and, with
  money set, open positions never worth more than the money. With a total loss limit, an entry
  is skipped when a full loss on it and on the open trades could pass the limit, and the
  replay ends the moment the losses leave no room for another trade with nothing open: the
  auto-trader would have switched itself off (STOPPED) there.

Overnight swap on CFDs is charged when the venue reports it (``Venue.swap``): for each
rollover a trade is held over, at about 21:00 UTC; for a market closed at weekends, nothing on
Saturday and Sunday and three nights on the venue's triple day. Not modelled, and said so where
results are shown: the minutes between a close and the fill, requotes, and the gap between
the signal series and the venue's own price. Floats: analytics only (ADR-007). A historical
simulation is not a promise of future results.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from cosmic.research.library import ta
from cosmic.research.library.catalog import Spec
from cosmic.research.library.engine import Market

STOP_ATR = 3.0  # as auto.STOP_ATR
ATR_N = 20
LIMIT_MARKUP = 0.001  # the entry is a marketable limit at the ask + 0.1 % (auto._entry)
STOP_LIMIT_BELOW = 0.005  # without a market stop, the stop-limit is 0.5 % below the stop
SLIPPAGE = 0.0005  # per fill: ASSUMPTION
EPS = 1e-9
# The broker's day and week follow New York: the daily rollover (00:00 server time) is 17:00
# in New York, and CFD metals close from Friday 17:00 to Sunday 18:00 there. In UTC that is
# 21:00 / Sunday 22:00 in summer and an hour later in winter.
NEW_YORK = ZoneInfo("America/New_York")
ROLLOVER_NY_HOUR = 17
REOPEN_NY_HOUR = 18

SKIP_REASONS = {
    "TOO_BIG": "the smallest trade would have risked more than the per-trade limit",
    "NO_MONEY": "the money was already in open trades",
    "TRADES_TODAY": "the day's trades were used",
    "OPEN_LIMIT": "the open-trade limit was reached",
    "DAILY_STOP": "the daily loss stop had been reached",
    "TOTAL_STOP": "a full loss on it and the open trades could have passed the total loss limit",
    "RISK_ROOM": "the risk policy left no room for even the smallest trade (the share of equity "
    "in one market, or the loss if every stop is hit)",
    "RISK_LOSS_LIMITS": "the risk policy's loss limits had stopped new trades (losses in a row, "
    "a drawdown or the day's loss)",
}


@dataclass(frozen=True)
class Venue:
    """One market as the auto-trader trades it."""

    key: str  # the traded instrument, e.g. CFD:BTC/USD@HFM
    bars: Market  # completed hourly bars of its signal series, oldest first
    lot: float  # the quantity step
    min_qty: float  # the smallest trade
    fee: float  # per fill, a fraction of the value (the risk policy's assumption)
    half_spread: float  # a fraction of the price: buying pays it, selling gives it up
    market_stop: bool = True  # the venue fills the stop at market (no stop-limit price)
    swap: float = 0.0  # long swap per night, a fraction of the price (negative: paid)
    swap_triple: int | None = None  # closed at weekends: the weekday that charges three nights
    # The venue does not trade at weekends (gold CFDs), although the signal series does: no
    # fill, stop or decision then; a stop crossed over the weekend fills at the reopening.
    weekend_closed: bool = False


@dataclass(frozen=True)
class Limits:
    per_trade: float  # the most one trade may lose at its stop
    daily_stop: float  # no new entry once a UTC day's closed trades lost this much
    trades_per_day: int
    open_positions: int
    capital: float | None = None  # open positions, fully paid, never worth more
    max_order_notional: float = math.inf
    total_stop: float | None = None  # the most it may lose in total; then it stops for good
    # Size to the evidence (mandates.sizing): half size from a drop of this many trades' worth
    # below the best running result, full again below ``restore_below``.
    halve_after: float | None = None
    restore_below: float = 2.5
    # The account's risk policy as its checks apply it (S36), on the account's equity: the
    # start ``equity`` (else ``capital``) plus the result so far, open trades marked at the close.
    equity: float | None = None
    concentration: float | None = None  # R06: at most this share of equity in one market
    open_risk: float | None = None  # R19: the loss if every stop is hit, as a share of equity
    max_drawdown: float | None = None  # R08: no new trade this share below the equity's high
    loss_streak: int | None = None  # R08: no new trade after this many losing trades in a row
    max_daily_loss: float | None = None  # R08: no new trade after this loss in a UTC day


@dataclass(frozen=True)
class Trade:
    key: str
    opened: datetime
    closed: datetime
    quantity: float
    entry: float  # fill
    stop: float
    exit: float  # fill
    risk: float  # the loss at the stop as sized, fees included (at most the limit)
    pnl: float  # after costs, swap included
    how: str  # stop-loss | rule exit | still open
    swap: float = 0.0  # the overnight swap in ``pnl`` (negative: paid)


@dataclass(frozen=True)
class Skip:
    key: str
    at: datetime
    reason: str  # a key of SKIP_REASONS


@dataclass
class Result:
    start: datetime
    end: datetime
    hours: int  # hourly steps replayed
    trades: list[Trade] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    hours_in_market: int = 0
    stopped_at: datetime | None = None  # when the total loss stop switched it off


@dataclass
class _Open:
    quantity: float
    entry: float
    stop: float
    risk: float
    opened: datetime
    stop_limit: float = 0.0  # where the stop order may fill at worst (R19's floor)


def _ny(t: datetime) -> datetime:
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(NEW_YORK)


def weekend_closed_at(t: datetime) -> bool:
    """The bar opening at ``t`` falls in the weekend close of a CFD venue: Friday 17:00 to
    Sunday 18:00 in New York (a daily bar of Saturday or Sunday UTC falls in it entirely)."""
    ny = _ny(t)
    day, hour = ny.weekday(), ny.hour
    return (
        (day == 4 and hour >= ROLLOVER_NY_HOUR) or day == 5 or (day == 6 and hour < REOPEN_NY_HOUR)
    )


def nights(opened: datetime, closed: datetime, triple: int | None) -> int:
    """Swap nights between two times: each rollover (17:00 in New York) held over counts once.
    With ``triple`` (a market closed at weekends), Saturday and Sunday count nothing and that
    weekday three."""
    count = 0
    day, last = _ny(opened).date(), _ny(closed).date()
    while day <= last:
        rollover = datetime.combine(day, time(ROLLOVER_NY_HOUR), NEW_YORK)
        if opened < rollover <= closed:
            if triple is None:
                count += 1
            elif day.weekday() < 5:
                count += 3 if day.weekday() == triple else 1
        day += timedelta(days=1)
    return count


def _lots(amount: float, lot: float) -> float:
    """Whole lots in ``amount``, rounded down."""
    return math.floor(amount / lot + EPS) * lot if amount > 0 else 0.0


def size(
    *,
    per_trade: float,
    entry: float,
    stop_limit: float,
    fee: float,
    lot: float,
    min_qty: float,
    room: float,
    max_qty: float = math.inf,
) -> tuple[float, float]:
    """(quantity, loss per unit) as ``auto.size_for_max_loss`` sizes a live entry: whole lots
    whose loss at the stop-limit, with fees on both sides, stays within ``per_trade``, whose
    value stays within ``room`` and quantity within ``max_qty``. Zero when even the smallest
    trade does not fit."""
    per_unit = entry - stop_limit + 2 * entry * fee
    if per_unit <= 0:
        return 0.0, per_unit
    qty = _lots(per_trade / per_unit, lot)
    if math.isfinite(room):
        qty = min(qty, _lots(room / entry, lot))
    if math.isfinite(max_qty):
        qty = min(qty, _lots(max_qty, lot))
    return (qty, per_unit) if qty + EPS >= min_qty else (0.0, per_unit)


# Per market: the rule's weight and the ATR at every bar. Both are causal, so they can be
# computed once on the whole series and replayed over any window (``signals``).
Signals = tuple[Sequence[float], Sequence[float | None]]


def signals_for(spec: Spec, bars: Market) -> Signals:
    if spec.fn is None:
        raise ValueError("only single-market rules can be replayed")
    return spec.fn(bars, spec.params), ta.atr(bars.high, bars.low, bars.close, ATR_N)


class _Replay:
    def __init__(
        self,
        venues: Sequence[Venue],
        limits: Limits,
        spec: Spec,
        start: datetime,
        slippage: float,
        end: datetime | None = None,
        signals: Mapping[str, Signals] | None = None,
    ) -> None:
        self.limits, self.slippage = limits, slippage
        self.venues = {v.key: v for v in venues}
        given = signals or {}
        computed = {v.key: given.get(v.key) or signals_for(spec, v.bars) for v in venues}
        self.weights = {k: w for k, (w, _) in computed.items()}
        self.atr = {k: a for k, (_, a) in computed.items()}
        self.index = {
            v.key: {
                t: i
                for i, t in enumerate(v.bars.dates)
                if not (v.weekend_closed and isinstance(t, datetime) and weekend_closed_at(t))
            }
            for v in venues
        }
        self.times: list[datetime] = sorted(
            {
                t
                for v in venues
                for t in v.bars.dates
                if isinstance(t, datetime) and t >= start and (end is None or t < end)
            }
        )
        self.last: dict[str, int] = {}  # each market's last bar replayed
        self.result = Result(start, self.times[-1] if self.times else start, len(self.times))
        self.held: dict[str, _Open] = {}
        self.pending: dict[str, tuple[str, float]] = {}  # key → (ENTER | EXIT, decision ATR)
        self.entries_on: dict[date, int] = {}
        self.result_on: dict[date, float] = {}
        self.lost = 0.0  # in total, after costs (negative while ahead)
        self.best = 0.0  # the best running result so far (for the half-size rule)
        self.half = False
        self.streak = 0  # losing trades in a row (R08)
        self.step: set[str] = set()  # the markets with a bar at the time being replayed
        self.high = self._equity()  # the equity's high-water mark at bar closes (R08)

    def run(self) -> Result:
        for t in self.times:
            here = [(k, idx[t]) for k, idx in self.index.items() if t in idx]
            self.last.update(here)
            self.step = {k for k, _ in here}
            # At the open: what the previous close decided fills now, exits first so the
            # money they free can pay for entries.
            for key, i in sorted(here, key=lambda h: self.pending.get(h[0], ("",))[0] != "EXIT"):
                action = self.pending.pop(key, None)
                if action is not None:
                    self._fill(key, i, t, *action)
            # During the bar: protective stops.
            for key, i in here:
                if key in self.held:
                    self._stop(key, i, t)
            # At the close: the rule decides, and the equity's high is marked (R08).
            for key, i in here:
                self._decide(key, i)
            self.high = max(self.high, self._equity())
            if self.held:
                self.result.hours_in_market += 1
            elif self._out_of_room():
                self.result.stopped_at = t  # it switches itself off: nothing more happens
                break
        for key in list(self.held):  # open at the end: marked at the last bid, after fees
            v = self.venues[key]
            price = v.bars.close[self.last[key]] * (1 - v.half_spread)
            self._close(key, self.result.end, price, "still open")
        return self.result

    def _mark(self, key: str, at_open: bool) -> float:
        """An open trade's price: at a fill, the open of a market's bar of this time (its close
        is not known yet), else the last close."""
        bars, i = self.venues[key].bars, self.last[key]
        return bars.open[i] if at_open and key in self.step else bars.close[i]

    def _start_equity(self) -> float | None:
        lim = self.limits
        return lim.equity if lim.equity is not None else lim.capital

    def _equity(self, at_open: bool = False) -> float:
        """The account's equity: the start, the result so far, open trades marked (at the
        fill moment with ``at_open``, at the close otherwise)."""
        marked = sum(
            p.quantity * (self._mark(k, at_open) - p.entry)
            for k, p in self.held.items()
            if k in self.last
        )
        return (self._start_equity() or 0.0) - self.lost + marked

    def _loss_limits_hit(self, day: date) -> bool:
        """R08: losses in a row, the drawdown from the equity's high, or the day's loss."""
        lim = self.limits
        if lim.loss_streak is not None and self.streak >= lim.loss_streak:
            return True
        if self._start_equity() is None:  # no equity to measure a drawdown or loss against
            return False
        equity = self._equity(at_open=True)
        drop = (self.high - equity) / self.high if self.high > 0 else 0.0
        if lim.max_drawdown is not None and drop >= lim.max_drawdown:
            return True
        if lim.max_daily_loss is not None:
            open_result = equity - ((self._start_equity() or 0.0) - self.lost)
            if -(self.result_on.get(day, 0.0) + open_result) >= lim.max_daily_loss:
                return True
        return False

    def _out_of_room(self) -> bool:
        """The total loss stop: losses leave no room for one more full losing trade."""
        lim = self.limits
        return lim.total_stop is not None and self.lost + lim.per_trade > lim.total_stop

    def _decide(self, key: str, i: int) -> None:
        w = self.weights[key]
        if i == 0 or key in self.pending:
            return
        if w[i - 1] == 0.0 and w[i] > 0.0 and key not in self.held:
            atr = self.atr[key][i]
            if atr is not None and atr > 0:
                self.pending[key] = ("ENTER", atr)
        elif w[i] == 0.0 and key in self.held:
            self.pending[key] = ("EXIT", 0.0)

    def _fill(self, key: str, i: int, t: datetime, action: str, atr: float) -> None:
        v = self.venues[key]
        price = v.bars.open[i]
        if action == "EXIT":
            if key in self.held:
                self._close(key, t, price * (1 - v.half_spread) * (1 - self.slippage), "rule exit")
            return
        ask = price * (1 + v.half_spread)
        limit = ask * (1 + LIMIT_MARKUP)
        stop = limit - STOP_ATR * atr
        stop_limit = stop if v.market_stop else stop * (1 - STOP_LIMIT_BELOW)
        if stop_limit <= 0:
            return
        lim = self.limits
        day = t.date()
        if self._loss_limits_hit(day):
            self._skip(key, t, "RISK_LOSS_LIMITS")
            return
        room = lim.max_order_notional
        if lim.capital is not None:
            free = lim.capital - sum(p.quantity * p.entry for p in self.held.values())
            if free <= 0:
                self._skip(key, t, "NO_MONEY")
                return
            room = min(room, free)
        policy_room, max_qty = math.inf, math.inf
        known = self._start_equity() is not None  # the equity-based limits need a start
        equity = self._equity(at_open=True)
        if known and lim.concentration is not None and equity > 0:  # R06 (flat in this market)
            policy_room = lim.concentration * equity
        if known and lim.open_risk is not None and equity > 0:  # R19
            at_risk = sum(
                p.quantity * max(self._mark(k, True) - p.stop_limit, 0.0)
                for k, p in self.held.items()
                if k in self.last
            )
            max_qty = max(lim.open_risk * equity - at_risk, 0.0) / (limit - stop_limit)
        qty, per_unit = size(
            per_trade=lim.per_trade * (0.5 if self.half else 1.0),
            entry=limit,
            stop_limit=stop_limit,
            fee=v.fee,
            lot=v.lot,
            min_qty=v.min_qty,
            room=min(room, policy_room),
            max_qty=max_qty,
        )
        if qty <= 0:
            if v.min_qty * limit > room:
                reason = "NO_MONEY"
            elif v.min_qty * limit > policy_room or v.min_qty > max_qty:
                reason = "RISK_ROOM"
            else:
                reason = "TOO_BIG"
            self._skip(key, t, reason)
            return
        if self.entries_on.get(day, 0) >= lim.trades_per_day:
            self._skip(key, t, "TRADES_TODAY")
        elif len(self.held) >= lim.open_positions:
            self._skip(key, t, "OPEN_LIMIT")
        elif self.result_on.get(day, 0.0) <= -lim.daily_stop:
            self._skip(key, t, "DAILY_STOP")
        elif (
            lim.total_stop is not None
            and self.lost + (len(self.held) + 1) * lim.per_trade > lim.total_stop
        ):
            self._skip(key, t, "TOTAL_STOP")
        else:
            fill = ask * (1 + self.slippage)
            self.held[key] = _Open(qty, fill, stop, qty * per_unit, t, stop_limit)
            self.entries_on[day] = self.entries_on.get(day, 0) + 1

    def _stop(self, key: str, i: int, t: datetime) -> None:
        v, pos = self.venues[key], self.held[key]
        bid_open = v.bars.open[i] * (1 - v.half_spread)
        if bid_open <= pos.stop:  # a gap through the stop fills at the open
            self._close(key, t, bid_open * (1 - self.slippage), "stop-loss")
        elif v.bars.low[i] * (1 - v.half_spread) <= pos.stop:
            self._close(key, t, pos.stop * (1 - self.slippage), "stop-loss")

    def _close(self, key: str, t: datetime, fill: float, how: str) -> None:
        v, pos = self.venues[key], self.held.pop(key)
        swap = (
            pos.quantity * pos.entry * v.swap * nights(pos.opened, t, v.swap_triple)
            if v.swap
            else 0.0
        )
        pnl = pos.quantity * (fill - pos.entry) - pos.quantity * (pos.entry + fill) * v.fee + swap
        self.result.trades.append(
            Trade(
                key,
                pos.opened,
                t,
                pos.quantity,
                pos.entry,
                pos.stop,
                fill,
                pos.risk,
                pnl,
                how,
                swap,
            )
        )
        self.result_on[t.date()] = self.result_on.get(t.date(), 0.0) + pnl
        self.lost -= pnl
        self.streak = self.streak + 1 if pnl < 0 else 0
        lim = self.limits
        if lim.halve_after is not None:
            self.best = max(self.best, -self.lost)
            drop = self.best + self.lost
            if not self.half and drop >= lim.halve_after * lim.per_trade:
                self.half = True
            elif self.half and drop < lim.restore_below * lim.per_trade:
                self.half = False

    def _skip(self, key: str, t: datetime, reason: str) -> None:
        self.result.skipped.append(Skip(key, t, reason))


def simulate(
    venues: Sequence[Venue],
    limits: Limits,
    spec: Spec,
    *,
    start: datetime,
    slippage: float = SLIPPAGE,
    end: datetime | None = None,
    signals: Mapping[str, Signals] | None = None,
) -> Result:
    """Replay the auto-trader on ``venues`` from ``start`` to before ``end``; earlier bars only
    warm the rule up (an entry needs a fresh flat → long change, as for a newly granted
    mandate), and a trade still open at the end is marked at the last close replayed.
    ``signals`` may carry each market's precomputed weights and ATR (``signals_for``)."""
    return _Replay(venues, limits, spec, start, slippage, end, signals).run()


# ---- the summary ------------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketResult:
    key: str
    trades: int
    wins: int
    total: float


@dataclass(frozen=True)
class Summary:
    start: datetime
    end: datetime
    days: int
    trades: int
    wins: int
    losses: int
    total: float
    avg_win: float | None
    avg_loss: float | None
    best: float | None
    worst: float | None
    stopped: int  # trades the stop ended
    per_week: float
    losing_streak: int  # the most losing trades in a row
    streak_loss: float  # what that streak lost
    max_drawdown: float  # the deepest fall of the running result from its high (≤ 0)
    first_half: float
    second_half: float
    in_market: float  # share of the hours with a trade open
    months: list[tuple[date, float]]
    markets: list[MarketResult]
    skipped: dict[str, int]
    curve: list[tuple[datetime, float]]  # the running result after each trade
    stopped_at: datetime | None = None  # the total loss stop switched it off here

    @property
    def win_rate(self) -> int | None:
        return round(100 * self.wins / self.trades) if self.trades else None

    @property
    def per_trade(self) -> float | None:
        """The average result of one trade (the expectancy)."""
        return self.total / self.trades if self.trades else None


def _streak(pnls: Sequence[float]) -> tuple[int, float]:
    """(the longest run of losses, its sum); between equal runs, the costlier one."""
    best, best_sum, run, run_sum = 0, 0.0, 0, 0.0
    for p in pnls:
        if p < 0:
            run, run_sum = run + 1, run_sum + p
            if run > best or (run == best and run_sum < best_sum):
                best, best_sum = run, run_sum
        else:
            run, run_sum = 0, 0.0
    return best, best_sum


def _months(start: datetime, end: datetime, trades: Sequence[Trade]) -> list[tuple[date, float]]:
    totals: dict[date, float] = {}
    first, last = date(start.year, start.month, 1), date(end.year, end.month, 1)
    m = first
    while m <= last:
        totals[m] = 0.0
        m = date(m.year + m.month // 12, m.month % 12 + 1, 1)
    for t in trades:
        key = date(t.closed.year, t.closed.month, 1)
        totals[key] = totals.get(key, 0.0) + t.pnl
    return sorted(totals.items())


def summarize(r: Result) -> Summary:
    trades = sorted(r.trades, key=lambda t: t.closed)
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    running, peak, drawdown = 0.0, 0.0, 0.0
    curve: list[tuple[datetime, float]] = []
    for t in trades:
        running += t.pnl
        peak = max(peak, running)
        drawdown = min(drawdown, running - peak)
        curve.append((t.closed, running))
    middle = r.start + (r.end - r.start) / 2
    days = max(1, round((r.end - r.start).total_seconds() / 86_400))
    streak, streak_sum = _streak(pnls)
    by_market: dict[str, list[float]] = {}
    for t in trades:
        by_market.setdefault(t.key, []).append(t.pnl)
    skipped: dict[str, int] = {}
    for s in r.skipped:
        skipped[s.reason] = skipped.get(s.reason, 0) + 1
    return Summary(
        start=r.start,
        end=r.end,
        days=days,
        trades=len(pnls),
        wins=len(wins),
        losses=len(losses),
        total=sum(pnls),
        avg_win=sum(wins) / len(wins) if wins else None,
        avg_loss=sum(losses) / len(losses) if losses else None,
        best=max(pnls) if pnls else None,
        worst=min(pnls) if pnls else None,
        stopped=sum(1 for t in trades if t.how == "stop-loss"),
        per_week=len(pnls) / days * 7,
        losing_streak=streak,
        streak_loss=streak_sum,
        max_drawdown=drawdown,
        first_half=sum(t.pnl for t in trades if t.closed < middle),
        second_half=sum(t.pnl for t in trades if t.closed >= middle),
        in_market=r.hours_in_market / r.hours if r.hours else 0.0,
        months=_months(r.start, r.end, trades),
        markets=[
            MarketResult(k, len(v), sum(1 for p in v if p > 0), sum(v))
            for k, v in sorted(by_market.items())
        ],
        skipped=skipped,
        curve=curve,
        stopped_at=r.stopped_at,
    )
