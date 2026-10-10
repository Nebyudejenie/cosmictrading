"""Job handler registry and periodic schedule."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from cosmic.worker.context import WorkerContext

Handler = Callable[[WorkerContext, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class Periodic:
    kind: str
    interval_seconds: int
    priority: int
    payload: dict[str, Any] = field(default_factory=dict)
    enabled: Callable[[WorkerContext], bool] = lambda _ctx: True


class Registry:
    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self.periodic: list[Periodic] = []

    def handler(self, kind: str) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            if kind in self.handlers:
                raise ValueError(f"duplicate handler for {kind}")
            self.handlers[kind] = fn
            return fn

        return decorator

    def every(self, periodic: Periodic) -> None:
        self.periodic.append(periodic)
