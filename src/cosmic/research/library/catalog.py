"""The strategy library: 100 published or widely documented rules, long or flat, daily bars.

Each entry states its rule in plain language, the source it comes from and any caveat.
Parameters are the published or conventional defaults — they are NOT tuned on our data,
so the library is tested once, as specified. That still leaves selection bias: testing 100
rules guarantees some look good by luck, which is why every result is judged with the
Deflated Sharpe ratio over all trials (``evaluate.py``).

Adaptations, applied to every rule alike:
- Long or flat only (spot, no leverage, no shorting) — short legs of the originals are cash.
- Daily UTC bars; decisions at the close, execution at the next open. Rules defined on
  intraday stop entries are close-confirmed daily variants (noted per rule).
- 24/7 markets: a "month" is a calendar month, "12 months" is 365 days.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from cosmic.research.library import ta
from cosmic.research.library.engine import Market, Panel

Weights = list[float]
SingleFn = Callable[[Market, dict[str, Any]], Weights]
PanelFn = Callable[[Panel, dict[str, Any]], dict[str, Weights]]


@dataclass(frozen=True)
class Spec:
    number: int
    key: str
    name: str
    family: str
    rule: str
    source: str
    params: dict[str, Any]
    fn: SingleFn | None = None
    panel_fn: PanelFn | None = None
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def kind(self) -> str:
        return "PANEL" if self.panel_fn is not None else "SINGLE"


# ---- building blocks ------------------------------------------------------------------------


def regime(cond: Sequence[bool | None]) -> Weights:
    return [1.0 if c else 0.0 for c in cond]


def state(enter: Sequence[bool | None], exit_: Sequence[bool | None]) -> Weights:
    """Flat → long when ``enter``; long → flat when ``exit_`` (checked while long)."""
    out: Weights = []
    pos = 0.0
    for e, x in zip(enter, exit_, strict=True):
        if pos == 0.0 and e:
            pos = 1.0
        elif pos == 1.0 and x:
            pos = 0.0
        out.append(pos)
    return out


def hold_for(
    enter: Sequence[bool | None], bars: int, exit_: Sequence[bool | None] | None = None
) -> Weights:
    """Enter on a signal, exit after ``bars`` bars (or earlier on ``exit_``)."""
    out: Weights = []
    left = 0
    for t, e in enumerate(enter):
        if left > 0:
            left -= 1
            if left == 0 or (exit_ is not None and exit_[t]):
                left = 0
        elif e:
            left = bars
        out.append(1.0 if left > 0 else 0.0)
    return out


def gt(a: Sequence[float | None], b: Sequence[float | None]) -> list[bool | None]:
    return [None if x is None or y is None else x > y for x, y in zip(a, b, strict=True)]


def lt(a: Sequence[float | None], b: Sequence[float | None]) -> list[bool | None]:
    return [None if x is None or y is None else x < y for x, y in zip(a, b, strict=True)]


def all_of(*conds: Sequence[bool | None]) -> list[bool | None]:
    return [None if any(c is None for c in row) else all(row) for row in zip(*conds, strict=True)]


def any_of(*conds: Sequence[bool | None]) -> list[bool | None]:
    return [bool(any(c for c in row if c is not None)) for row in zip(*conds, strict=True)]


def scaled(series: Sequence[float | None], k: float) -> list[float | None]:
    return [None if v is None else v * k for v in series]


def shift(series: Sequence[Any], n: int = 1) -> list[Any]:
    return [None] * n + list(series[: len(series) - n])


def rising(series: Sequence[float | None], n: int = 1) -> list[bool | None]:
    return gt(series, shift(series, n))


def month_end(d: date) -> bool:
    return (d + timedelta(days=1)).month != d.month


def on_month_end(m: Market, signal: Weights) -> Weights:
    """Hold each month-end decision for the whole next month (calendar known in advance)."""
    out: Weights = []
    cur = 0.0
    for d, s in zip(m.dates, signal, strict=True):
        if month_end(d):
            cur = s
        out.append(cur)
    return out


def on_weekday(m: Market, signal: Weights, weekday: int = 6) -> Weights:
    """Rebalance once a week (Sunday close by default)."""
    out: Weights = []
    cur = 0.0
    for d, s in zip(m.dates, signal, strict=True):
        if d.weekday() == weekday:
            cur = s
        out.append(cur)
    return out


def month_end_closes(m: Market) -> tuple[list[int], list[float]]:
    idx = [i for i, d in enumerate(m.dates) if month_end(d)]
    return idx, [m.close[i] for i in idx]


def on_month_series(
    m: Market, monthly: Sequence[float | None], idx: Sequence[int]
) -> list[float | None]:
    """Spread a series computed on month-end closes back to daily bars (step function)."""
    out: list[float | None] = [None] * len(m)
    k = -1
    for t in range(len(m)):
        while k + 1 < len(idx) and idx[k + 1] <= t:
            k += 1
        out[t] = monthly[k] if k >= 0 else None
    return out


# ---- rule builders ----------------------------------------------------------------------------


def _ma(kind: str, values: Sequence[float], n: int) -> list[float | None]:
    return {"sma": ta.sma, "ema": ta.ema, "dema": ta.dema, "tema": ta.tema}[kind](values, n)


def close_above_ma(m: Market, p: dict[str, Any]) -> Weights:
    return regime(gt(m.close, _ma(p["ma"], m.close, p["n"])))


def ma_over_ma(m: Market, p: dict[str, Any]) -> Weights:
    return regime(gt(_ma(p["ma"], m.close, p["fast"]), _ma(p["ma"], m.close, p["slow"])))


def triple_ma(m: Market, p: dict[str, Any]) -> Weights:
    a, b, c = (ta.sma(m.close, n) for n in (p["a"], p["b"], p["c"]))
    return regime(all_of(gt(a, b), gt(b, c)))


def faber_monthly(m: Market, p: dict[str, Any]) -> Weights:
    idx, closes = month_end_closes(m)
    ma = ta.sma(closes, p["months"])
    above = [None if a is None else c > a for c, a in zip(closes, ma, strict=True)]
    daily = on_month_series(m, [None if x is None else float(x) for x in above], idx)
    return [1.0 if d else 0.0 for d in daily]


def hma_rising(m: Market, p: dict[str, Any]) -> Weights:
    return regime(rising(ta.hma(m.close, p["n"])))


def close_above_kama(m: Market, p: dict[str, Any]) -> Weights:
    return regime(gt(m.close, ta.kama(m.close, p["n"], p["fast"], p["slow"])))


def slope_positive(m: Market, p: dict[str, Any]) -> Weights:
    return regime([None if s is None else s > 0 for s in ta.linreg_slope(m.close, p["n"])])


def envelope_breakout(m: Market, p: dict[str, Any]) -> Weights:
    ma = ta.sma(m.close, p["n"])
    return state(gt(m.close, scaled(ma, 1 + p["pct"])), lt(m.close, ma))


def ma_and_rising(m: Market, p: dict[str, Any]) -> Weights:
    ma = ta.sma(m.close, p["n"])
    return regime(all_of(gt(m.close, ma), rising(ma, p["lookback"])))


def above_two_mas(m: Market, p: dict[str, Any]) -> Weights:
    return regime(
        all_of(gt(m.close, ta.sma(m.close, p["a"])), gt(m.close, ta.sma(m.close, p["b"])))
    )


def donchian(m: Market, p: dict[str, Any]) -> Weights:
    return state(
        gt(m.close, ta.prior_highest(m.high, p["entry"])),
        lt(m.close, ta.prior_lowest(m.low, p["exit"])),
    )


def trading_range_breakout(m: Market, p: dict[str, Any]) -> Weights:
    return hold_for(gt(m.close, ta.prior_highest(m.close, p["n"])), p["hold"])


def keltner_breakout(m: Market, p: dict[str, Any]) -> Weights:
    _, mid, upper = ta.keltner(m.high, m.low, m.close, p["n"], p["k"], p["atr_n"])
    return state(gt(m.close, upper), lt(m.close, mid))


def bollinger_breakout(m: Market, p: dict[str, Any]) -> Weights:
    _, mid, upper = ta.bollinger(m.close, p["n"], p["k"])
    return state(gt(m.close, upper), lt(m.close, mid))


def atr_channel(m: Market, p: dict[str, Any]) -> Weights:
    ma, a = ta.sma(m.close, p["n"]), ta.atr(m.high, m.low, m.close, p["atr_n"])
    upper = [None if x is None or y is None else x + p["k"] * y for x, y in zip(ma, a, strict=True)]
    return state(gt(m.close, upper), lt(m.close, ma))


def volatility_breakout(m: Market, p: dict[str, Any]) -> Weights:
    rng = [h - lo for h, lo in zip(m.high, m.low, strict=True)]
    trigger = [None] + [m.open[t] + p["k"] * rng[t - 1] for t in range(1, len(m))]
    return hold_for(gt(m.close, trigger), p["hold"])


def nr7_breakout(m: Market, p: dict[str, Any]) -> Weights:
    rng = [h - lo for h, lo in zip(m.high, m.low, strict=True)]
    narrowest = [
        None if t < p["n"] - 1 else rng[t] == min(rng[t - p["n"] + 1 : t + 1])
        for t in range(len(m))
    ]
    enter = [
        None
        if t < 1 or narrowest[t - 1] is None
        else bool(narrowest[t - 1]) and m.close[t] > m.high[t - 1]
        for t in range(len(m))
    ]
    return hold_for(enter, p["hold"])


def near_high(m: Market, p: dict[str, Any]) -> Weights:
    hh = ta.highest(m.close, p["n"])
    return state(gt(m.close, scaled(hh, p["enter"])), lt(m.close, scaled(hh, p["exit"])))


def new_high_trailing(m: Market, p: dict[str, Any]) -> Weights:
    prior = ta.prior_highest(m.close, p["n"])
    out: Weights = []
    pos, peak = 0.0, 0.0
    for t, c in enumerate(m.close):
        if pos == 0.0 and prior[t] is not None and c > prior[t]:  # type: ignore[operator]
            pos, peak = 1.0, c
        elif pos == 1.0:
            peak = max(peak, c)
            if c < peak * (1 - p["trail"]):
                pos = 0.0
        out.append(pos)
    return out


def darvas_box(m: Market, p: dict[str, Any]) -> Weights:
    """Simplified Darvas box: a top that held for ``confirm`` bars; exit below the box floor."""
    n, k = p["n"], p["confirm"]
    out: Weights = []
    pos, floor = 0.0, 0.0
    for t in range(len(m)):
        if pos == 0.0 and t >= n:
            top = max(m.high[t - n : t - k])
            held = max(m.high[t - k : t]) <= top
            if held and m.close[t] > top:
                pos, floor = 1.0, min(m.low[t - n : t])
        elif pos == 1.0 and m.close[t] < floor:
            pos = 0.0
        out.append(pos)
    return out


def supertrend_rule(m: Market, p: dict[str, Any]) -> Weights:
    return regime(ta.supertrend(m.high, m.low, m.close, p["n"], p["k"]))


def chandelier(m: Market, p: dict[str, Any]) -> Weights:
    hh, a = ta.highest(m.high, p["n"]), ta.atr(m.high, m.low, m.close, p["n"])
    line = [None if h is None or x is None else h - p["k"] * x for h, x in zip(hh, a, strict=True)]
    return state(gt(m.close, ta.prior_highest(m.high, p["n"])), lt(m.close, line))


def psar_rule(m: Market, p: dict[str, Any]) -> Weights:
    return regime(ta.psar(m.high, m.low, p["step"], p["cap"]))


def ichimoku_rule(m: Market, p: dict[str, Any]) -> Weights:
    tk, kj, a, b = ta.ichimoku(m.high, m.low, p["tenkan"], p["kijun"], p["senkou_b"])
    top = [None if x is None or y is None else max(x, y) for x, y in zip(a, b, strict=True)]
    bottom = [None if x is None or y is None else min(x, y) for x, y in zip(a, b, strict=True)]
    return state(all_of(gt(m.close, top), gt(tk, kj)), any_of(lt(m.close, bottom), lt(tk, kj)))


def adx_rule(m: Market, p: dict[str, Any]) -> Weights:
    pdi, mdi, adx = ta.dmi(m.high, m.low, m.close, p["n"])
    strong = [None if x is None else x > p["threshold"] for x in adx]
    return state(all_of(strong, gt(pdi, mdi)), lt(pdi, mdi))


def aroon_rule(m: Market, p: dict[str, Any]) -> Weights:
    up, down = ta.aroon(m.high, m.low, p["n"])
    strong = [None if u is None else u > p["threshold"] for u in up]
    return state(all_of(strong, gt(up, down)), lt(up, down))


def vortex_rule(m: Market, p: dict[str, Any]) -> Weights:
    plus, minus = ta.vortex(m.high, m.low, m.close, p["n"])
    return regime(gt(plus, minus))


def heikin_ashi_rule(m: Market, p: dict[str, Any]) -> Weights:
    o, c = ta.heikin_ashi(m.open, m.high, m.low, m.close)
    bull = [x > y for x, y in zip(c, o, strict=True)]
    n = p["confirm"]
    enter = [t >= n - 1 and all(bull[t - n + 1 : t + 1]) for t in range(len(m))]
    exit_ = [t >= n - 1 and not any(bull[t - n + 1 : t + 1]) for t in range(len(m))]
    return state(enter, exit_)


def tsmom(m: Market, p: dict[str, Any]) -> Weights:
    r = ta.roc(m.close, p["n"])
    skip = p.get("skip", 0)
    if skip:
        r = [
            None
            if t < p["n"] or m.close[t - p["n"]] <= 0
            else m.close[t - skip] / m.close[t - p["n"]] - 1
            for t in range(len(m))
        ]
    sig = regime([None if x is None else x > 0 for x in r])
    return on_month_end(m, sig) if p.get("rebalance") == "monthly" else sig


def trend_vote(m: Market, p: dict[str, Any]) -> Weights:
    votes = [ta.roc(m.close, n) for n in p["horizons"]]
    out: Weights = []
    for t in range(len(m)):
        vals = [v[t] for v in votes]
        out.append(
            0.0 if any(x is None for x in vals) else sum(1 for x in vals if x and x > 0) / len(vals)
        )
    return out


def roc_rule(m: Market, p: dict[str, Any]) -> Weights:
    pos = [None if x is None else x > 0 for x in ta.roc(m.close, p["n"])]
    if p.get("trend_n"):
        return regime(all_of(pos, gt(m.close, ta.sma(m.close, p["trend_n"]))))
    return regime(pos)


def macd_rule(m: Market, p: dict[str, Any]) -> Weights:
    line, sig, hist = ta.macd(m.close, p["fast"], p["slow"], p["signal"])
    zero = [0.0] * len(m)
    if p["mode"] == "signal":
        return regime(gt(line, sig))
    if p["mode"] == "zero":
        return regime(gt(line, zero))
    return regime(all_of(gt(hist, zero), gt(line, zero)))


def trix_rule(m: Market, p: dict[str, Any]) -> Weights:
    return regime([None if x is None else x > 0 for x in ta.trix(m.close, p["n"])])


def kst_rule(m: Market, p: dict[str, Any]) -> Weights:
    k, s = ta.kst(m.close)
    return regime(gt(k, s))


def coppock_rule(m: Market, p: dict[str, Any]) -> Weights:
    idx, closes = month_end_closes(m)
    cc = ta.coppock(closes, p["r1"], p["r2"], p["n"])
    pos: Weights = []
    cur = 0.0
    for k in range(len(cc)):
        a, b = cc[k], cc[k - 1] if k else None
        if a is not None and b is not None:
            if cur == 0.0 and a < 0 and a > b:
                cur = 1.0
            elif cur == 1.0 and a > 0 and a < b:
                cur = 0.0
        pos.append(cur)
    daily = on_month_series(m, pos, idx)
    return [x or 0.0 for x in daily]


def elder_impulse(m: Market, p: dict[str, Any]) -> Weights:
    e = ta.ema(m.close, p["ema"])
    _, _, hist = ta.macd(m.close, 12, 26, 9)
    green = all_of(rising(e), rising(hist))
    red = all_of(lt(e, shift(e)), lt(hist, shift(hist)))
    return state(green, red)


def momentum_low_vol(m: Market, p: dict[str, Any]) -> Weights:
    pos = [None if x is None else x > 0 for x in ta.roc(m.close, p["n"])]
    return regime(
        all_of(pos, lt(ta.realized_vol(m.close, p["short"]), ta.realized_vol(m.close, p["long"])))
    )


def rsi_momentum(m: Market, p: dict[str, Any]) -> Weights:
    r = ta.rsi(m.close, p["n"])
    return state(
        [None if x is None else x > p["enter"] for x in r],
        [None if x is None else x < p["exit"] for x in r],
    )


def cci_trend(m: Market, p: dict[str, Any]) -> Weights:
    c = ta.cci(m.high, m.low, m.close, p["n"])
    return state(
        [None if x is None else x > p["enter"] for x in c],
        [None if x is None else x < p["exit"] for x in c],
    )


def connors_rsi2(m: Market, p: dict[str, Any]) -> Weights:
    r, trend, exit_ma = ta.rsi(m.close, 2), ta.sma(m.close, 200), ta.sma(m.close, p["exit_ma"])
    oversold = [None if x is None else x < p["below"] for x in r]
    return state(all_of(gt(m.close, trend), oversold), gt(m.close, exit_ma))


def connors_cumulative(m: Market, p: dict[str, Any]) -> Weights:
    r = ta.rsi(m.close, 2)
    cum = [
        None if t < 1 or r[t] is None or r[t - 1] is None else r[t] + r[t - 1]  # type: ignore[operator]
        for t in range(len(m))
    ]
    return state(
        all_of(
            gt(m.close, ta.sma(m.close, 200)), [None if x is None else x < p["below"] for x in cum]
        ),
        [None if x is None else x > p["exit_rsi"] for x in r],
    )


def double_sevens(m: Market, p: dict[str, Any]) -> Weights:
    low7, high7 = ta.lowest(m.close, p["n"]), ta.highest(m.close, p["n"])
    at_low = [None if x is None else c <= x for c, x in zip(m.close, low7, strict=True)]
    at_high = [None if x is None else c >= x for c, x in zip(m.close, high7, strict=True)]
    return state(all_of(gt(m.close, ta.sma(m.close, 200)), at_low), at_high)


def multiple_days_down(m: Market, p: dict[str, Any]) -> Weights:
    down = [None] + [m.close[t] < m.close[t - 1] for t in range(1, len(m))]
    count = [
        None if t < 5 else sum(1 for x in down[t - 4 : t + 1] if x) >= p["of5"]
        for t in range(len(m))
    ]
    sma5 = ta.sma(m.close, 5)
    return state(
        all_of(gt(m.close, ta.sma(m.close, 200)), lt(m.close, sma5), count), gt(m.close, sma5)
    )


def connors_rsi_rule(m: Market, p: dict[str, Any]) -> Weights:
    crsi = ta.connors_rsi(m.close)
    return state(
        all_of(
            gt(m.close, ta.sma(m.close, 200)), [None if x is None else x < p["below"] for x in crsi]
        ),
        [None if x is None else x > p["exit"] for x in crsi],
    )


def rsi_reversion(m: Market, p: dict[str, Any]) -> Weights:
    r = ta.rsi(m.close, p["n"])
    enter = [None if x is None else x < p["below"] for x in r]
    if p.get("trend"):
        enter = all_of(enter, gt(m.close, ta.sma(m.close, 200)))
    return state(enter, [None if x is None else x > p["exit"] for x in r])


def bollinger_reversion(m: Market, p: dict[str, Any]) -> Weights:
    lower, mid, _ = ta.bollinger(m.close, p["n"], p["k"])
    enter = lt(m.close, lower)
    if p.get("trend"):
        enter = all_of(enter, gt(m.close, ta.sma(m.close, 200)))
    return state(enter, gt(m.close, mid))


def zscore_reversion(m: Market, p: dict[str, Any]) -> Weights:
    z = ta.zscore(m.close, p["n"])
    return state(
        [None if x is None else x < -p["k"] for x in z], [None if x is None else x > 0 for x in z]
    )


def williams_r_rule(m: Market, p: dict[str, Any]) -> Weights:
    w = ta.williams_r(m.high, m.low, m.close, p["n"])
    return state(
        all_of(
            gt(m.close, ta.sma(m.close, 200)), [None if x is None else x < p["below"] for x in w]
        ),
        [None if x is None else x > p["exit"] for x in w],
    )


def stochastic_cross(m: Market, p: dict[str, Any]) -> Weights:
    k, d = ta.stochastic(m.high, m.low, m.close, p["n"], p["d"])
    cross = [
        None
        if t < 1 or None in (k[t], d[t], k[t - 1], d[t - 1])
        else k[t] > d[t] and k[t - 1] <= d[t - 1] and k[t] < p["below"]  # type: ignore[operator]
        for t in range(len(m))
    ]
    return state(cross, [None if x is None else x > p["exit"] for x in k])


def ibs_rule(m: Market, p: dict[str, Any]) -> Weights:
    v = ta.ibs(m.high, m.low, m.close)
    return state([x < p["below"] for x in v], [x > p["exit"] for x in v])


def mfi_rule(m: Market, p: dict[str, Any]) -> Weights:
    v = ta.mfi(m.high, m.low, m.close, m.volume, p["n"])
    return state(
        [None if x is None else x < p["below"] for x in v],
        [None if x is None else x > p["exit"] for x in v],
    )


def deviation_reversion(m: Market, p: dict[str, Any]) -> Weights:
    ma = ta.sma(m.close, p["n"])
    return state(lt(m.close, scaled(ma, 1 - p["pct"])), gt(m.close, ma))


def shock_rebound(m: Market, p: dict[str, Any]) -> Weights:
    r = ta.log_returns(m.close)
    sd: list[float | None] = [None] * len(m)
    for t in range(p["n"] + 1, len(m)):
        w = [x for x in r[t - p["n"] : t] if x is not None]  # σ from the n bars BEFORE today
        if len(w) == p["n"]:
            mu = sum(w) / len(w)
            sd[t] = (sum((x - mu) ** 2 for x in w) / (len(w) - 1)) ** 0.5
    enter: list[bool | None] = []
    for rt, st in zip(r, sd, strict=True):
        enter.append(None if rt is None or st is None else rt < -p["k"] * st)
    return hold_for(enter, p["hold"])


def pullback_in_uptrend(m: Market, p: dict[str, Any]) -> Weights:
    fast = ta.sma(m.close, p["fast"])
    return state(
        all_of(gt(m.close, ta.sma(m.close, p["trend"])), lt(m.close, fast)), gt(m.close, fast)
    )


def vol_target(m: Market, p: dict[str, Any]) -> Weights:
    rv = ta.realized_vol(m.close, p["n"])
    out: Weights = []
    for t in range(len(m)):
        hist = [x for x in rv[max(0, t - p["lookback"]) : t + 1] if x is not None]
        if len(hist) < p["lookback"] // 2 or rv[t] is None or rv[t] == 0:
            out.append(0.0)
            continue
        target = sorted(hist)[len(hist) // 2]
        out.append(min(1.0, target / rv[t]))  # type: ignore[operator]
    return on_weekday(m, out)


def vol_contraction(m: Market, p: dict[str, Any]) -> Weights:
    return regime(
        all_of(
            lt(ta.realized_vol(m.close, p["short"]), ta.realized_vol(m.close, p["long"])),
            gt(m.close, ta.sma(m.close, p["trend"])),
        )
    )


def squeeze(m: Market, p: dict[str, Any]) -> Weights:
    lower, mid, upper = ta.bollinger(m.close, p["n"], p["k"])
    bw = [
        None if lo is None or mi is None or up is None or mi == 0 else (up - lo) / mi
        for lo, mi, up in zip(lower, mid, upper, strict=True)
    ]
    low_bw: list[bool | None] = [None] * len(m)
    for t in range(p["lookback"], len(m)):
        w = [x for x in bw[t - p["lookback"] : t + 1] if x is not None]
        if len(w) > p["lookback"] // 2:
            recent = [x for x in bw[t - p["recent"] : t + 1] if x is not None]
            low_bw[t] = bool(recent) and min(recent) <= min(w)
    return state(all_of(low_bw, gt(m.close, upper)), lt(m.close, mid))


def range_expansion(m: Market, p: dict[str, Any]) -> Weights:
    tr = ta.true_range(m.high, m.low, m.close)
    a = shift(ta.atr(m.high, m.low, m.close, p["n"]))
    enter = [
        None if a[t] is None else tr[t] > p["k"] * a[t] and m.close[t] > m.open[t]
        for t in range(len(m))
    ]
    return hold_for(enter, p["hold"], lt(m.close, ta.sma(m.close, p["exit_ma"])))


def choppiness_rule(m: Market, p: dict[str, Any]) -> Weights:
    ch = ta.choppiness(m.high, m.low, m.close, p["n"])
    return regime(
        all_of(
            [None if x is None else x < p["below"] for x in ch],
            gt(m.close, ta.sma(m.close, p["trend"])),
        )
    )


def efficiency_rule(m: Market, p: dict[str, Any]) -> Weights:
    er, up = ta.efficiency_ratio(m.close, p["n"]), ta.roc(m.close, p["n"])
    return state(
        all_of(
            [None if x is None else x > p["enter"] for x in er],
            [None if x is None else x > 0 for x in up],
        ),
        any_of(
            [None if x is None else x < p["exit"] for x in er],
            [None if x is None else x < 0 for x in up],
        ),
    )


def obv_rule(m: Market, p: dict[str, Any]) -> Weights:
    o = ta.obv(m.close, m.volume)
    return regime(all_of(gt(o, ta.sma(o, p["n"])), gt(m.close, ta.sma(m.close, p["trend"]))))


def cmf_rule(m: Market, p: dict[str, Any]) -> Weights:
    v = ta.cmf(m.high, m.low, m.close, m.volume, p["n"])
    return state(
        [None if x is None else x > p["enter"] for x in v],
        [None if x is None else x < p["exit"] for x in v],
    )


def volume_breakout(m: Market, p: dict[str, Any]) -> Weights:
    loud = gt(m.volume, scaled(ta.sma(m.volume, p["n"]), p["k"]))
    return state(
        all_of(gt(m.close, ta.prior_highest(m.high, p["n"])), loud),
        lt(m.close, ta.prior_lowest(m.low, p["exit"])),
    )


def ad_rule(m: Market, p: dict[str, Any]) -> Weights:
    a = ta.ad_line(m.high, m.low, m.close, m.volume)
    return regime(all_of(gt(a, ta.sma(a, p["n"])), gt(m.close, ta.sma(m.close, p["n"]))))


def turn_of_month(m: Market, p: dict[str, Any]) -> Weights:
    """Decide at today's close for TOMORROW (calendar): hold if tomorrow is in the window."""
    out: Weights = []
    for d in m.dates:
        nxt = d + timedelta(days=1)
        last = (nxt + timedelta(days=p["last"])).month != nxt.month
        out.append(1.0 if nxt.day <= p["first"] or last else 0.0)
    return out


