"""Atlassian Statuspage JSON (``/api/v2/status.json``) for venue health."""

from __future__ import annotations

from typing import Any

import httpx

from cosmic.sources.connectors._parse import iso
from cosmic.sources.contract import FetchResult, SourceSchemaError, VenueStatusRecord
from cosmic.sources.http import TokenBucket, get_json

INDICATOR_STATUS = {
    "none": "OPERATIONAL",
    "minor": "DEGRADED",
    "major": "DEGRADED",
    "critical": "DEGRADED",
    "maintenance": "MAINTENANCE",
}


def parse_status(data: Any) -> VenueStatusRecord:
    try:
        indicator = str(data["status"]["indicator"])
        description = str(data["status"]["description"])
    except (TypeError, KeyError) as exc:
        raise SourceSchemaError("status response missing status.indicator") from exc
    updated = data.get("page", {}).get("updated_at") if isinstance(data, dict) else None
    return VenueStatusRecord(
        status=INDICATOR_STATUS.get(indicator, "UNKNOWN"),
        indicator=indicator,
        description=description[:300],
        source_ts=iso(updated, "page.updated_at") if updated else None,
    )


class StatuspageConnector:
    def __init__(self, source_key: str, base_url: str, client: httpx.AsyncClient) -> None:
        self.source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.bucket = TokenBucket(rate=0.5, capacity=1)

    async def fetch_status(self) -> FetchResult[VenueStatusRecord]:
        data, status = await get_json(
            self.client, f"{self.base_url}/status.json", bucket=self.bucket
        )
        return FetchResult([parse_status(data)], status)
