"""The probability that a 1-minute gold entry reaches its take-profit before its stop-loss (S85;
the owner's directive of 2026-11-11: "calibrated probabilities, not invented AI confidence
numbers ... If the model cannot provide a reliable estimate, classify it as NO TRADE").

One trade shape, the owner's: per ounce a stop-loss 1.90 from the price, 0.10 allowed for the
entry's slippage (2.00 at risk, HFM's spread inside it) and a take-profit 4.00 away (1:2). The
probability is the measured frequency, on HFM's own 1-minute candles, with which that target
came before that stop, given what is known when a candle closes:
- the 15-minute and the 5-minute trend (with the trade, flat or against it);
- the 1-minute momentum over three candles;
- the volatility (the 1-minute ATR against the stop);
- the session (the hour, UTC).

Model ``m1-prob-1`` (Run 6 of the lab's pre-registered log, 2026-11-11): a logistic regression
fitted on 2022-11 to 2024-12, its calibration on 2025, tested on 2026: 579,710 signals on 267
days it had never seen. Predicted 26.8 %, happened 26.9 % (95 % interval 26.7 to 27.1). Over
every market condition its estimate spans only 26.1 % to 27.0 %: these features say almost
nothing about which trade wins, and that is what it reports. Break-even at 1:2 is 33.3 %.

So by the owner's own rules (50 % or more: automatic; 30 % to below 50 %: ask; below 30 %:
reject) it opens nothing by itself. A trade below 30 % is opened only as a PRACTICE trade the
owner approved in advance, and is labelled so.

A model is a reviewed, versioned constant: a new one arrives only by a code change with its own
out-of-sample evidence, never by itself. Pure: no database, and no clock but the one passed in.
Indicators are floats (statistics, not money); every price and amount is a Decimal."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from cosmic.market.hfm_candles import Candle

BUY, SELL, WAIT = "BUY", "SELL", "WAIT"
AUTO, ASK, REJECT, NO_ESTIMATE = "AUTO", "ASK", "REJECT", "NO_ESTIMATE"
AUTO_AT, ASK_AT = Decimal("0.50"), Decimal("0.30")  # the owner's decision rules (2026-11-11)
UP, FLAT, DOWN = 0, 1, 2
MIN_CANDLES = 50  # completed 5- and 15-minute candles a trend needs, as it was measured
MIN_M1 = 60
STALE = timedelta(minutes=4)  # the last 1-minute candle opened longer ago: the market is closed
BAND = Decimal("0.50")  # a fill this much better than the price at the decision is still taken
TREND_AT = 0.25  # (EMA20 − EMA50) / ATR14 beyond this: a trend
K20, K50, K21 = 2 / 21, 2 / 51, 2 / 22


@dataclass(frozen=True)
class Model:
    version: str
    stop: Decimal  # from the price to the stop-loss, an ounce
    slippage: Decimal  # the entry's limit beyond the price: counted in the loss
    target: Decimal  # from the price to the take-profit, an ounce
    spread: Decimal  # HFM's spread when it was measured
    max_spread: Decimal  # wider than this now: no estimate
    max_fee: Decimal  # commission both ways, an ounce, above which the measurement does not hold
    beta: tuple[float, ...]
    calibration: tuple[float, float]
    evidence: str

    @property
    def risk(self) -> Decimal:
        """What an ounce can lose at its stop, slippage allowance included."""
        return self.stop + self.slippage

    @property
    def break_even(self) -> Decimal:
        """The probability at which such a trade neither earns nor loses on average."""
        return (self.risk / (self.risk + self.target)).quantize(Decimal("0.0001"))


MODEL = Model(
    version="m1-prob-1",
    stop=Decimal("1.90"),
    slippage=Decimal("0.10"),
    target=Decimal("4.00"),
    spread=Decimal("0.35"),
    max_spread=Decimal("0.50"),
    max_fee=Decimal("0.05"),
    # intercept; M15 flat, against; M5 flat, against; momentum bins 1..4; volatility mid, high;
    # session 07-11, 12-16, 17-22 UTC (the first of each group is the baseline)
    beta=(
        -1.3011,
        0.0314,
        0.0335,
        0.0064,
        -0.0156,
        0.0068,
        0.0087,
        0.0073,
        0.0152,
        0.2454,
        0.2898,
        0.1112,
        -0.0021,
        -0.2927,
    ),
    calibration=(-0.9419, 0.0601),
    evidence=(
        "HFM M1 2022-11 to 2026-11, 2.76 million signals; tested on 2026 (579,710 signals, 267 "
        "days): predicted 26.8 %, happened 26.9 % (95 % interval 26.7 to 27.1); its estimates "
        "span 26.1 % to 27.0 %; break-even 33.3 % (lab Run 6, pre-registered 2026-11-11)"
    ),
)


@dataclass(frozen=True)
class Prediction:
    """What the model says at one 1-minute candle's close."""

    bar: datetime | None
    model: str
    p_buy: Decimal | None  # None: no reliable estimate (``why``), so NO TRADE
    p_sell: Decimal | None
    setup: str  # BUY or SELL when the 1-minute setup is on this candle, else WAIT
    why: str | None
    features: dict[str, Any]

    def of(self, side: str) -> Decimal | None:
        return self.p_buy if side == BUY else self.p_sell if side == SELL else None

    @property
    def best(self) -> tuple[str, Decimal] | None:
        """The direction with the higher probability, and that probability."""
        if self.p_buy is None or self.p_sell is None:
            return None
        return (BUY, self.p_buy) if self.p_buy >= self.p_sell else (SELL, self.p_sell)


