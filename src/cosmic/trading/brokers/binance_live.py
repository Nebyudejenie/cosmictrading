"""The Binance real-money link as the worker runs it (ADR-034), built and kept off.

Built only in the worker, and only while lock 1 (``LIVE_ADAPTERS`` registers this adapter, a
reviewed code change) and lock 3 (``COSMIC_LIVE_VENUES`` names BINANCE) are open and both key
files are mounted. Neither is true in this build, so ``build`` returns None and no process can
reach the real venue with a key. Elsewhere the key is named only by its fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.logs import get_logger
from cosmic.sources.registry import INSTRUMENTS
from cosmic.trading import authority
from cosmic.trading.brokers.binance_spot import BinanceSpotAdapter, Ed25519Signer
from cosmic.trading.brokers.binance_testnet import key_fingerprint

ADAPTER = "binance_spot_live"
VENUE = "BINANCE"
LISTING = "binance_public"

log = get_logger("cosmic.trading.brokers.binance_live")


@dataclass(frozen=True)
class LiveBroker:
    adapter: BinanceSpotAdapter
    key_fingerprint: str


def live_symbols() -> dict[str, str]:
    """Instrument key → Binance symbol for every real-venue Binance instrument."""
    return {d.canonical_key: d.listings[LISTING] for d in INSTRUMENTS if d.venue_key == VENUE}


def server_locks_open(settings: Settings) -> bool:
    """Locks 1 and 3 of ADR-034: the reviewed code registers this adapter and the server's env
    file names the venue. Read at call time, never cached."""
    return ADAPTER in authority.LIVE_ADAPTERS and VENUE in settings.live_venues


def build(
    settings: Settings, clock: Clock, client: httpx.AsyncClient | None = None
) -> LiveBroker | None:
    """The live link, or None (fail closed) unless locks 1 and 3 are open and a usable key is
    mounted. The other locks (mode cap, gates, key check, activation, approval) are checked
    where each acts."""
    if not server_locks_open(settings):
        return None
    key, pem = settings.binance_live_api_key, settings.binance_live_private_key
    if key is None or pem is None:
        log.warning("binance_live_keys_missing", reason="the live key files are not mounted")
        return None
    try:
        signer = Ed25519Signer(pem.get_secret_value().encode())
    except (ValueError, TypeError):
        log.error("binance_live_key_unusable", reason="private key is not an Ed25519 PEM key")
        return None
    api_key = key.get_secret_value()
    adapter = BinanceSpotAdapter.live_link(
        client or httpx.AsyncClient(timeout=10),
        api_key=api_key,
        signer=signer,
        symbols=live_symbols(),
        clock=clock,
    )
    return LiveBroker(adapter, key_fingerprint(api_key))
