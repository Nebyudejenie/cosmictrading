from __future__ import annotations

import pytest

from cosmic.identity.capabilities import (
    ROLE_CAPABILITIES,
    SERVICE_CAPABILITIES,
    STEP_UP_CAPABILITIES,
    Capability,
    Role,
    ServiceIdentity,
    capabilities_for_roles,
)


def test_capability_list_matches_directive() -> None:
    required = {
        "READ_MARKET_DATA",
        "READ_ACCOUNT",
        "RESEARCH",
        "PROPOSE_TRADE",
        "APPROVE_TRADE",
        "SUBMIT_ORDER",
        "CANCEL_ORDER",
        "MANAGE_APPROVED_POSITION",
        "CHANGE_RISK_LIMITS",
        "ENABLE_STRATEGY_AUTHORITY",
        "RECONCILE",
        "ADMINISTER_SYSTEM",
    }
    assert required <= {c.value for c in Capability}


def test_research_worker_cannot_approve_submit_or_change_authority() -> None:
    caps = SERVICE_CAPABILITIES[ServiceIdentity.RESEARCH_WORKER]
    for forbidden in (
        Capability.APPROVE_TRADE,
        Capability.SUBMIT_ORDER,
        Capability.CANCEL_ORDER,
        Capability.CHANGE_RISK_LIMITS,
        Capability.ENABLE_STRATEGY_AUTHORITY,
        Capability.ADMINISTER_SYSTEM,
        Capability.MANAGE_APPROVED_POSITION,
    ):
        assert forbidden not in caps


def test_execution_authority_cannot_approve_or_change_limits() -> None:
    caps = SERVICE_CAPABILITIES[ServiceIdentity.EXECUTION_AUTHORITY]
    assert Capability.APPROVE_TRADE not in caps
    assert Capability.CHANGE_RISK_LIMITS not in caps
    assert Capability.ENABLE_STRATEGY_AUTHORITY not in caps


@pytest.mark.parametrize("role", [Role.ANALYST, Role.VIEWER])
def test_non_owner_roles_lack_privileged_capabilities(role: Role) -> None:
    caps = ROLE_CAPABILITIES[role]
    assert not caps & STEP_UP_CAPABILITIES
    assert Capability.SUBMIT_ORDER not in caps


def test_unknown_roles_grant_nothing() -> None:
    assert capabilities_for_roles(["ROOT", "admin"]) == frozenset()
    assert capabilities_for_roles(["VIEWER", "nope"]) == ROLE_CAPABILITIES[Role.VIEWER]


def test_step_up_set() -> None:
    assert {
        Capability.APPROVE_TRADE,
        Capability.CHANGE_RISK_LIMITS,
        Capability.ENABLE_STRATEGY_AUTHORITY,
        Capability.ADMINISTER_SYSTEM,
    } == STEP_UP_CAPABILITIES
