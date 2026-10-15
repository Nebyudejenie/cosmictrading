"""HFM through a MetaTrader 5 bridge (ADR-028): names shared by the portal, the worker and tests.

The bridge program itself lives in ``bridge/cosmic_mt5_bridge.py`` and runs on Windows next to
the MT5 terminal; nothing here talks to the network.
"""

from __future__ import annotations

import hashlib

ADAPTER = "hfm_mt5_demo"
VENUE = "HFM"
BRIDGE = "hfm-demo"
QUOTE_SOURCE = "hfm_mt5_bridge"
STATUS_SOURCE = "hfm_mt5_status"
QUOTE = "USD"


def magic(client_order_id: str) -> int:
    """The MT5 ``magic`` number for one of our orders: the first 63 bits of SHA-256 of its client
    order ID. Orders, deals and positions carry it, so an order is found again without guessing
    (docs/integrations/mt5-python-api.md facts 15, 26)."""
    return int.from_bytes(hashlib.sha256(client_order_id.encode()).digest()[:8], "big") >> 1
