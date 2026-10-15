"""Adapter lookup. LIVE accounts have no adapter in this build: lookups fail closed.

SANDBOX accounts (ADR-026) use the Binance Spot Test Network adapter, which exists only in the
worker and only when the testnet keys are mounted. Other processes (web, bot) cannot reach it
and hand such work to the worker.
"""

from __future__ import annotations

from cosmic.core.errors import ConflictError
from cosmic.trading.accounts import Account
from cosmic.trading.brokers.base import BrokerAdapter
from cosmic.trading.brokers.binance_testnet import ADAPTER as TESTNET_ADAPTER
from cosmic.trading.brokers.binance_testnet import SandboxBroker
from cosmic.trading.brokers.paper import PaperBroker


class BrokerRegistry:
    def __init__(self, paper: PaperBroker, sandbox: SandboxBroker | None = None) -> None:
        self.paper = paper
        self.sandbox = sandbox

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
        raise ConflictError(
            f"no broker adapter '{account.broker_adapter}' is configured for "
            f"{account.kind} accounts; live trading is not available in this build"
        )

    def serves(self, account: Account) -> bool:
        try:
            self.for_account(account)
        except ConflictError:
            return False
        return True
