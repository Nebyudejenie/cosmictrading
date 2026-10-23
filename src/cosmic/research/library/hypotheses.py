"""Ideas from this system's own research, registered 2026-10-23 (ADR-031 addendum).

The Gold page's research (ADR-032) showed patterns in the past year: how the auto-trader's
rule did by the daily trend and by the trading session. A rule built on a pattern found in
the past cannot be judged on that same past, so each idea here is judged on the FORWARD test
only: prices after its registration. Its earlier results are shown, marked as seen before it
was registered, and never count.

Each idea is a published rule with a filter on its entries. The rule's exits are untouched,
and an entry still needs the rule's fresh flat → long change, exactly as the auto-trader
trades. All filters are causal: they use the bar of the decision and the bars before it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from cosmic.market.indicators import sma
from cosmic.research.library.catalog import BY_KEY, Spec, Weights
from cosmic.research.library.engine import Market

REGISTERED = "2026-10-23"
SOURCE = f"This system's research of the past year, registered {REGISTERED} (not published)"
TREND_DAYS = 50
BUSY_HOURS = (7, 21)  # London and New York, UTC
Filter = Callable[[Market], list[bool]]


def _hourly(m: Market) -> bool:
    d0, d1 = m.dates[0], m.dates[1]
    return isinstance(d0, datetime) and isinstance(d1, datetime) and (d1 - d0).days < 1


def above_trend(m: Market) -> list[bool]:
    """The close is above its 50-day average (1,200 hours on hourly bars)."""
    n = TREND_DAYS * 24 if _hourly(m) else TREND_DAYS
    avg = sma(m.close, n) if len(m) >= n else [None] * len(m)
    return [a is not None and c > a for c, a in zip(m.close, avg, strict=True)]


def busy_hours(m: Market) -> list[bool]:
    """The decision bar opened in London or New York hours (hourly bars only)."""
    if not _hourly(m):
        raise ValueError("an idea for hourly bars")
    start, end = BUSY_HOURS
    return [isinstance(d, datetime) and start <= d.hour < end for d in m.dates]


def both(m: Market) -> list[bool]:
    return [a and b for a, b in zip(above_trend(m), busy_hours(m), strict=True)]


def filtered(base: Spec, allow: Filter) -> Callable[[Market, dict[str, Any]], Weights]:
    """The base rule, entering only on a fresh flat → long change that ``allow`` permits."""
    assert base.fn is not None
    base_fn = base.fn

    def fn(m: Market, p: dict[str, Any]) -> Weights:
        w = base_fn(m, dict(p))
        ok = allow(m)
        out: Weights = []
        pos = 0.0
        for t, x in enumerate(w):
            if pos == 0.0:
                fresh = x > 0 and (t == 0 or w[t - 1] == 0)
                pos = 1.0 if fresh and ok[t] else 0.0
            else:
                pos = 1.0 if x > 0 else 0.0
            out.append(pos)
        return out

    return fn


def idea(base_key: str, key: str, label: str, allow: Filter, *, hourly: bool = False) -> Spec:
    """A published rule with an entry filter; ``hourly`` ideas are not run on daily bars."""
    base = BY_KEY[base_key]
    return replace(
        base,
        number=0,
        key=key,
        name=f"{base.name} {label}",
        family="IDEA",
        rule=f"{base.rule} Entries only {label}.",
        source=SOURCE,
        fn=filtered(base, allow),
        note=f"Judged forward only, from {REGISTERED}.",
        tags=("idea", "hourly") if hourly else ("idea",),
    )


IDEAS: tuple[Spec, ...] = (
    idea("turtle_20_10", "idea_turtle_trend", "with the daily trend up", above_trend),
    idea(
        "turtle_20_10",
        "idea_turtle_busy",
        "in London and New York hours",
        busy_hours,
        hourly=True,
    ),
    idea(
        "turtle_20_10",
        "idea_turtle_trend_busy",
        "with the daily trend up, in London and New York hours",
        both,
        hourly=True,
    ),
    idea("keltner_breakout", "idea_keltner_trend", "with the daily trend up", above_trend),
)
