"""Truthfulness labels shown next to every market or account value (PRIN-06)."""

from __future__ import annotations

from enum import StrEnum


class DataLabel(StrEnum):
    # A licensed stream within its SLA (none in this build), or a venue's own book read as a
    # real-money order is sent (ADR-034, context_builder.venue_market).
    LIVE = "LIVE"
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
