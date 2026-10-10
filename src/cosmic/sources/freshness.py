"""Freshness classification (PRIN-06, DATA-16/17)."""

from __future__ import annotations

from datetime import datetime, timedelta

from cosmic.core.labels import DataLabel

FUTURE_TOLERANCE = timedelta(minutes=2)


def classify(
    source_ts: datetime | None,
    now: datetime,
    freshness_limit_seconds: int,
    *,
    latency_class: str = "DELAYED_POLLING",
    enabled: bool = True,
    simulated: bool = False,
) -> DataLabel:
    if not enabled:
        return DataLabel.UNAVAILABLE
    if simulated:
        return DataLabel.SIMULATED
    if source_ts is None:
        return DataLabel.UNKNOWN
    age = now - source_ts
    if age < -FUTURE_TOLERANCE:
        return DataLabel.UNKNOWN  # timestamp from the future: not trustworthy
    if age > timedelta(seconds=freshness_limit_seconds):
        return DataLabel.STALE
    # Only a licensed streaming feed within its SLA may be called LIVE. None exists here.
    return DataLabel.LIVE if latency_class == "LIVE_STREAM" else DataLabel.DELAYED


def age_seconds(source_ts: datetime | None, now: datetime) -> float | None:
    return None if source_ts is None else (now - source_ts).total_seconds()
