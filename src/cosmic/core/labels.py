"""Truthfulness labels shown next to every market or account value (PRIN-06)."""

from __future__ import annotations

from enum import StrEnum


class DataLabel(StrEnum):
    LIVE = "LIVE"  # licensed streaming data within SLA — not available in this build
    DELAYED = "DELAYED"  # fresh, but polled / delayed by design
    STALE = "STALE"  # older than the freshness limit
    SIMULATED = "SIMULATED"  # produced by the paper broker or a simulation
    UNKNOWN = "UNKNOWN"  # no usable timestamp
    UNAVAILABLE = "UNAVAILABLE"  # source disabled, failing or never fetched
    NOT_CONFIGURED = "NOT_CONFIGURED"  # integration switched off / missing credentials


class ValueBasis(StrEnum):
    BROKER_CONFIRMED = "BROKER_CONFIRMED"
    LOCAL_REQUEST = "LOCAL_REQUEST"
    ESTIMATE = "ESTIMATE"
    ASSUMPTION = "ASSUMPTION"
    SIMULATED = "SIMULATED"
