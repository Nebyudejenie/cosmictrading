"""Resource pressure: free disk and memory in use drive background-work shedding (S115:
memory)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Pressure(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


PRESSURE_ORDER = [Pressure.NORMAL, Pressure.WARNING, Pressure.CRITICAL]


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


# Memory (S115): the share of this container's memory limit in use (cgroup v2, as `docker stats`
# counts it: the page cache it can reclaim is not counted), or of the host's when no limit is
# set. Every container on the VM has its own limit; one that reaches it is killed and restarted.
MEMORY_WARNING_USED_PCT = 85
MEMORY_CRITICAL_USED_PCT = 95
CGROUP = Path("/sys/fs/cgroup")
MEMINFO = Path("/proc/meminfo")


@dataclass(frozen=True)
class MemoryStatus:
    used_bytes: int
    limit_bytes: int
    used_pct: float
    pressure: Pressure
    scope: str  # "container" (its own limit) or "host"


def _pressure_of(used_pct: float, warning: int, critical: int) -> Pressure:
    if used_pct >= critical:
        return Pressure.CRITICAL
    if used_pct >= warning:
        return Pressure.WARNING
    return Pressure.NORMAL


def memory_status(
    *,
    warning_pct: int = MEMORY_WARNING_USED_PCT,
    critical_pct: int = MEMORY_CRITICAL_USED_PCT,
    cgroup: Path = CGROUP,
    meminfo: Path = MEMINFO,
) -> MemoryStatus | None:
    """The memory in use against this container's limit, else the host's; None when neither
    can be read (never guessed)."""
    try:
        limit_raw = (cgroup / "memory.max").read_text().strip()
        current = int((cgroup / "memory.current").read_text().strip())
        if limit_raw != "max":
            limit = int(limit_raw)
            stat = dict(
                line.split(" ", 1) for line in (cgroup / "memory.stat").read_text().splitlines()
            )
            used = max(current - int(stat.get("inactive_file", "0")), 0)
            pct = 100.0 * used / limit if limit else 100.0
            return MemoryStatus(
                used, limit, round(pct, 2), _pressure_of(pct, warning_pct, critical_pct),
                "container",
            )  # fmt: skip
    except (OSError, ValueError):
        pass
    try:
        info = {}
        for line in meminfo.read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) * 1024
        total, available = info["MemTotal"], info["MemAvailable"]
    except (OSError, ValueError, KeyError, IndexError):
        return None
    used = max(total - available, 0)
    pct = 100.0 * used / total if total else 100.0
    return MemoryStatus(
        used, total, round(pct, 2), _pressure_of(pct, warning_pct, critical_pct), "host"
    )