def months_in(m: Market, p: dict[str, Any]) -> Weights:
    return [1.0 if (d + timedelta(days=1)).month in p["months"] else 0.0 for d in m.dates]


def weekday_only(m: Market, p: dict[str, Any]) -> Weights:
    return [1.0 if (d + timedelta(days=1)).weekday() in p["weekdays"] else 0.0 for d in m.dates]


HALVINGS = (date(2012, 11, 28), date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 20))


def halving_cycle(m: Market, p: dict[str, Any]) -> Weights:
    out: Weights = []
    for d in m.dates:
        nxt = d + timedelta(days=1)
        out.append(1.0 if any(h <= nxt < h + timedelta(days=p["days"]) for h in HALVINGS) else 0.0)
    return out


def trend_ensemble(m: Market, p: dict[str, Any]) -> Weights:
    parts = [
        close_above_ma(m, {"ma": "sma", "n": 50}),
        close_above_ma(m, {"ma": "sma", "n": 200}),
        donchian(m, {"entry": 55, "exit": 20}),
        tsmom(m, {"n": 91}),
        tsmom(m, {"n": 182}),
        tsmom(m, {"n": 365}),
    ]
    return [sum(x) / len(parts) for x in zip(*parts, strict=True)]


def trend_vol_scaled(m: Market, p: dict[str, Any]) -> Weights:
    sign, vt = tsmom(m, {"n": 365}), vol_target(m, {"n": 30, "lookback": 365})
    return [s * v for s, v in zip(sign, vt, strict=True)]


