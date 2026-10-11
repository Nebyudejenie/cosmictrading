"""Loopback health probe used by deploy smoke tests (`cosmic probe web`).

The only HTTP client outside the adapter packages: it can reach nothing but 127.0.0.1 on
the configured port (the architecture test allowlists this module for that reason).
"""

from __future__ import annotations

import urllib.request


def local_ready(port: int, timeout: float = 4.0) -> bool:
    if not 0 < port < 65536:
        return False
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/readyz", headers={"Host": "127.0.0.1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - loopback only  # nosec B310
            return bool(resp.status == 200)
    except OSError:
        return False
