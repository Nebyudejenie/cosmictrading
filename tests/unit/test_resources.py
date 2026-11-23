from __future__ import annotations

import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from cosmic.ops.resources import Pressure, disk_status, memory_status

Usage = namedtuple("Usage", "total used free")


@pytest.mark.parametrize(
    ("free", "expected"),
    [
        (50, Pressure.NORMAL),
        (15, Pressure.NORMAL),
        (14, Pressure.WARNING),
        (10, Pressure.WARNING),
        (9, Pressure.CRITICAL),
        (0, Pressure.CRITICAL),
    ],
)
def test_disk_thresholds(monkeypatch: pytest.MonkeyPatch, free: int, expected: Pressure) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100, 100 - free, free))
    status = disk_status(Path("/"), warning_pct=15, critical_pct=10)
    assert status.pressure is expected and status.free_pct == free


def _cgroup(tmp: Path, *, limit: str, current: int, inactive: int) -> Path:
    (tmp / "memory.max").write_text(limit + "\n")
    (tmp / "memory.current").write_text(f"{current}\n")
    (tmp / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\nactive_file 2\n")
    return tmp


@pytest.mark.parametrize(
    ("current", "expected"),
    [(700, Pressure.NORMAL), (900, Pressure.WARNING), (1000, Pressure.CRITICAL)],
)
def test_memory_is_measured_against_the_containers_own_limit(
    tmp_path: Path, current: int, expected: Pressure
) -> None:
    """S115: as `docker stats` counts it, the page cache the container can reclaim excluded:
    1,000 bytes allowed, 50 of them reclaimable cache."""
    cg = _cgroup(tmp_path, limit="1000", current=current, inactive=50)
    status = memory_status(cgroup=cg, meminfo=tmp_path / "none")
    assert status is not None and status.scope == "container"
    assert status.used_bytes == current - 50 and status.pressure is expected


def test_without_a_container_limit_the_hosts_memory_is_read_and_nothing_is_guessed(
    tmp_path: Path,
) -> None:
    cg = _cgroup(tmp_path, limit="max", current=1, inactive=0)
    info = tmp_path / "meminfo"
    info.write_text("MemTotal:       10000 kB\nMemFree: 100 kB\nMemAvailable:    1000 kB\n")
    status = memory_status(cgroup=cg, meminfo=info)
    assert status is not None and status.scope == "host" and status.used_pct == 90.0
    assert status.pressure is Pressure.WARNING
    assert memory_status(cgroup=tmp_path / "x", meminfo=tmp_path / "y") is None