def reversion_ensemble(m: Market, p: dict[str, Any]) -> Weights:
    trend = gt(m.close, ta.sma(m.close, 200))
    parts = [
        connors_rsi2(m, {"below": 10, "exit_ma": 5}),
        bollinger_reversion(m, {"n": 20, "k": 2.0}),
        ibs_rule(m, {"below": 0.2, "exit": 0.8}),
    ]
    return [
        (sum(x) / len(parts)) if t else 0.0
        for x, t in zip(zip(*parts, strict=True), trend, strict=True)
    ]


def regime_switch(m: Market, p: dict[str, Any]) -> Weights:
    trend = gt(m.close, ta.sma(m.close, 200))
    follow = ma_over_ma(m, {"ma": "sma", "fast": 20, "slow": 50})
    revert = connors_rsi2(m, {"below": 5, "exit_ma": 5})
    return [f if up else r for f, r, up in zip(follow, revert, trend, strict=True)]


# ---- two-asset rotation (BTC and gold) ---------------------------------------------------


def _keys(pn: Panel) -> tuple[str, str]:
    k = list(pn.markets)
    return k[0], k[1]


def dual_momentum(pn: Panel, p: dict[str, Any]) -> dict[str, Weights]:
    a, b = _keys(pn)
    ra, rb = ta.roc(pn.markets[a].close, p["n"]), ta.roc(pn.markets[b].close, p["n"])
    wa: Weights = []
    wb: Weights = []
    for x, y in zip(ra, rb, strict=True):
        if x is None or y is None:
            wa.append(0.0)
            wb.append(0.0)
            continue
        best, ret = (a, x) if x >= y else (b, y)
        wa.append(1.0 if best == a and ret > 0 else 0.0)
        wb.append(1.0 if best == b and ret > 0 else 0.0)
    ma = pn.markets[a]
    return {a: on_month_end(ma, wa), b: on_month_end(ma, wb)}


