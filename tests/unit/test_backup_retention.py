from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cosmic.ops.backups import Retention, plan_prune, prune


def names_for(ts: datetime) -> list[str]:
    stem = "cosmic-" + ts.strftime("%Y%m%dT%H%M%SZ")
    return [f"{stem}.dump.age", f"{stem}.config.tar.age", f"{stem}.manifest.json"]


def test_daily_weekly_monthly_buckets() -> None:
    start = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    stamps = [start + timedelta(days=i) for i in range(400)]
    names = [n for ts in stamps for n in names_for(ts)]
    doomed = set(plan_prune(names))
    kept = sorted({n.split(".")[0] for n in names if n not in doomed})
    # 14 newest days + newest of 8 weeks + newest of 6 months, with overlaps collapsed
    newest = stamps[-1]
    assert "cosmic-" + newest.strftime("%Y%m%dT%H%M%SZ") in kept
    days = {k[7:15] for k in kept}
    assert len([d for d in days if d >= (newest - timedelta(days=13)).strftime("%Y%m%d")]) == 14
    assert len({k[7:13] for k in kept}) == 6  # six distinct months represented
    assert 14 <= len(kept) <= 14 + 8 + 6
    # every file of a kept set is kept; every file of a deleted set is deleted
    for stem in kept:
        assert not any(n.startswith(stem) for n in doomed)


def test_never_touches_unrecognised_files_and_keeps_newest() -> None:
    names = ["notes.txt", "cosmic-latest.dump.age", "cosmic-20261332T000000Z.dump.age"]
    names += names_for(datetime(2026, 10, 11, 2, 30, tzinfo=UTC))
    assert plan_prune(names, Retention(daily=0, weekly=0, monthly=0, recent=0)) == []


def test_multiple_backups_in_one_day_keep_only_the_newest_for_that_day() -> None:
    day = datetime(2026, 10, 11, tzinfo=UTC)
    names = names_for(day.replace(hour=2)) + names_for(day.replace(hour=14))
    doomed = plan_prune(names, Retention(daily=14, weekly=0, monthly=0, recent=1))
    assert doomed == sorted(names_for(day.replace(hour=2)))


def test_pre_deploy_backup_survives_a_later_backup_the_same_day() -> None:
    day = datetime(2026, 10, 11, tzinfo=UTC)
    pre_deploy = names_for(day.replace(hour=1))
    nightly = names_for(day.replace(hour=2, minute=30))
    assert plan_prune(pre_deploy + nightly) == []  # default keeps the 3 newest sets


def test_prune_deletes_on_disk(tmp_path: Path) -> None:
    for i in range(20):
        for n in names_for(datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=i)):
            (tmp_path / n).write_text("x")
    (tmp_path / "keep-me.txt").write_text("x")
    policy = Retention(daily=3, weekly=0, monthly=0, recent=1)
    planned = prune(tmp_path, policy, dry_run=True)
    assert len(list(tmp_path.iterdir())) == 61
    assert prune(tmp_path, policy) == planned
    assert len(list(tmp_path.iterdir())) == 3 * 3 + 1
