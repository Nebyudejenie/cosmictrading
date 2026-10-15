"""Backup sets on disk, shaped like deploy/backup/cosmic-backup.sh writes them (for tests)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def make_set(directory: Path, when: datetime, *, skip: str | None = None, lie: bool = False) -> str:
    """Four files per set; ``skip`` leaves one out, ``lie`` makes the manifest's sizes wrong."""
    stem = f"cosmic-{when:%Y%m%dT%H%M%SZ}"
    files = []
    for suffix, size in (("dump.age", 9000), ("config.tar.age", 300)):
        (directory / f"{stem}.{suffix}").write_bytes(b"x" * size)
        files.append({"name": f"{stem}.{suffix}", "bytes": size + (1 if lie else 0)})
    (directory / f"{stem}.sha256").write_text("checksums\n")
    (directory / f"{stem}.manifest.json").write_text(json.dumps({"stem": stem, "files": files}))
    if skip:
        (directory / f"{stem}.{skip}").unlink()
    return stem


def record_drill(directory: Path, stem: str, when: datetime, result: str = "PASS") -> None:
    """One line, as scripts/drill-staging.sh appends it."""
    line = {"stem": stem, "drilled_at": when.isoformat(), "result": result, "checks": 10}
    with (directory / "restore-drills.jsonl").open("a") as f:
        f.write(json.dumps({**line, "where": "workstation"}) + "\n")
