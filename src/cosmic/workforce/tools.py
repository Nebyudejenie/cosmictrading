"""Allowlisted toolboxes per role. A role can only call the tools it declares."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from cosmic.workforce.roles import ROLES


class ToolNotAllowedError(Exception):
    pass


class Toolbox:
    def __init__(self, role_key: str, tools: dict[str, Callable[..., Awaitable[Any]]]) -> None:
        self.role = ROLES[role_key]
        self._tools = tools

    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in self.role.allowed_tools:
            raise ToolNotAllowedError(f"role {self.role.key} may not use tool {name}")
        fn = self._tools.get(name)
        if fn is None:
            raise ToolNotAllowedError(f"tool {name} is not available")
        return await fn(*args, **kwargs)
