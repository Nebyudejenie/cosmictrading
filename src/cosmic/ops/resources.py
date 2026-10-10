"""Resource pressure: disk free percentage drives background-work shedding."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Pressure(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class DiskStatus:
    path: str
    total_bytes: int
    free_bytes: int
    free_pct: float
    pressure: Pressure


def disk_status(path: Path, warning_pct: int, critical_pct: int) -> DiskStatus:
    usage = shutil.disk_usage(path)
    free_pct = 100.0 * usage.free / usage.total if usage.total else 0.0
    if free_pct < critical_pct:
        pressure = Pressure.CRITICAL
    elif free_pct < warning_pct:
        pressure = Pressure.WARNING
    else:
        pressure = Pressure.NORMAL
    return DiskStatus(str(path), usage.total, usage.free, round(free_pct, 2), pressure)
