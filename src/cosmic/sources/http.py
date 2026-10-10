"""Outbound HTTP for data sources: shared client, rate limiting, SSRF guard (SEC-15)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from cosmic import __version__
from cosmic.sources.contract import (
    SourceRateLimitedError,
    SourceSchemaError,
    SourceUnavailableError,
)

USER_AGENT = f"cosmictrading-quant/{__version__} (+operator research; low-rate polling)"
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_BODY_BYTES = 2_000_000


class TokenBucket:
    """Async token bucket: ``rate`` requests per second, burst ``capacity``."""

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
        transport=transport,
    )


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    bucket: TokenBucket | None = None,
) -> tuple[Any, int]:
    """GET JSON with exact decimals. Maps transport/HTTP failures to source errors."""
    if bucket is not None:
        await bucket.acquire()
    try:
        response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise SourceUnavailableError(f"timeout contacting {urlparse(url).netloc}") from exc
    except httpx.TransportError as exc:
        raise SourceUnavailableError(
            f"connection error contacting {urlparse(url).netloc}: {type(exc).__name__}"
        ) from exc
    if response.status_code == 429:
        retry = response.headers.get("retry-after")
        raise SourceRateLimitedError(
            "rate limited by source", float(retry) if retry and retry.isdigit() else None
        )
    if response.status_code >= 500:
        raise SourceUnavailableError(f"source returned HTTP {response.status_code}")
    if response.status_code != 200:
        raise SourceSchemaError(f"unexpected HTTP {response.status_code}")
    if len(response.content) > MAX_BODY_BYTES:
        raise SourceSchemaError("response too large")
    try:
        return json.loads(response.text, parse_float=Decimal), response.status_code
    except ValueError as exc:
        raise SourceSchemaError("response is not valid JSON") from exc


# ---- SSRF guard for operator- or AI-supplied URLs (document fetching) --------------------

_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "::ffff:0:0/96",
    )
]


class UnsafeUrlError(ValueError):
    pass


def check_public_url(url: str, resolver: Any = None) -> str:
    """Validate that ``url`` is https and resolves only to public addresses.

    Returns the hostname. ``resolver`` (host → list of IP strings) is injectable for tests.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeUrlError("only https URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrlError("URL must have a host and no credentials")
    if parsed.port not in (None, 443):
        raise UnsafeUrlError("non-standard ports are not allowed")
    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        if resolver is None:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            addresses = sorted({str(info[4][0]) for info in infos})
        else:
            addresses = list(resolver(host))
    if not addresses:
        raise UnsafeUrlError("host does not resolve")
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if any(ip in net for net in _BLOCKED_NETS) or ip.is_private or ip.is_reserved:
            raise UnsafeUrlError("URL resolves to a non-public address")
    return host
