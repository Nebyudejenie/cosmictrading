"""Parsing helpers shared by connectors."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cosmic.core.money import to_decimal
from cosmic.sources.contract import SourceSchemaError


def dec(value: Any, field: str) -> Decimal:
    try:
        return to_decimal(value if not isinstance(value, float) else str(value))
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError(f"field {field!r} is not a decimal") from exc


def opt_dec(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return dec(value, field)


def epoch(value: Any, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SourceSchemaError(f"field {field!r} is not an epoch timestamp") from exc


def iso(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SourceSchemaError(f"field {field!r} is not a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceSchemaError(f"field {field!r} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SourceSchemaError(f"field {field!r} has no timezone")
    return parsed.astimezone(UTC)


def decimals_to_step(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)