def both_trend(pn: Panel, p: dict[str, Any]) -> dict[str, Weights]:
    out = {}
    for k, mk in pn.markets.items():
        out[k] = [0.5 * w for w in on_month_end(mk, close_above_ma(mk, {"ma": "sma", "n": p["n"]}))]
    return out


def inverse_vol(pn: Panel, p: dict[str, Any]) -> dict[str, Weights]:
    a, b = _keys(pn)
    va, vb = (ta.realized_vol(pn.markets[k].close, p["n"]) for k in (a, b))
    wa, wb = [], []
    for x, y in zip(va, vb, strict=True):
        if not x or not y:
            wa.append(0.0)
            wb.append(0.0)
            continue
        ia, ib = 1 / x, 1 / y
        wa.append(ia / (ia + ib))
        wb.append(ib / (ia + ib))
    ma = pn.markets[a]
    return {a: on_month_end(ma, wa), b: on_month_end(ma, wb)}


def ratio_reversion(pn: Panel, p: dict[str, Any]) -> dict[str, Weights]:
    a, b = _keys(pn)
    ratio = [x / y for x, y in zip(pn.markets[a].close, pn.markets[b].close, strict=True)]
    z = ta.zscore(ratio, p["n"])
    wa, wb = [], []
    for v in z:
        if v is None:
            wa.append(0.0)
            wb.append(0.0)
        elif v < -p["k"]:
            wa.append(1.0)
            wb.append(0.0)
        elif v > p["k"]:
            wa.append(0.0)
            wb.append(1.0)
        else:
            wa.append(0.5)
            wb.append(0.5)
    return {a: wa, b: wb}


# ---- the catalogue ----------------------------------------------------------------------------

BLL = (
    "Brock, Lakonishok & LeBaron (1992), 'Simple Technical Trading Rules and the Stochastic "
    "Properties of Stock Returns', Journal of Finance 47(5)"
)
DETZEL = (
    "Detzel, Liu, Strauss, Zhou & Zhu (2021), 'Learning and Predictability via Technical "
    "Analysis: Evidence from Bitcoin and Stocks with Hard-to-Value Fundamentals', "
    "Financial Management 50(1)"
)
KAUFMAN13 = "Kaufman (2013), Trading Systems and Methods, 5th ed., Wiley"
FABER = (
    "Faber (2007), 'A Quantitative Approach to Tactical Asset Allocation', Journal of "
    "Wealth Management 9(4)"
)
MOP = (
    "Moskowitz, Ooi & Pedersen (2012), 'Time Series Momentum', Journal of Financial "
    "Economics 104(2)"
)
HOP = (
    "Hurst, Ooi & Pedersen (2017), 'A Century of Evidence on Trend-Following Investing', "
    "Journal of Portfolio Management 44(1)"
)
LT = (
    "Liu & Tsyvinski (2021), 'Risks and Returns of Cryptocurrency', Review of Financial "
    "Studies 34(6)"
)
TURTLE = "Faith (2007), Way of the Turtle, McGraw-Hill (Turtle trading rules, Donchian channels)"
WILDER = "Wilder (1978), New Concepts in Technical Trading Systems, Trend Research"
CA = "Connors & Alvarez (2009), Short Term Trading Strategies That Work, TradingMarkets"
BOLL = "Bollinger (2001), Bollinger on Bollinger Bands, McGraw-Hill"
ACHELIS = "Achelis (2000), Technical Analysis from A to Z, 2nd ed., McGraw-Hill"
CLOSE_CONFIRMED = (
    "Close-confirmed daily variant of a rule originally traded with intraday stop entries."
)

