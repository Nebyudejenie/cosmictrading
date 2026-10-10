"""Dependencies passed to job handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.db.engine import Database


@dataclass
class WorkerContext:
    settings: Settings
    db: Database
    clock: Clock
    worker_id: str
    services: dict[str, Any] = field(default_factory=dict)
