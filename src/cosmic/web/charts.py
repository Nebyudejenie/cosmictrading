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
) -> Markup:
    if len(values) < 2:
        return Markup('<p class="muted">Not enough completed bars to draw a chart.</p>')
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
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">',
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