S: list[Spec] = []


def add(
    key: str,
    name: str,
    family: str,
    rule: str,
    source: str,
    params: dict[str, Any],
    fn: SingleFn | None = None,
    panel_fn: PanelFn | None = None,
    note: str = "",
) -> None:
    S.append(Spec(len(S) + 1, key, name, family, rule, source, params, fn, panel_fn, note))


# Trend: moving averages
add(
    "bll_vma_1_50",
    "Price above 50-day average (BLL)",
    "TREND_MA",
    "Hold while the close is above its 50-day simple moving average.",
    BLL,
    {"ma": "sma", "n": 50},
    close_above_ma,
)
add(
    "bll_vma_1_150",
    "Price above 150-day average (BLL)",
    "TREND_MA",
    "Hold while the close is above its 150-day simple moving average.",
    BLL,
    {"ma": "sma", "n": 150},
    close_above_ma,
)
add(
    "bll_vma_5_150",
    "5-day over 150-day average (BLL)",
    "TREND_MA",
    "Hold while the 5-day average is above the 150-day average.",
    BLL,
    {"ma": "sma", "fast": 5, "slow": 150},
    ma_over_ma,
)
add(
    "bll_vma_1_200",
    "Price above 200-day average (BLL)",
    "TREND_MA",
    "Hold while the close is above its 200-day simple moving average.",
    BLL,
    {"ma": "sma", "n": 200},
    close_above_ma,
)
add(
    "bll_vma_2_200",
    "2-day over 200-day average (BLL)",
    "TREND_MA",
    "Hold while the 2-day average is above the 200-day average.",
    BLL,
    {"ma": "sma", "fast": 2, "slow": 200},
    ma_over_ma,
)
add(
    "detzel_sma_10",
    "Price above 10-day average (bitcoin study)",
    "TREND_MA",
    "Hold while the close is above its 10-day moving average.",
    DETZEL,
    {"ma": "sma", "n": 10},
    close_above_ma,
)
add(
    "detzel_sma_20",
    "Price above 20-day average (bitcoin study)",
    "TREND_MA",
    "Hold while the close is above its 20-day moving average.",
    DETZEL,
    {"ma": "sma", "n": 20},
    close_above_ma,
)
add(
    "ema_50_trend",
    "Price above 50-day EMA",
    "TREND_MA",
    "Hold while the close is above its 50-day exponential moving average.",
    KAUFMAN13,
    {"ma": "ema", "n": 50},
    close_above_ma,
)
add(
    "ema_200_trend",
    "Price above 200-day EMA",
    "TREND_MA",
    "Hold while the close is above its 200-day exponential moving average.",
    KAUFMAN13,
    {"ma": "ema", "n": 200},
    close_above_ma,
)
add(
    "golden_cross_50_200",
    "Golden cross 50/200",
    "TREND_MA",
    "Hold while the 50-day average is above the 200-day average.",
    KAUFMAN13,
    {"ma": "sma", "fast": 50, "slow": 200},
    ma_over_ma,
)
add(
    "sma_cross_20_50",
    "20/50-day average cross",
    "TREND_MA",
    "Hold while the 20-day average is above the 50-day average.",
    KAUFMAN13,
    {"ma": "sma", "fast": 20, "slow": 50},
    ma_over_ma,
)
add(
    "ema_cross_12_26",
    "12/26-day EMA cross",
    "TREND_MA",
    "Hold while the 12-day EMA is above the 26-day EMA.",
    KAUFMAN13,
    {"ma": "ema", "fast": 12, "slow": 26},
    ma_over_ma,
)
add(
    "triple_ma_10_20_50",
    "Triple moving average 10/20/50",
    "TREND_MA",
    "Hold while the 10-day average is above the 20-day, and the 20-day above the 50-day.",
    KAUFMAN13,
    {"a": 10, "b": 20, "c": 50},
    triple_ma,
)
add(
    "faber_10_month",
    "Faber 10-month average (monthly)",
    "TREND_MA",
    "At each month end, hold for the next month if the month-end close is above the average of the "
    "last 10 month-end closes.",
    FABER,
    {"months": 10},
    faber_monthly,
)
add(
    "hull_ma_55",
    "Hull moving average 55 rising",
    "TREND_MA",
    "Hold while the 55-day Hull moving average is rising.",
    "Hull (2005), Hull Moving Average, alanhull.com",
    {"n": 55},
    hma_rising,
    note="Practitioner indicator; no peer-reviewed test.",
)
add(
    "kama_10_2_30",
    "Kaufman adaptive average",
    "TREND_MA",
    "Hold while the close is above Kaufman's adaptive moving average (10, 2, 30).",
    "Kaufman (1995), Smarter Trading, McGraw-Hill",
    {"n": 10, "fast": 2, "slow": 30},
    close_above_kama,
)
add(
    "tema_50",
    "Price above 50-day TEMA",
    "TREND_MA",
    "Hold while the close is above the 50-day triple-exponential moving average.",
    "Mulloy (1994), 'Smoothing Data with Faster Moving Averages', Technical Analysis of Stocks & "
    "Commodities 12(1)",
    {"ma": "tema", "n": 50},
    close_above_ma,
)
add(
    "linreg_slope_50",
    "50-day regression slope positive",
    "TREND_MA",
    "Hold while the least-squares slope of the last 50 closes is positive.",
    KAUFMAN13,
    {"n": 50},
    slope_positive,
)
add(
    "ma_envelope_breakout",
    "Moving-average envelope breakout",
    "TREND_MA",
    "Buy when the close rises 2% above its 20-day average; sell when it closes below the average.",
    KAUFMAN13,
    {"n": 20, "pct": 0.02},
    envelope_breakout,
)
add(
    "sma200_rising",
    "Above a rising 200-day average",
    "TREND_MA",
    "Hold while the close is above the 200-day average and that average is higher than 20 days "
    "ago.",
    KAUFMAN13,
    {"n": 200, "lookback": 20},
    ma_and_rising,
)

