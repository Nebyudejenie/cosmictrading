from __future__ import annotations

import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from cosmic.ops.resources import Pressure, disk_status

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
