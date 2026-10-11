"""HTTP client construction for AI providers (the only place httpx is imported in cosmic.ai)."""

from __future__ import annotations

import httpx

AIHttpClient = httpx.AsyncClient
Transport = httpx.AsyncBaseTransport


def make_client(transport: Transport | None = None) -> AIHttpClient:
    return httpx.AsyncClient(transport=transport, follow_redirects=False)