# Trend: breakouts and channels
add(
    "turtle_20_10",
    "Turtle system 1 (20/10 breakout)",
    "BREAKOUT",
    "Buy on a close above the prior 20-day high; sell on a close below the prior 10-day low.",
    TURTLE,
    {"entry": 20, "exit": 10},
    donchian,
    note=CLOSE_CONFIRMED,
)
add(
    "turtle_55_20",
    "Turtle system 2 (55/20 breakout)",
    "BREAKOUT",
    "Buy on a close above the prior 55-day high; sell on a close below the prior 20-day low.",
    TURTLE,
    {"entry": 55, "exit": 20},
    donchian,
    note=CLOSE_CONFIRMED,
)
add(
    "donchian_100_50",
    "Donchian 100/50 breakout",
    "BREAKOUT",
    "Buy on a close above the prior 100-day high; sell below the prior 50-day low.",
    TURTLE,
    {"entry": 100, "exit": 50},
    donchian,
)
add(
    "donchian_200_100",
    "Donchian 200/100 breakout",
    "BREAKOUT",
    "Buy on a close above the prior 200-day high; sell below the prior 100-day low.",
    TURTLE,
    {"entry": 200, "exit": 100},
    donchian,
)
add(
    "bll_trb_50",
    "Trading-range breakout 50 (BLL)",
    "BREAKOUT",
    "Buy on a close above the highest close of the prior 50 days; hold 10 days.",
    BLL,
    {"n": 50, "hold": 10},
    trading_range_breakout,
)
add(
    "bll_trb_200",
    "Trading-range breakout 200 (BLL)",
    "BREAKOUT",
    "Buy on a close above the highest close of the prior 200 days; hold 10 days.",
    BLL,
    {"n": 200, "hold": 10},
    trading_range_breakout,
)
add(
    "keltner_breakout",
    "Keltner channel breakout",
    "BREAKOUT",
    "Buy on a close above EMA(20) + 2×ATR(10); sell on a close below EMA(20).",
    "Keltner (1960), How to Make Money in Commodities (modern ATR form)",
    {"n": 20, "k": 2.0, "atr_n": 10},
    keltner_breakout,
)
add(
    "bollinger_breakout",
    "Bollinger band breakout",
    "BREAKOUT",
    "Buy on a close above the upper Bollinger band (20, 2); sell on a close below the middle band.",
    BOLL,
    {"n": 20, "k": 2.0},
    bollinger_breakout,
)
add(
    "atr_channel_breakout",
    "ATR channel breakout",
    "BREAKOUT",
    "Buy on a close above SMA(50) + 2×ATR(20); sell on a close below SMA(50).",
    KAUFMAN13,
    {"n": 50, "atr_n": 20, "k": 2.0},
    atr_channel,
)
add(
    "williams_volatility_breakout",
    "Volatility breakout (Williams)",
    "BREAKOUT",
    "Buy when the close exceeds the open plus half of the previous day's range; hold 5 days.",
    "Williams (1999), Long-Term Secrets to Short-Term Trading, Wiley",
    {"k": 0.5, "hold": 5},
    volatility_breakout,
    note=CLOSE_CONFIRMED,
)
add(
    "nr7_breakout",
    "NR7 breakout (Crabel)",
    "BREAKOUT",
    "After the narrowest-range day of the last 7, buy on a close above that day's high; hold 5 "
    "days.",
    "Crabel (1990), Day Trading with Short Term Price Patterns and Opening Range Breakout",
    {"n": 7, "hold": 5},
    nr7_breakout,
    note=CLOSE_CONFIRMED,
)
add(
    "near_52_week_high",
    "Near the 52-week high",
    "BREAKOUT",
    "Hold while the close is within 5% of its 365-day high; sell when it falls 15% below it.",
    "George & Hwang (2004), 'The 52-Week High and Momentum Investing', Journal of Finance 59(5)",
    {"n": 365, "enter": 0.95, "exit": 0.85},
    near_high,
)
add(
    "new_high_trailing_20",
    "New 365-day high, 20% trailing stop",
    "BREAKOUT",
    "Buy on a close above the prior 365-day high; sell 20% below the highest close since entry.",
    "George & Hwang (2004), 'The 52-Week High and Momentum Investing', Journal of Finance 59(5)",
    {"n": 365, "trail": 0.20},
    new_high_trailing,
)
add(
    "darvas_box",
    "Darvas box (simplified)",
    "BREAKOUT",
    "Buy on a close above a 20-day box top that held for 3 days; sell below the box floor.",
    "Darvas (1960), How I Made $2,000,000 in the Stock Market",
    {"n": 20, "confirm": 3},
    darvas_box,
    note="Simplified mechanical reading of a discretionary method.",
)
add(
    "supertrend_10_3",
    "SuperTrend (10, 3)",
    "BREAKOUT",
    "Hold while SuperTrend (ATR 10, multiplier 3) is in its up state.",
    "Seban (2000s), SuperTrend indicator",
    {"n": 10, "k": 3.0},
    supertrend_rule,
    note="Practitioner indicator; no peer-reviewed test.",
)
add(
    "chandelier_22_3",
    "22-day breakout with chandelier exit",
    "BREAKOUT",
    "Buy on a close above the prior 22-day high; sell below the 22-day high minus 3×ATR(22).",
    "LeBeau & Lucas (1992), Technical Traders Guide to Computer Analysis of the Futures Market",
    {"n": 22, "k": 3.0},
    chandelier,
)
add(
    "parabolic_sar",
    "Parabolic SAR",
    "BREAKOUT",
    "Hold while the parabolic stop-and-reverse is below price (0.02 step, 0.2 cap).",
    WILDER,
    {"step": 0.02, "cap": 0.2},
    psar_rule,
)
add(
    "ichimoku_cloud",
    "Ichimoku cloud",
    "BREAKOUT",
    "Buy when price is above the cloud and tenkan is above kijun; sell when price drops into the "
    "cloud "
    "or tenkan falls below kijun.",
    "Elliott (2007), Ichimoku Charts, Harriman House (Hosoda's system)",
    {"tenkan": 9, "kijun": 26, "senkou_b": 52},
    ichimoku_rule,
)
add(
    "adx_di_25",
    "ADX trend with directional lines",
    "BREAKOUT",
    "Buy when ADX(14) is above 25 and +DI is above −DI; sell when +DI falls below −DI.",
    WILDER,
    {"n": 14, "threshold": 25},
    adx_rule,
)
add(
    "aroon_25",
    "Aroon trend (25)",
    "BREAKOUT",
    "Buy when Aroon Up is above 70 and above Aroon Down; sell when it drops below Aroon Down.",
    "Chande (1995), 'Aroon', Technical Analysis of Stocks & Commodities",
    {"n": 25, "threshold": 70},
    aroon_rule,
)
add(
    "vortex_14",
    "Vortex indicator (14)",
    "BREAKOUT",
    "Hold while VI+ is above VI−.",
    "Botes & Siepman (2010), 'The Vortex Indicator', Technical Analysis of "
    "Stocks & Commodities 28(1)",
    {"n": 14},
    vortex_rule,
)
add(
    "heikin_ashi_trend",
    "Heikin-Ashi trend",
    "BREAKOUT",
    "Buy after two bullish Heikin-Ashi candles; sell after two bearish ones.",
    "Valcu (2004), 'Using the Heikin-Ashi Technique', Technical Analysis of Stocks & Commodities "
    "22(2)",
    {"confirm": 2},
    heikin_ashi_rule,
)

