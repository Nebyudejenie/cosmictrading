from __future__ import annotations

import dataclasses

import pytest

from cosmic.workforce.roles import FORBIDDEN_FOR_ROLES, FORBIDDEN_TOOLS, ROLES
from cosmic.workforce.tools import Toolbox, ToolNotAllowedError


def test_exactly_the_seven_initial_roles() -> None:
    assert set(ROLES) == {
        "orchestrator",
        "source_data_analyst",
        "market_researcher",
        "quant_analyst",
        "risk_reviewer",
        "task_reviewer",
        "ops_security_monitor",
    }


@pytest.mark.parametrize("key", sorted(ROLES))
def test_role_declares_everything(key: str) -> None:
    role = ROLES[key]
    for f in dataclasses.fields(role):
        value = getattr(role, f.name)
        if f.name in ("reviewer", "capabilities", "ai_call_budget", "uses_ai"):
            continue
        assert value not in (None, "", ()), f"{key}.{f.name} is empty"
    assert not role.capabilities & FORBIDDEN_FOR_ROLES
    assert not role.allowed_tools & FORBIDDEN_TOOLS
    assert role.uses_ai == (role.ai_call_budget > 0)


def test_only_market_researcher_uses_ai_and_only_risk_reviewer_proposes() -> None:
    assert [k for k, r in ROLES.items() if "ai.research_narrative" in r.allowed_tools] == [
        "market_researcher"
    ]
    assert [k for k, r in ROLES.items() if "trading.propose" in r.allowed_tools] == [
        "risk_reviewer"
    ]


async def test_toolbox_enforces_allowlist() -> None:
    called: list[str] = []

    async def tool(*_a: object) -> str:
        called.append("x")
        return "ok"

    tb = Toolbox("market_researcher", {"trading.propose": tool, "ai.research_narrative": tool})
    with pytest.raises(ToolNotAllowedError):
        await tb.call("trading.propose")
    assert await tb.call("ai.research_narrative") == "ok"
    with pytest.raises(ToolNotAllowedError):
        await Toolbox("risk_reviewer", {}).call("broker.submit")
