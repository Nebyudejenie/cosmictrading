"""Adapter lookup. LIVE accounts have no adapter in this build: lookups fail closed."""

from __future__ import annotations

from cosmic.core.errors import ConflictError
from cosmic.trading.accounts import Account
from cosmic.trading.brokers.base import BrokerAdapter
from cosmic.trading.brokers.paper import PaperBroker


class BrokerRegistry:
    def __init__(self, paper: PaperBroker) -> None:
        self.paper = paper

    def for_account(self, account: Account) -> BrokerAdapter:
        if account.kind == "PAPER" and account.broker_adapter == "paper":
            return self.paper
        raise ConflictError(
            f"no broker adapter '{account.broker_adapter}' is configured for "
            f"{account.kind} accounts; live trading is not available in this build"
        )