# Momentum
add(
    "tsmom_12m",
    "12-month time-series momentum",
    "MOMENTUM",
    "At each month end, hold for the next month if the price is above its level 365 days earlier.",
    MOP,
    {"n": 365, "rebalance": "monthly"},
    tsmom,
)
add(
    "tsmom_6m",
    "6-month time-series momentum",
    "MOMENTUM",
    "At each month end, hold if the 182-day return is positive.",
    MOP,
    {"n": 182, "rebalance": "monthly"},
    tsmom,
)
add(
    "tsmom_3m",
    "3-month time-series momentum",
    "MOMENTUM",
    "At each month end, hold if the 91-day return is positive.",
    MOP,
    {"n": 91, "rebalance": "monthly"},
    tsmom,
)
add(
    "tsmom_1m_daily",
    "1-month momentum (daily)",
    "MOMENTUM",
    "Hold while the 30-day return is positive.",
    MOP,
    {"n": 30},
    tsmom,
)
add(
    "crypto_tsmom_1w",
    "1-week momentum (crypto study)",
    "MOMENTUM",
    "Hold while the 7-day return is positive.",
    LT,
    {"n": 7},
    tsmom,
)
add(
    "crypto_tsmom_4w",
    "4-week momentum (crypto study)",
    "MOMENTUM",
    "Hold while the 28-day return is positive.",
    LT,
    {"n": 28},
    tsmom,
)
add(
    "momentum_12_1",
    "12-1 momentum (skip last month)",
    "MOMENTUM",
    "At each month end, hold if the return from 365 to 30 days ago is positive.",
    "Jegadeesh & Titman (1993), 'Returns to Buying Winners and Selling Losers', Journal of Finance "
    "48(1)",
    {"n": 365, "skip": 30, "rebalance": "monthly"},
    tsmom,
)
add(
    "trend_vote_1_3_12",
    "Trend vote 1/3/12 months",
    "MOMENTUM",
    "Hold a third of the position for each of the 30-, 91- and 365-day returns that is positive.",
    HOP,
    {"horizons": (30, 91, 365)},
    trend_vote,
)
add(
    "roc_20_with_trend",
    "20-day rate of change with 200-day filter",
    "MOMENTUM",
    "Hold while the 20-day return is positive and the close is above its 200-day average.",
    ACHELIS,
    {"n": 20, "trend_n": 200},
    roc_rule,
)
add(
    "macd_signal",
    "MACD over signal line",
    "MOMENTUM",
    "Hold while MACD(12, 26) is above its 9-day signal line.",
    "Appel (1979), The Moving Average Convergence-Divergence Trading Method",
    {"fast": 12, "slow": 26, "signal": 9, "mode": "signal"},
    macd_rule,
)
add(
    "macd_histogram",
    "MACD and histogram positive",
    "MOMENTUM",
    "Hold while both MACD and its histogram are above zero.",
    "Appel (1979), The Moving Average Convergence-Divergence Trading Method",
    {"fast": 12, "slow": 26, "signal": 9, "mode": "hist"},
    macd_rule,
)
add(
    "trix_15",
    "TRIX above zero",
    "MOMENTUM",
    "Hold while the 15-day TRIX is positive.",
    "Hutson (1983), 'TRIX', Technical Analysis of Stocks & Commodities",
    {"n": 15},
    trix_rule,
)
add(
    "kst_signal",
    "Know Sure Thing over signal",
    "MOMENTUM",
    "Hold while Pring's KST (daily form) is above its 9-day signal line.",
    "Pring (2002), Technical Analysis Explained, 4th ed., McGraw-Hill",
    {},
    kst_rule,
)
add(
    "coppock_monthly",
    "Coppock curve (monthly)",
    "MOMENTUM",
    "At month ends, buy when the Coppock curve is below zero and turns up; sell when it is above "
    "zero "
    "and turns down.",
    "Coppock (1962), Barron's",
    {"r1": 14, "r2": 11, "n": 10},
    coppock_rule,
)
add(
    "elder_impulse",
    "Elder impulse system",
    "MOMENTUM",
    "Buy when the 13-day EMA and the MACD histogram both rise; sell when both fall.",
    "Elder (2002), Come Into My Trading Room, Wiley",
    {"ema": 13},
    elder_impulse,
)
add(
    "momentum_low_vol",
    "Momentum in calm markets",
    "MOMENTUM",
    "Hold while the 91-day return is positive and 30-day volatility is below 365-day volatility.",
    "Moreira & Muir (2017), 'Volatility-Managed Portfolios', Journal of Finance 72(4); " + MOP,
    {"n": 91, "short": 30, "long": 365},
    momentum_low_vol,
)
add(
    "rsi_momentum_55_45",
    "RSI momentum (55/45)",
    "MOMENTUM",
    "Buy when RSI(14) rises above 55; sell when it falls below 45.",
    WILDER,
    {"n": 14, "enter": 55, "exit": 45},
    rsi_momentum,
)
add(
    "cci_trend_100",
    "CCI trend (+100)",
    "MOMENTUM",
    "Buy when CCI(20) rises above +100; sell when it falls below zero.",
    "Lambert (1980), 'Commodity Channel Index: Tool for Trading Cyclic Trends', Commodities",
    {"n": 20, "enter": 100, "exit": 0},
    cci_trend,
)

# Mean reversion
add(
    "connors_rsi2",
    "Connors RSI(2) pullback",
    "MEAN_REVERSION",
    "Above the 200-day average, buy when RSI(2) is below 10; sell on a close above the 5-day "
    "average.",
    CA,
    {"below": 10, "exit_ma": 5},
    connors_rsi2,
)
add(
    "connors_cumulative_rsi",
    "Connors cumulative RSI",
    "MEAN_REVERSION",
    "Above the 200-day average, buy when the 2-day sum of RSI(2) is below 35; sell when RSI(2) > "
    "65.",
    CA,
    {"below": 35, "exit_rsi": 65},
    connors_cumulative,
)
add(
    "connors_double_7",
    "Connors double 7s",
    "MEAN_REVERSION",
    "Above the 200-day average, buy at a 7-day closing low; sell at a 7-day closing high.",
    CA,
    {"n": 7},
    double_sevens,
)
add(
    "connors_multiple_days_down",
    "Connors multiple days down",
    "MEAN_REVERSION",
    "Above the 200-day average and below the 5-day average, buy after 4 down days out of 5; sell "
    "above "
    "the 5-day average.",
    CA,
    {"of5": 4},
    multiple_days_down,
)
add(
    "connors_rsi_composite",
    "ConnorsRSI below 15",
    "MEAN_REVERSION",
    "Above the 200-day average, buy when ConnorsRSI(3, 2, 100) is below 15; sell when it exceeds "
    "70.",
    "Connors, Alvarez & Radtke (2012), An Introduction to ConnorsRSI, Connors Research",
    {"below": 15, "exit": 70},
    connors_rsi_rule,
)
add(
    "rsi14_30_50",
    "RSI(14) oversold",
    "MEAN_REVERSION",
    "Buy when RSI(14) is below 30; sell when it rises above 50.",
    WILDER,
    {"n": 14, "below": 30, "exit": 50},
    rsi_reversion,
)
add(
    "rsi14_trend_30_70",
    "RSI(14) oversold in an uptrend",
    "MEAN_REVERSION",
    "Above the 200-day average, buy when RSI(14) is below 30; sell when it rises above 70.",
    WILDER,
    {"n": 14, "below": 30, "exit": 70, "trend": True},
    rsi_reversion,
)
add(
    "bollinger_reversion",
    "Bollinger band reversion",
    "MEAN_REVERSION",
    "Buy on a close below the lower Bollinger band (20, 2); sell on a close above the middle band.",
    BOLL,
    {"n": 20, "k": 2.0},
    bollinger_reversion,
)
add(
    "zscore_reversion_20",
    "Z-score reversion",
    "MEAN_REVERSION",
    "Buy when the close is 2 standard deviations below its 20-day mean; sell when it is back above "
    "the mean.",
    KAUFMAN13,
    {"n": 20, "k": 2.0},
    zscore_reversion,
)
add(
    "williams_r_reversion",
    "Williams %R oversold",
    "MEAN_REVERSION",
    "Above the 200-day average, buy when %R(14) is below −90; sell when it rises above −20.",
    "Williams (1973), How I Made One Million Dollars Last Year Trading Commodities",
    {"n": 14, "below": -90, "exit": -20},
    williams_r_rule,
)
add(
    "stochastic_oversold_cross",
    "Stochastic oversold cross",
    "MEAN_REVERSION",
    "Buy when %K(14) crosses above %D(3) below 20; sell when %K exceeds 80.",
    "Lane (1984), 'Lane's Stochastics', Technical Analysis of Stocks & Commodities 2(3)",
    {"n": 14, "d": 3, "below": 20, "exit": 80},
    stochastic_cross,
)
add(
    "ibs_reversion",
    "Internal bar strength",
    "MEAN_REVERSION",
    "Buy when the close is in the bottom 20% of the day's range; sell when it closes in the top "
    "20%.",
    "Pagonidis (2014), 'The IBS Effect: Mean Reversion in Equity ETFs', NAAIM",
    {"below": 0.2, "exit": 0.8},
    ibs_rule,
)
add(
    "mfi_oversold",
    "Money flow index oversold",
    "MEAN_REVERSION",
    "Buy when MFI(14) is below 20; sell when it rises above 50.",
    "Quong & Soudack (1989), 'Volume-Weighted RSI: Money Flow', Technical Analysis of Stocks & "
    "Commodities",
    {"n": 14, "below": 20, "exit": 50},
    mfi_rule,
)
add(
    "deviation_8pct",
    "8% below the 20-day average",
    "MEAN_REVERSION",
    "Buy when the close is 8% below its 20-day average; sell when it is back above the average.",
    KAUFMAN13,
    {"n": 20, "pct": 0.08},
    deviation_reversion,
)
add(
    "shock_rebound",
    "Rebound after a 2.5σ drop",
    "MEAN_REVERSION",
    "Buy after a daily fall larger than 2.5 times the prior 20-day volatility; hold 5 days.",
    "Jegadeesh (1990), 'Evidence of Predictable Behavior of Security Returns', Journal of Finance "
    "45(3)",
    {"n": 20, "k": 2.5, "hold": 5},
    shock_rebound,
)
add(
    "pullback_in_uptrend",
    "Pullback in an uptrend",
    "MEAN_REVERSION",
    "Above the 200-day average, buy on a close below the 10-day average; sell on a close back "
    "above it.",
    CA,
    {"fast": 10, "trend": 200},
    pullback_in_uptrend,
)