def _trend(candles: Sequence[Candle]) -> int | None:
    """Up, flat or down from completed candles, oldest first; None with too few."""
    if len(candles) < MIN_CANDLES:
        return None
    e20 = e50 = atr = prev = None
    for c in candles:
        high, low, close = float(c.high), float(c.low), float(c.close)
        tr = high - low if prev is None else max(high - low, abs(high - prev), abs(low - prev))
        atr = tr if atr is None else (atr * 13 + tr) / 14
        e20 = close if e20 is None else e20 + K20 * (close - e20)
        e50 = close if e50 is None else e50 + K50 * (close - e50)
        prev = close
    if not atr or e20 is None or e50 is None:
        return None
    z = (e20 - e50) / atr
    return UP if z > TREND_AT else DOWN if z < -TREND_AT else FLAT


def _before(candles: Sequence[Candle], bar: datetime, minutes: int) -> list[Candle]:
    """The candles of ``minutes`` complete before the one that holds the 1-minute candle
    ``bar``: what the measurement used (a 5-minute candle that ends with this very minute is
    not yet counted)."""
    start = bar.replace(minute=bar.minute - bar.minute % minutes, second=0, microsecond=0)
    length = timedelta(minutes=minutes)
    return [c for c in candles if c.open_time + length <= start]


def _one_minute(candles: Sequence[Candle]) -> dict[str, float] | None:
    """ATR(14), the 3-candle momentum in ATRs, RSI(14) now and one candle before, EMA(21)."""
    atr = gain = loss = ema = None
    rsi = before = 50.0
    closes: list[float] = []
    for c in candles:
        high, low, close = float(c.high), float(c.low), float(c.close)
        ema = close if ema is None else ema + K21 * (close - ema)
        if closes:
            prev = closes[-1]
            tr = max(high - low, abs(high - prev), abs(low - prev))
            atr = tr if atr is None else (atr * 13 + tr) / 14
            up, down = max(close - prev, 0.0), max(prev - close, 0.0)
            gain = up if gain is None else (gain * 13 + up) / 14
            loss = down if loss is None else (loss * 13 + down) / 14
            before = rsi
            rsi = (
                (50.0 if gain == 0 else 100.0) if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
            )
        closes.append(close)
    if not atr or ema is None or len(closes) < 4:
        return None
    return {
        "atr": atr,
        "momentum": (closes[-1] - closes[-4]) / atr,
        "rsi": rsi,
        "rsi_before": before,
        "ema21": ema,
        "close": closes[-1],
    }


def _p(
    model: Model, t15: int, t5: int, momentum: float, atr: float, hour: int, buy: bool
) -> Decimal:
    a15, a5 = (t15, t5) if buy else (2 - t15, 2 - t5)
    z = momentum if buy else -momentum
    mb = 0 if z > 1 else 1 if z > 0.25 else 2 if z >= -0.25 else 3 if z >= -1 else 4
    v = atr / float(model.stop - model.spread)  # the stop, in 1-minute ATRs of the bid
    vb = 0 if v < 0.4 else 1 if v < 0.8 else 2
    sb = 0 if (hour >= 23 or hour <= 6) else 1 if hour <= 11 else 2 if hour <= 16 else 3
    x = [0.0] * 14
    x[0] = 1.0
    if a15:
        x[a15] = 1.0
    if a5:
        x[2 + a5] = 1.0
    if mb:
        x[4 + mb] = 1.0
    if vb:
        x[8 + vb] = 1.0
    if sb:
        x[10 + sb] = 1.0
    eta = sum(b * xi for b, xi in zip(model.beta, x, strict=True))
    a, b = model.calibration
    return Decimal(str(round(1 / (1 + math.exp(-(a + b * eta))), 4)))


