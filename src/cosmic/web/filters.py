"""Jinja2 filters: exact decimal display, UTC times, labels."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from jinja2 import Environment

from cosmic.core.clock import ensure_utc
from cosmic.core.money import canonical_str
from cosmic.research.library.expect import SKIP_REASONS
from cosmic.trading.modes import account_ref, environment_of, environment_short, friendly


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


def fmt_signed(value: object, places: int = 2) -> str:
    """A result with its sign: +12.40, −29.10 (a true minus sign), 0.00; "—" when unknown."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "—"
    if value is None:
        return "—"
    text = f"{abs(d):,.{places}f}"
    return f"+{text}" if d > 0 else (f"\u2212{text}" if d < 0 else text)


def register_filters(env: Environment) -> None:
    env.globals["environment_of"] = environment_of
    env.globals["account_ref"] = account_ref  # "alias [SIMULATED · DEMO]" (DIR-02)
    env.globals["environment_short"] = environment_short
    env.globals["friendly"] = friendly  # "CFD:XAU/USD@HFM" → "Gold"
    env.globals["skip_reasons"] = SKIP_REASONS  # the past-year test's skipped signals
    env.filters["dec"] = fmt_dec
    env.filters["signed"] = fmt_signed
    env.filters["ts"] = fmt_ts
    env.filters["age"] = fmt_age
