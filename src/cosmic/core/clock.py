"""Injectable UTC clock. All business time comes from here, never from clients."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic clock for tests and simulations."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("timezone-aware datetime required")
        self._now = value.astimezone(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
    return value.astimezone(UTC)


def isoformat_utc(value: datetime) -> str:
    """Canonical UTC ISO-8601 with microseconds and a 'Z' suffix."""
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
