"""Server-side SVG charts (no client JavaScript; CSP-safe).

Only numbers and escaped text are interpolated into the markup. Colours come from the
stylesheet by class: marks wear the series colour, text wears the text colours. Hover
tooltips are SVG ``<title>`` elements, and every value shown in a tooltip is also on the
page in words or a table.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from markupsafe import Markup, escape

AXIS = 'class="axis"'
TEXT = 'class="axis-text" font-size="11"'


def _num(v: float) -> str:
    """An axis value: whole numbers from 1,000 up, cents below."""
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"


def line_chart(
    times: Sequence[datetime],
    values: Sequence[float],
    *,
    width: int = 720,
    height: int = 220,
    label: str = "",
    empty: str = "Not enough completed bars to draw a chart.",
    css_class: str = "chart",
    baseline: float | None = None,
    end_label: bool = False,
    time_format: str = "%m-%d %H:%M UTC",
) -> Markup:
    """One series over time. ``baseline`` draws a hairline at that value (0 for a result);
    ``end_label`` marks the last point and writes its value."""
    if len(values) < 2:
        return Markup('<p class="muted">{}</p>').format(empty)
    pad_l, pad_r, pad_t, pad_b = 64, 12 + (70 if end_label else 0), 12, 28
    lo, hi = min(values), max(values)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    span = (hi - lo) or 1.0
    w, h = width - pad_l - pad_r, height - pad_t - pad_b

    def y(v: float) -> float:
        return pad_t + h * (1 - (v - lo) / span)

    xy = [(pad_l + w * i / (len(values) - 1), y(v)) for i, v in enumerate(values)]
    pts = " ".join(f"{px:.1f},{py:.1f}" for px, py in xy)
    first, last = times[0].strftime(time_format), times[-1].strftime(time_format)
    title = escape(label)
    bottom = pad_t + h
    parts = [
        f'<svg class="{escape(css_class)}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{title}">',
        f"<title>{title}</title>",
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{bottom}" {AXIS}/>',
        f'<line x1="{pad_l}" y1="{bottom}" x2="{pad_l + w}" y2="{bottom}" {AXIS}/>',
        f'<text x="{pad_l - 6}" y="{pad_t + 10}" text-anchor="end" {TEXT}>{hi:,.2f}</text>',
        f'<text x="{pad_l - 6}" y="{bottom}" text-anchor="end" {TEXT}>{lo:,.2f}</text>',
        f'<text x="{pad_l}" y="{height - 8}" {TEXT}>{first}</text>',
        f'<text x="{pad_l + w}" y="{height - 8}" text-anchor="end" {TEXT}>{last}</text>',
    ]
    if baseline is not None and lo < baseline < hi:
        yb = y(baseline)
        parts.append(f'<line x1="{pad_l}" y1="{yb:.1f}" x2="{pad_l + w}" y2="{yb:.1f}" {AXIS}/>')
        if pad_t + 16 < yb < bottom - 12:
            parts.append(
                f'<text x="{pad_l - 6}" y="{yb + 4:.1f}" text-anchor="end" {TEXT}>'
                f"{_num(baseline)}</text>"
            )
    parts.append(f'<polyline class="series" points="{pts}"/>')
    if end_label:
        ex, ey = xy[-1]
        parts.append(f'<circle class="end-dot" cx="{ex:.1f}" cy="{ey:.1f}" r="4"/>')
        parts.append(
            f'<text class="end-label" x="{ex + 8:.1f}" y="{ey + 4:.1f}" font-size="12">'
            f"{values[-1]:+,.2f}</text>"
        )
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 - numbers + escaped title  # nosec B704


def _bar_path(x: float, base: float, end: float, width: float, r: float = 4.0) -> str:
    """A bar from ``base`` to ``end`` (y): square at the baseline, rounded at the data end."""
    r = min(r, width / 2, abs(end - base))
    right = x + width
    if end <= base:  # grows up
        return (
            f"M{x:.1f},{base:.1f}V{end + r:.1f}A{r:.1f},{r:.1f} 0 0 1 {x + r:.1f},{end:.1f}"
            f"H{right - r:.1f}A{r:.1f},{r:.1f} 0 0 1 {right:.1f},{end + r:.1f}V{base:.1f}Z"
        )
    return (
        f"M{x:.1f},{base:.1f}V{end - r:.1f}A{r:.1f},{r:.1f} 0 0 0 {x + r:.1f},{end:.1f}"
        f"H{right - r:.1f}A{r:.1f},{r:.1f} 0 0 0 {right:.1f},{end - r:.1f}V{base:.1f}Z"
    )


def bar_chart(
    values: Sequence[float],
    labels: Sequence[str],
    *,
    width: int = 720,
    height: int = 200,
    label: str = "",
    empty: str = "No closed trades yet.",
) -> Markup:
    """One bar per value from a zero line: wins up (class ``bar-win``), losses down
    (``bar-loss``). Each bar carries its label and value as a tooltip."""
    if not values:
        return Markup('<p class="muted">{}</p>').format(empty)
    pad_l, pad_r, pad_t, pad_b = 64, 12, 12, 16
    hi, lo = max(max(values), 0.0), min(min(values), 0.0)
    span = (hi - lo) or 1.0
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    zero = pad_t + h * hi / span
    step = w / len(values)
    bar = max(2.0, min(24.0, step * 0.7))
    title = escape(label)
    parts = [
        f'<svg class="chart chart-bars" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{title}">',
        f"<title>{title}</title>",
        f'<line x1="{pad_l}" y1="{zero:.1f}" x2="{pad_l + w}" y2="{zero:.1f}" {AXIS}/>',
        f'<text x="{pad_l - 6}" y="{pad_t + 10}" text-anchor="end" {TEXT}>{hi:+,.2f}</text>',
        f'<text x="{pad_l - 6}" y="{pad_t + h}" text-anchor="end" {TEXT}>{lo:+,.2f}</text>',
    ]
    for i, (v, name) in enumerate(zip(values, labels, strict=True)):
        x = pad_l + step * i + (step - bar) / 2
        size = max(1.0, h * abs(v) / span)
        end = zero - size if v >= 0 else zero + size
        cls = "bar-win" if v > 0 else ("bar-loss" if v < 0 else "bar-even")
        parts.append(
            f'<path class="{cls}" d="{_bar_path(x, zero, end, bar)}">'
            f"<title>{escape(name)}: {v:+,.2f}</title></path>"
        )
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 - numbers + escaped labels  # nosec B704


@dataclass(frozen=True)
class Guide:
    """A level drawn across a price chart: one value per bar (a channel that moves with the
    bars) and the value for the coming bar, drawn to the right edge and labelled there."""

    values: Sequence[float | None]
    tail: float
    css: str  # its stylesheet class: colour and dash
    text: str  # the direct label at the right edge
    legend: str
    step: bool = True  # a level that holds for each bar; False: a smooth line through the closes


def _steps(
    values: Sequence[float | None], x: Sequence[float], y: Callable[[float], float]
) -> list[str]:
    """Step lines (one polyline per run of defined values): value i holds from x[i] to x[i+1]."""
    runs: list[list[str]] = [[]]
    for i, v in enumerate(values):
        if v is None:
            runs.append([])
            continue
        yy = y(v)
        runs[-1] += [f"{x[i]:.1f},{yy:.1f}", f"{x[i + 1]:.1f},{yy:.1f}"]
    return [" ".join(r) for r in runs if r]


def price_chart(
    times: Sequence[datetime],
    closes: Sequence[float],
    guides: Sequence[Guide] = (),
    *,
    series: str = "price",
    marker: int | None = None,
    marker_text: str = "",
    tips: Sequence[str] | None = None,
    label: str = "",
    width: int = 420,
    height: int = 200,
    time_format: str = "%d %b %H:%M",
) -> Markup:
    """Hourly closes with the levels a rule acts on, sized for a card: a legend, the price
    line, each guide as a dashed step line labelled at its right end, an optional marker on
    the price (a purchase) and one tooltip per hour. Bar i spans x[i]..x[i+1]; its close sits
    at the end of its span, and the right-most span is the coming bar."""
    n = len(closes)
    if n < 2:
        return Markup('<p class="muted">Not enough completed hourly bars to draw the chart.</p>')
    pad_l, pad_r, pad_t, pad_b = 52, 6, 8, 20
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    seen = [*closes, *(g.tail for g in guides)]
    seen += [v for g in guides for v in g.values if v is not None]
    lo, hi = min(seen), max(seen)
    margin = (hi - lo) * 0.06 or abs(hi) * 0.01 or 1.0
    lo, hi = (max(lo - margin, 0.0) if lo >= 0 else lo - margin), hi + margin  # money stays ≥ 0
    x = [pad_l + w * k / (n + 1) for k in range(n + 2)]

    def y(v: float) -> float:
        return pad_t + h * (1 - (v - lo) / (hi - lo))

    title = escape(label)
    bottom = pad_t + h
    legend = [f'<li><span class="key key-price" aria-hidden="true"></span>{escape(series)}</li>']
    legend += [
        f'<li><span class="key key-{escape(g.css)}" aria-hidden="true"></span>'
        f"{escape(g.legend)}</li>"
        for g in guides
    ]
    if marker is not None:
        legend.append('<li><span class="key key-marker" aria-hidden="true"></span>bought</li>')
    parts = [
        f'<ul class="legend">{"".join(legend)}</ul>',
        f'<svg class="chart chart-price" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{title}">',
        f"<title>{title}</title>",
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{bottom}" {AXIS}/>',
        f'<line x1="{pad_l}" y1="{bottom}" x2="{x[-1]:.1f}" y2="{bottom}" {AXIS}/>',
        f'<text x="{pad_l - 5}" y="{pad_t + 10}" text-anchor="end" {TEXT}>{_num(hi)}</text>',
        f'<text x="{pad_l - 5}" y="{bottom}" text-anchor="end" {TEXT}>{_num(lo)}</text>',
        f'<text x="{pad_l}" y="{height - 5}" {TEXT}>{times[0].strftime(time_format)}</text>',
        f'<text x="{x[n]:.1f}" y="{height - 5}" text-anchor="end" {TEXT}>'
        f"{times[-1].strftime(time_format)} UTC</text>",
    ]
    if tips is not None:  # hover targets: one column per hour
        parts += [
            f'<rect class="hit" x="{x[i]:.1f}" y="{pad_t}" width="{x[i + 1] - x[i]:.1f}" '
            f'height="{h}"><title>{escape(tip)}</title></rect>'
            for i, tip in enumerate(tips)
        ]
    for g in guides:
        if g.step:
            runs = _steps([*g.values, g.tail], x, y)
        else:  # through the bars' ends, like the price, then on to the right edge
            pts = [(x[i + 1], v) for i, v in enumerate(g.values) if v is not None]
            pts.append((x[-1], g.tail))
            runs = [" ".join(f"{px:.1f},{y(v):.1f}" for px, v in pts)]
        parts += [f'<polyline class="guide {escape(g.css)}" points="{pts}"/>' for pts in runs]
    price = " ".join(f"{x[i + 1]:.1f},{y(c):.1f}" for i, c in enumerate(closes))
    parts.append(f'<polyline class="series" points="{price}"/>')
    # Direct labels at the right edge; two that would touch go one above, one below.
    spots = sorted((y(g.tail), g.text) for g in guides)
    for k, (yy, text) in enumerate(spots):
        above = k == 0 or yy - spots[k - 1][0] >= 14 or k % 2 == 0
        ty = yy - 4 if above else yy + 12
        parts.append(
            f'<text class="guide-label" x="{x[-1] - 2:.1f}" y="{ty:.1f}" text-anchor="end" '
            f'font-size="11">{escape(text)}</text>'
        )
    if marker is not None and 0 <= marker < n:
        mx, my = x[marker + 1], y(closes[marker])
        anchor = "end" if mx > pad_l + w * 0.75 else "start"
        tx = mx - 7 if anchor == "end" else mx + 7
        parts.append(f'<circle class="marker" cx="{mx:.1f}" cy="{my:.1f}" r="4"/>')
        parts.append(
            f'<text class="guide-label" x="{tx:.1f}" y="{my - 7:.1f}" text-anchor="{anchor}" '
            f'font-size="11">{escape(marker_text)}</text>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 - numbers + escaped text  # nosec B704


def spark(
    values: Sequence[float],
    *,
    split: int | None = None,
    width: int = 160,
    height: int = 36,
    label: str = "",
) -> Markup:
    """A small running total for a table row: its zero line and, at ``split``, a dashed mark
    where the validation period starts. The row carries the numbers."""
    if len(values) < 2:
        return Markup('<span class="muted">—</span>')
    pts = [0.0, *values]
    lo, hi = min(min(pts), 0.0), max(max(pts), 0.0)
    span = (hi - lo) or 1.0

    def x(i: float) -> float:
        return 2 + (width - 4) * i / (len(pts) - 1)

    def y(v: float) -> float:
        return 2 + (height - 4) * (1 - (v - lo) / span)

    title = escape(label)
    parts = [
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">',
        f"<title>{title}</title>",
        f'<line class="axis" x1="2" y1="{y(0):.1f}" x2="{width - 2}" y2="{y(0):.1f}"/>',
    ]
    if split is not None and 0 < split < len(values):
        xs = x(split)
        parts.append(f'<line class="split" x1="{xs:.1f}" y1="2" x2="{xs:.1f}" y2="{height - 2}"/>')
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pts))
    parts += [f'<polyline class="series" points="{line}"/>', "</svg>"]
    return Markup("".join(parts))  # noqa: S704 - numbers + escaped title  # nosec B704
