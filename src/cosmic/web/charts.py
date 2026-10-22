"""Server-side SVG charts (no client JavaScript; CSP-safe).

Only numbers and an escaped label are interpolated into the markup.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from markupsafe import Markup, escape

AXIS = 'stroke="currentColor" stroke-opacity=".3"'
TEXT = 'font-size="11" fill="currentColor"'


def line_chart(
    times: Sequence[datetime],
    values: Sequence[float],
    *,
    width: int = 720,
    height: int = 220,
    label: str = "",
    empty: str = "Not enough completed bars to draw a chart.",
    css_class: str = "chart",
) -> Markup:
    if len(values) < 2:
        return Markup('<p class="muted">{}</p>').format(empty)
    pad_l, pad_r, pad_t, pad_b = 64, 12, 12, 28
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    pts = " ".join(
        f"{pad_l + w * i / (len(values) - 1):.1f},{pad_t + h * (1 - (v - lo) / span):.1f}"
        for i, v in enumerate(values)
    )
    first, last = times[0].strftime("%m-%d %H:%M"), times[-1].strftime("%m-%d %H:%M")
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
        f'<text x="{pad_l}" y="{height - 8}" {TEXT}>{first} UTC</text>',
        f'<text x="{pad_l + w}" y="{height - 8}" text-anchor="end" {TEXT}>{last} UTC</text>',
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/>',
        "</svg>",
    ]
    return Markup("".join(parts))  # noqa: S704 - numbers + escaped title  # nosec B704


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
    bar = max(2.0, min(40.0, step * 0.7))
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
        top = zero - h * max(v, 0.0) / span
        height_px = max(1.0, h * abs(v) / span)
        cls = "bar-win" if v > 0 else ("bar-loss" if v < 0 else "bar-even")
        parts.append(
            f'<rect class="{cls}" x="{x:.1f}" y="{top:.1f}" width="{bar:.1f}" '
            f'height="{height_px:.1f}" rx="2"><title>{escape(name)}: {v:+,.2f}</title></rect>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 - numbers + escaped labels  # nosec B704
