"""The Binance Spot Test Network adapter as the worker runs it (ADR-026).

Built only in the worker, and only when both testnet key files are mounted as secrets. The
adapter speaks for the one test-network account its key belongs to. Elsewhere the key is
identified only by a fingerprint (a truncated SHA-256), never by its value.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.logs import get_logger
from cosmic.sources.registry import INSTRUMENTS
from cosmic.trading.brokers.binance_spot import BinanceSpotAdapter, Ed25519Signer

ADAPTER = "binance_spot_testnet"
VENUE = "BINANCE_TESTNET"
ENVIRONMENT = "testnet.binance.vision"
LISTING = "binance_testnet_public"

log = get_logger("cosmic.trading.brokers.binance_testnet")


@dataclass(frozen=True)
class SandboxBroker:
    adapter: BinanceSpotAdapter
    key_fingerprint: str


def key_fingerprint(api_key: str) -> str:
    """Which key a contract run or an account used, without revealing the key."""
    return hashlib.sha256(api_key.strip().encode()).hexdigest()[:16]


def testnet_symbols() -> dict[str, str]:
    """Instrument key → Binance symbol for every test-network instrument in the registry."""
    return {d.canonical_key: d.listings[LISTING] for d in INSTRUMENTS if d.venue_key == VENUE}


def build(
    settings: Settings, clock: Clock, client: httpx.AsyncClient | None = None
) -> SandboxBroker | None:
    """The adapter, or None when the keys are absent or unusable (SANDBOX fails closed; paper
    trading is unaffected)."""
    key, pem = settings.binance_testnet_api_key, settings.binance_testnet_private_key
    if key is None or pem is None:
        return None
    try:
        signer = Ed25519Signer(pem.get_secret_value().encode())
    except (ValueError, TypeError):
        log.error("binance_testnet_key_unusable", reason="private key is not an Ed25519 PEM key")
        return None
    api_key = key.get_secret_value()
    adapter = BinanceSpotAdapter(
        client or httpx.AsyncClient(timeout=10),
        api_key=api_key,
        signer=signer,
        symbols=testnet_symbols(),
        clock=clock,
    )
    return SandboxBroker(adapter, key_fingerprint(api_key))
