"""Jinja2 filters: exact decimal display, UTC times, labels."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from jinja2 import Environment

from cosmic.core.clock import ensure_utc
from cosmic.core.money import canonical_str


def fmt_dec(value: Any, places: int | None = None) -> str:
    """Exact decimal display; ``places`` rounds for reading (money to the cent). Numeric
    strings — approval packets store numbers as strings — are shown the same way."""
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except InvalidOperation:
            return str(value)
    if isinstance(value, Decimal):
        if places is not None:
            return f"{value:,.{places}f}"
        return canonical_str(value)
    return str(value)


def fmt_ts(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):  # ISO 8601 as stored in approval packets
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return str(value)
    if isinstance(value, datetime):
        return ensure_utc(value).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value)


def fmt_age(seconds: Any) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def register_filters(env: Environment) -> None:
    env.filters["dec"] = fmt_dec
    env.filters["ts"] = fmt_ts
    env.filters["age"] = fmt_age