def predict(
    model: Model,
    at: datetime,
    m1: Sequence[Candle],
    m5: Sequence[Candle],
    m15: Sequence[Candle],
    closing: str | None = None,
) -> Prediction:
    """The estimate at the last complete 1-minute candle, from HFM's complete candles (oldest
    first). ``closing``: why gold stops trading within the half hour, if it does (the
    measurement left those entries out, so there is no estimate for them)."""
    if not m1:
        return Prediction(None, model.version, None, None, WAIT, "no 1-minute candles yet", {})
    bar = m1[-1].open_time

    def none(why: str) -> Prediction:
        return Prediction(bar, model.version, None, None, WAIT, why, {})

    if at - bar > STALE:
        return none("no new 1-minute candle lately (the market may be closed)")
    if closing is not None:
        return none(closing)
    if len(m1) < MIN_M1:
        return none(f"{len(m1)} of {MIN_M1} 1-minute candles stored so far")
    t15, t5 = _trend(_before(m15, bar, 15)), _trend(_before(m5, bar, 5))
    if t15 is None or t5 is None:
        return none("too few complete 5- or 15-minute candles for a trend yet")
    one = _one_minute(m1)
    if one is None:
        return none("the 1-minute candles do not move: no volatility to measure")
    turned_up = one["rsi_before"] <= 50 < one["rsi"] and one["close"] > one["ema21"]
    turned_down = one["rsi_before"] >= 50 > one["rsi"] and one["close"] < one["ema21"]
    setup = (
        BUY
        if t15 == UP and t5 == UP and turned_up
        else SELL
        if t15 == DOWN and t5 == DOWN and turned_down
        else WAIT
    )
    names = {UP: "UP", FLAT: "FLAT", DOWN: "DOWN"}
    return Prediction(
        bar,
        model.version,
        _p(model, t15, t5, one["momentum"], one["atr"], bar.hour, True),
        _p(model, t15, t5, one["momentum"], one["atr"], bar.hour, False),
        setup,
        None,
        {
            "m15": names[t15],
            "m5": names[t5],
            "momentum": round(one["momentum"], 3),
            "atr": round(one["atr"], 3),
            "rsi": round(one["rsi"], 1),
            "hour": bar.hour,
        },
    )


def band(p: Decimal | None) -> str:
    """The owner's decision rule for a probability."""
    if p is None:
        return NO_ESTIMATE
    return AUTO if p >= AUTO_AT else ASK if p >= ASK_AT else REJECT


def _tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding) * tick


@dataclass(frozen=True)
class Levels:
    """One trade's prices at HFM, from the price now."""

    side: str
    limit: Decimal  # filled now at this or better, or not at all
    stop: Decimal
    take_profit: Decimal
    band_low: Decimal
    band_high: Decimal


def levels(model: Model, side: str, bid: Decimal, ask: Decimal, tick: Decimal) -> Levels | str:
    """The entry limit, stop-loss and take-profit of the model's trade shape, or why there is
    none: a spread wider than the estimate holds for."""
    tick = tick.normalize()  # 0.01, not 0.010000000000000000: prices as people write them
    spread = ask - bid
    if spread < 0 or spread > model.max_spread:
        return (
            f"HFM's spread is {spread:.2f} an ounce, wider than the {model.max_spread:.2f} the "
            "estimate holds for"
        )
    if side == BUY:
        limit = _tick(ask + model.slippage, tick, ROUND_CEILING)
        return Levels(
            BUY,
            limit,
            _tick(ask - model.stop, tick, ROUND_DOWN),
            _tick(ask + model.target, tick, ROUND_DOWN),
            _tick(ask - BAND, tick, ROUND_DOWN),
            limit,
        )
    limit = _tick(bid - model.slippage, tick, ROUND_DOWN)
    return Levels(
        SELL,
        limit,
        _tick(bid + model.stop, tick, ROUND_CEILING),
        _tick(bid - model.target, tick, ROUND_CEILING),
        limit,
        _tick(bid + BAND, tick, ROUND_CEILING),
    )


@dataclass(frozen=True)
class Economics:
    """What a trade of ``quantity`` ounces can lose and earn after costs, and its average."""

    max_loss: Decimal  # at the stop, filled at the limit, commission both ways
    net_at_target: Decimal  # at the take-profit, filled at the price now
    probability: Decimal
    expected: Decimal  # the average result of such a trade, by the measured probability
    reward_to_risk: Decimal


def economics(model: Model, quantity: Decimal, fee: Decimal, p: Decimal) -> Economics:
    """``fee``: commission both ways, an ounce."""
    cent = Decimal("0.01")
    loss = (quantity * (model.risk + fee)).quantize(cent)
    net = (quantity * (model.target - fee)).quantize(cent)
    return Economics(
        loss,
        net,
        p,
        (p * net - (1 - p) * loss).quantize(cent),
        (net / loss).quantize(cent) if loss else Decimal(0),
    )
