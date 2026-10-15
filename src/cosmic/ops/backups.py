"""Backup retention (ADR-019): keep 14 daily, 8 weekly and 6 monthly backup sets, plus the
3 most recent sets unconditionally (so a pre-deploy backup survives a later backup the same day).

A backup set is every file sharing a stem ``cosmic-YYYYmmddTHHMMSSZ`` (encrypted dump,
encrypted configuration archive, manifest). Files that do not match the pattern are
never touched, and the newest set is always kept.

``read_backups`` reports what the directory holds, for the worker (read-only): the newest set,
whether it is complete, and the last restore drill recorded in ``restore-drills.jsonl``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STEM = re.compile(r"^(cosmic-(\d{8}T\d{6}Z))\.")


@dataclass(frozen=True)
class Retention:
    daily: int = 14
    weekly: int = 8
    monthly: int = 6
    recent: int = 3


DEFAULT_RETENTION = Retention()


def parse_stem(name: str) -> tuple[str, datetime] | None:
    m = STEM.match(name)
    if m is None:
        return None
    try:
        ts = datetime.strptime(m.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return m.group(1), ts


def stems_to_keep(stems: dict[str, datetime], policy: Retention) -> set[str]:
    newest_first = sorted(stems, key=lambda s: stems[s], reverse=True)
    keep: set[str] = set(newest_first[: max(1, policy.recent)])
    for bucket, limit in (
        (lambda t: t.date().isoformat(), policy.daily),
        (lambda t: "{}-W{:02d}".format(*t.isocalendar()[:2]), policy.weekly),
        (lambda t: t.strftime("%Y-%m"), policy.monthly),
    ):
        seen: set[str] = set()
        for stem in newest_first:
            key = bucket(stems[stem])
            if key in seen:
                continue
            if len(seen) >= limit:
                break
            seen.add(key)
            keep.add(stem)  # the newest backup in each bucket represents it
    return keep


def plan_prune(names: list[str], policy: Retention = DEFAULT_RETENTION) -> list[str]:
    """Names to delete (only files belonging to a recognised backup set)."""
    stems: dict[str, datetime] = {}
    for name in names:
        parsed = parse_stem(name)
        if parsed is not None:
            stems[parsed[0]] = parsed[1]
    keep = stems_to_keep(stems, policy)
    out = []
    for name in names:
        parsed = parse_stem(name)
        if parsed is not None and parsed[0] not in keep:
            out.append(name)
    return sorted(out)


def prune(
    directory: Path, policy: Retention = DEFAULT_RETENTION, *, dry_run: bool = False
) -> list[str]:
    names = [p.name for p in directory.iterdir() if p.is_file() and not p.is_symlink()]
    doomed = plan_prune(names, policy)
    if not dry_run:
        for name in doomed:
            (directory / name).unlink()
    return doomed


# ---- what the backup directory holds (read by the worker; ADR-019) ---------------------------

SET_FILES = ("dump.age", "config.tar.age", "sha256", "manifest.json")
DRILL_LOG = "restore-drills.jsonl"  # one JSON line per drill; never pruned (not a set file)
WRITING_GRACE = timedelta(minutes=10)  # a set younger than this may still be being written


@dataclass(frozen=True)
class BackupSet:
    stem: str
    created_at: datetime
    problems: tuple[str, ...]  # missing files, sizes that differ from the manifest


@dataclass(frozen=True)
class DrillRecord:
    stem: str
    drilled_at: datetime
    passed: bool
    checks: int | None
    where: str


@dataclass(frozen=True)
class BackupReport:
    directory: str
    sets: int
    newest: BackupSet | None
    drill: DrillRecord | None
    error: str | None = None  # the directory could not be read

    def as_info(self) -> dict[str, Any]:
        """Plain JSON for the worker's heartbeat (no secrets: names, times, sizes)."""
        return {
            "directory": self.directory,
            "sets": self.sets,
            "error": self.error,
            "newest": None
            if self.newest is None
            else {
                "stem": self.newest.stem,
                "created_at": self.newest.created_at.isoformat(),
                "problems": list(self.newest.problems),
            },
            "drill": None
            if self.drill is None
            else {
                "stem": self.drill.stem,
                "drilled_at": self.drill.drilled_at.isoformat(),
                "passed": self.drill.passed,
                "checks": self.drill.checks,
                "where": self.drill.where,
            },
        }


def _problems(directory: Path, stem: str, names: set[str]) -> list[str]:
    problems = [
        f"{stem}.{suffix} is missing" for suffix in SET_FILES if f"{stem}.{suffix}" not in names
    ]
    manifest = directory / f"{stem}.manifest.json"
    if f"{stem}.manifest.json" not in names:
        return problems
    try:
        files = json.loads(manifest.read_text())["files"]
        for f in files:
            path = directory / str(f["name"])
            if path.exists() and path.stat().st_size != int(f["bytes"]):
                problems.append(
                    f"{f['name']} is {path.stat().st_size} bytes; the manifest says {f['bytes']}"
                )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(f"the manifest cannot be read ({type(exc).__name__})")
    return problems


def _last_drill(path: Path) -> DrillRecord | None:
    try:
        lines = [line for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return None
    for line in reversed(lines):  # the newest readable record
        try:
            d = json.loads(line)
            return DrillRecord(
                stem=str(d["stem"]),
                drilled_at=datetime.fromisoformat(str(d["drilled_at"])).astimezone(UTC),
                passed=d["result"] == "PASS",
                checks=int(d["checks"]) if d.get("checks") is not None else None,
                where=str(d.get("where") or "unknown"),
            )
        except (ValueError, KeyError, TypeError):
            continue
    return None


def read_backups(directory: Path, now: datetime) -> BackupReport:
    """The newest backup set (complete: every file present, sizes as its manifest records) and
    the last recorded restore drill. Reads names, sizes and the small manifest only."""
    try:
        names = {p.name for p in directory.iterdir() if p.is_file() and not p.is_symlink()}
    except OSError as exc:
        return BackupReport(str(directory), 0, None, None, f"{type(exc).__name__}: {exc}")
    stems: dict[str, datetime] = {}
    for name in names:
        parsed = parse_stem(name)
        if parsed is not None:
            stems[parsed[0]] = parsed[1]
    newest: BackupSet | None = None
    if stems:
        stem = max(stems, key=stems.__getitem__)
        problems = _problems(directory, stem, names)
        if problems and now - stems[stem] < WRITING_GRACE and len(stems) > 1:
            stem = max((s for s in stems if s != stem), key=stems.__getitem__)  # still writing
            problems = _problems(directory, stem, names)
        newest = BackupSet(stem, stems[stem], tuple(problems))
    return BackupReport(str(directory), len(stems), newest, _last_drill(directory / DRILL_LOG))