# Volatility
add(
    "vol_target_weekly",
    "Volatility-scaled holding",
    "VOLATILITY",
    "Hold min(1, typical volatility ÷ current 30-day volatility) of the position, rebalanced "
    "weekly; "
    "'typical' is the median of the past year.",
    "Moreira & Muir (2017), 'Volatility-Managed Portfolios', Journal of Finance 72(4)",
    {"n": 30, "lookback": 365},
    vol_target,
)
add(
    "vol_contraction_trend",
    "Calm uptrend",
    "VOLATILITY",
    "Hold while 20-day volatility is below 100-day volatility and the close is above its 50-day "
    "average.",
    KAUFMAN13,
    {"short": 20, "long": 100, "trend": 50},
    vol_contraction,
)
add(
    "bollinger_squeeze",
    "Bollinger squeeze breakout",
    "VOLATILITY",
    "After band width hits a 180-day low (within 5 days), buy on a close above the upper band; "
    "sell "
    "below the middle band.",
    BOLL,
    {"n": 20, "k": 2.0, "lookback": 180, "recent": 5},
    squeeze,
)
add(
    "range_expansion_up",
    "Wide-range up day",
    "VOLATILITY",
    "Buy after an up day whose range exceeds twice ATR(20); hold 10 days or until a close below "
    "the "
    "20-day average.",
    "Williams (1999), Long-Term Secrets to Short-Term Trading, Wiley",
    {"n": 20, "k": 2.0, "hold": 10, "exit_ma": 20},
    range_expansion,
)
add(
    "choppiness_trend",
    "Trending (low choppiness) uptrend",
    "VOLATILITY",
    "Hold while the choppiness index(14) is below 38.2 and the close is above its 50-day average.",
    "Dreiss (1990s), Choppiness Index",
    {"n": 14, "below": 38.2, "trend": 50},
    choppiness_rule,
    note="Practitioner indicator; no peer-reviewed test.",
)
add(
    "efficiency_trend",
    "Efficient trend (Kaufman ER)",
    "VOLATILITY",
    "Buy when the 20-day efficiency ratio exceeds 0.3 and the 20-day return is positive; sell when "
    "the "
    "ratio drops below 0.15 or the return turns negative.",
    "Kaufman (1995), Smarter Trading, McGraw-Hill",
    {"n": 20, "enter": 0.3, "exit": 0.15},
    efficiency_rule,
)

# Volume
add(
    "obv_trend",
    "On-balance volume trend",
    "VOLUME",
    "Hold while OBV is above its 20-day average and the close is above its 50-day average.",
    "Granville (1963), Granville's New Key to Stock Market Profits",
    {"n": 20, "trend": 50},
    obv_rule,
)
add(
    "chaikin_money_flow",
    "Chaikin money flow",
    "VOLUME",
    "Buy when CMF(20) exceeds +0.05; sell when it falls below −0.05.",
    ACHELIS,
    {"n": 20, "enter": 0.05, "exit": -0.05},
    cmf_rule,
)
add(
    "volume_breakout",
    "Breakout on high volume",
    "VOLUME",
    "Buy on a close above the prior 20-day high on volume 1.5× its 20-day average; sell below the "
    "prior "
    "10-day low.",
    KAUFMAN13,
    {"n": 20, "k": 1.5, "exit": 10},
    volume_breakout,
)
add(
    "ad_line_trend",
    "Accumulation/distribution trend",
    "VOLUME",
    "Hold while the A/D line and the close are both above their 50-day averages.",
    ACHELIS,
    {"n": 50},
    ad_rule,
)

# Calendar
add(
    "turn_of_month",
    "Turn of the month",
    "CALENDAR",
    "Hold over the last 3 and first 3 days of each month.",
    "Lakonishok & Smidt (1988), 'Are Seasonal Anomalies Real? A Ninety-Year Perspective', Review "
    "of "
    "Financial Studies 1(4)",
    {"first": 3, "last": 3},
    turn_of_month,
)
add(
    "halloween",
    "Halloween (November to April)",
    "CALENDAR",
    "Hold from November to April; stay in cash from May to October.",
    "Bouman & Jacobsen (2002), 'The Halloween Indicator', American Economic Review 92(5)",
    {"months": (11, 12, 1, 2, 3, 4)},
    months_in,
)
add(
    "gold_autumn",
    "Autumn effect (September and November)",
    "CALENDAR",
    "Hold only in September and November.",
    "Baur (2013), 'The Autumn Effect of Gold', Research in International Business and Finance "
    "27(1)",
    {"months": (9, 11)},
    months_in,
    note="Documented for gold; tested on every asset alike.",
)
add(
    "crypto_monday",
    "Monday only (crypto day-of-week)",
    "CALENDAR",
    "Hold only on Mondays (UTC).",
    "Caporale & Plastun (2019), 'The Day of the Week Effect in the Cryptocurrency Market', Finance "
    "Research Letters 31",
    {"weekdays": (0,)},
    weekday_only,
)
add(
    "halving_18m",
    "18 months after each bitcoin halving",
    "CALENDAR",
    "Hold for 540 days from each bitcoin block-reward halving; otherwise cash.",
    "Practitioner narrative (no peer-reviewed source)",
    {"days": 540},
    halving_cycle,
    note=(
        "Only 3–4 halvings exist: far too few events to test. Included because it is widely cited."
    ),
)

# Composite
add(
    "trend_ensemble_6",
    "Trend ensemble (6 rules)",
    "COMPOSITE",
    "Hold one sixth of the position for each of six trend rules that is long (50/200-day averages, "
    "55/20 breakout, 3/6/12-month momentum).",
    HOP,
    {},
    trend_ensemble,
)
add(
    "trend_vol_scaled",
    "12-month trend, volatility-scaled",
    "COMPOSITE",
    "Hold when 12-month momentum is positive, sized by the volatility-scaling rule.",
    MOP + "; Moreira & Muir (2017), Journal of Finance 72(4)",
    {},
    trend_vol_scaled,
)
add(
    "reversion_ensemble",
    "Mean-reversion ensemble in an uptrend",
    "COMPOSITE",
    "Above the 200-day average, hold one third for each of RSI(2), Bollinger and IBS reversion "
    "that is long.",
    CA + "; " + BOLL,
    {},
    reversion_ensemble,
)
add(
    "regime_switch",
    "Trend above 200 days, reversion below",
    "COMPOSITE",
    "Above the 200-day average follow the 20/50 cross; below it, only buy RSI(2) < 5 pullbacks.",
    KAUFMAN13,
    {},
    regime_switch,
)

# Two-asset rotation: BTC and gold
add(
    "rotation_dual_momentum_12m",
    "BTC/gold dual momentum (12 months)",
    "ROTATION",
    "At each month end, hold whichever of BTC and gold rose more over 365 days, if it rose; else "
    "cash.",
    "Antonacci (2014), Dual Momentum Investing, McGraw-Hill",
    {"n": 365},
    panel_fn=dual_momentum,
)
add(
    "rotation_dual_momentum_3m",
    "BTC/gold dual momentum (3 months)",
    "ROTATION",
    "At each month end, hold whichever of BTC and gold rose more over 91 days, if it rose; else "
    "cash.",
    "Antonacci (2014), Dual Momentum Investing, McGraw-Hill",
    {"n": 91},
    panel_fn=dual_momentum,
)
add(
    "rotation_both_trend_200",
    "BTC and gold each above 200 days",
    "ROTATION",
    "At each month end, hold half in each asset that closes above its 200-day average.",
    FABER,
    {"n": 200},
    panel_fn=both_trend,
)
add(
    "rotation_inverse_vol",
    "BTC/gold inverse-volatility mix",
    "ROTATION",
    "Always invested: weights inversely proportional to 30-day volatility, set at each month end.",
    "Baur & Lucey (2010), 'Is Gold a Hedge or a Safe Haven?', Financial Review 45(2) "
    "(diversification "
    "reference)",
    {"n": 30},
    panel_fn=inverse_vol,
)
add(
    "rotation_ratio_reversion",
    "BTC/gold ratio reversion",
    "ROTATION",
    "Hold BTC when the BTC/gold price ratio is 1σ below its 90-day mean, gold when 1σ above, else "
    "half each.",
    "Practitioner relative-value rule (no peer-reviewed source)",
    {"n": 90, "k": 1.0},
    panel_fn=ratio_reversion,
)

SPECS: tuple[Spec, ...] = tuple(S)
BY_KEY = {s.key: s for s in SPECS}


def buy_and_hold(m: Market, _p: dict[str, Any]) -> Weights:
    return [1.0] * len(m)
