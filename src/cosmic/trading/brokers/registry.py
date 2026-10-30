"""Adapter lookup. LIVE accounts have no adapter in this build: lookups fail closed.

SANDBOX accounts use the Binance Spot Test Network adapter (ADR-026) or the HFM MetaTrader 5
bridge adapter (ADR-028). Both exist only in the worker (the first only when the testnet keys are
mounted); other processes (web, bot) cannot reach them and hand such work to the worker.

A LIVE account uses the Binance live link (ADR-034) only in the worker, only when it was built
(locks 1 and 3 open and the live key mounted), and only while ``LIVE_ADAPTERS`` still registers
it at the moment of the lookup.
"""

from __future__ import annotations

import httpx

from cosmic.core.errors import ConflictError
from cosmic.trading import authority
from cosmic.trading.accounts import Account
from cosmic.trading.brokers.base import (
    BrokerAdapter,
    BrokerAmbiguousError,
    BrokerUnavailableError,
    VenueQuote,
)
from cosmic.trading.brokers.binance_live import ADAPTER as LIVE_ADAPTER
from cosmic.trading.brokers.binance_live import LiveBroker
from cosmic.trading.brokers.binance_spot import BinanceSpotAdapter
from cosmic.trading.brokers.binance_testnet import ADAPTER as TESTNET_ADAPTER
from cosmic.trading.brokers.binance_testnet import SandboxBroker
from cosmic.trading.brokers.mt5_bridge import ADAPTER as HFM_ADAPTER
from cosmic.trading.brokers.paper import PaperBroker
from cosmic.trading.modes import SIMULATED_ACCOUNT_KINDS


class BrokerRegistry:
    def __init__(
        self,
        paper: PaperBroker,
        sandbox: SandboxBroker | None = None,
        hfm: BrokerAdapter | None = None,
        live: LiveBroker | None = None,
    ) -> None:
        self.paper = paper
        self.sandbox = sandbox
        self.hfm = hfm
        self.live = live

    def for_account(self, account: Account) -> BrokerAdapter:
        if account.kind == "PAPER" and account.broker_adapter == "paper":
            return self.paper
        if account.kind == "SANDBOX" and account.broker_adapter == TESTNET_ADAPTER:
            if self.sandbox is None:
                raise ConflictError(
                    "the Binance test-network adapter is not available in this process "
                    "(only the worker holds its keys)"
                )
            return self.sandbox.adapter
        if account.kind == "SANDBOX" and account.broker_adapter == HFM_ADAPTER:
            if self.hfm is None:
                raise ConflictError(
                    "the HFM bridge adapter is not available in this process (worker only)"
                )
            return self.hfm
        if (
            account.kind == "LIVE"
            and account.broker_adapter == LIVE_ADAPTER
            and LIVE_ADAPTER in authority.LIVE_ADAPTERS
            and self.live is not None
        ):
            if self.live.key_fingerprint != account.key_fingerprint:
                raise ConflictError(
                    f"the worker's live key ({self.live.key_fingerprint}) is not the key "
                    f"{account.alias} was opened with ({account.key_fingerprint}); a new key "
                    "needs a new account"
                )
            return self.live.adapter
        raise ConflictError(
            f"no broker adapter '{account.broker_adapter}' is configured for "
            f"{account.kind} accounts; live trading is not available in this build"
        )

    async def venue_quote(self, account: Account, instrument_key: str) -> VenueQuote | None:
        """For a LIVE account, the venue's own best bid and ask read now through the live link
        (ADR-034); None for any other account, or when it cannot be read (R09 then refuses)."""
        if account.kind in SIMULATED_ACCOUNT_KINDS:
            return None
        try:
            adapter = self.for_account(account)
        except ConflictError:
            return None
        if not isinstance(adapter, BinanceSpotAdapter) or not adapter.live:
            return None
        try:
            return await adapter.book_ticker(instrument_key)
        except (BrokerUnavailableError, BrokerAmbiguousError, httpx.HTTPError, ArithmeticError):
            return None  # an unreadable book is no price: R09 refuses, the cycle goes on

    def serves(self, account: Account) -> bool:
        try:
            self.for_account(account)
        except ConflictError:
            return False
        return True
