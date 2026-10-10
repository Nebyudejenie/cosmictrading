"""Capabilities (V6 §2.2) and code-defined roles. One capability never implies another."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Capability(StrEnum):
    VIEW = "VIEW"
    READ_MARKET_DATA = "READ_MARKET_DATA"
    READ_ACCOUNT = "READ_ACCOUNT"
    RESEARCH = "RESEARCH"
    PROPOSE_TRADE = "PROPOSE_TRADE"
    APPROVE_TRADE = "APPROVE_TRADE"
    SUBMIT_ORDER = "SUBMIT_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    MANAGE_APPROVED_POSITION = "MANAGE_APPROVED_POSITION"
    CHANGE_RISK_LIMITS = "CHANGE_RISK_LIMITS"
    ENABLE_STRATEGY_AUTHORITY = "ENABLE_STRATEGY_AUTHORITY"
    RECONCILE = "RECONCILE"
    ADMINISTER_SYSTEM = "ADMINISTER_SYSTEM"
    ACTIVATE_EMERGENCY_STOP = "ACTIVATE_EMERGENCY_STOP"


class Role(StrEnum):
    OWNER = "OWNER"
    ANALYST = "ANALYST"
    VIEWER = "VIEWER"


class ServiceIdentity(StrEnum):
    RESEARCH_WORKER = "research-worker"
    EXECUTION_AUTHORITY = "execution-authority"


ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    # The owner holds every human capability. SUBMIT_ORDER / CANCEL_ORDER are also
    # held so the owner can cancel an order manually; submission itself still only
    # happens through the execution authority from a consumed approval.
    Role.OWNER: frozenset(Capability),
    Role.ANALYST: frozenset(
        {
            Capability.VIEW,
            Capability.READ_MARKET_DATA,
            Capability.READ_ACCOUNT,
            Capability.RESEARCH,
            Capability.PROPOSE_TRADE,
            Capability.ACTIVATE_EMERGENCY_STOP,
        }
    ),
    Role.VIEWER: frozenset({Capability.VIEW, Capability.READ_MARKET_DATA}),
}

SERVICE_CAPABILITIES: dict[ServiceIdentity, frozenset[Capability]] = {
    ServiceIdentity.RESEARCH_WORKER: frozenset(
        {Capability.READ_MARKET_DATA, Capability.RESEARCH, Capability.PROPOSE_TRADE}
    ),
    ServiceIdentity.EXECUTION_AUTHORITY: frozenset(
        {
            Capability.READ_ACCOUNT,
            Capability.SUBMIT_ORDER,
            Capability.CANCEL_ORDER,
            Capability.MANAGE_APPROVED_POSITION,
            Capability.RECONCILE,
        }
    ),
}

# Capabilities whose use requires TOTP verification within the step-up window.
STEP_UP_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.APPROVE_TRADE,
        Capability.CHANGE_RISK_LIMITS,
        Capability.ENABLE_STRATEGY_AUTHORITY,
        Capability.ADMINISTER_SYSTEM,
    }
)


def capabilities_for_roles(roles: Iterable[str]) -> frozenset[Capability]:
    caps: set[Capability] = set()
    for name in roles:
        try:
            caps |= ROLE_CAPABILITIES[Role(name)]
        except ValueError:
            continue  # unknown role grants nothing
    return frozenset(caps)
