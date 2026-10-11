"""Backup retention (ADR-019): keep 14 daily, 8 weekly and 6 monthly backup sets, plus the
3 most recent sets unconditionally (so a pre-deploy backup survives a later backup the same day).

A backup set is every file sharing a stem ``cosmic-YYYYmmddTHHMMSSZ`` (encrypted dump,
encrypted configuration archive, manifest). Files that do not match the pattern are
never touched, and the newest set is always kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
