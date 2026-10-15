"""What the worker sees in the backup directory, and when it alerts (ADR-019)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cosmic.ops.backup_check import backup_alerts
from cosmic.ops.backups import BackupReport, read_backups
from tests.backup_files import make_set
from tests.backup_files import record_drill as drill

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)


def test_a_fresh_complete_set_and_a_recent_drill_raise_nothing(tmp_path: Path) -> None:
    make_set(tmp_path, NOW - timedelta(days=1))
    stem = make_set(tmp_path, NOW - timedelta(hours=3))
    drill(tmp_path, stem, NOW - timedelta(days=1))
    report = read_backups(tmp_path, NOW)
    assert report.sets == 2 and report.newest is not None and report.newest.stem == stem
    assert report.newest.problems == () and report.drill is not None and report.drill.passed
    assert backup_alerts(report, NOW) == {}
    info = report.as_info()
    assert info["newest"]["stem"] == stem and info["drill"]["checks"] == 10
    json.dumps(info)  # plain JSON for the heartbeat


def test_late_backups_warn_then_turn_critical(tmp_path: Path) -> None:
    stem = make_set(tmp_path, NOW - timedelta(hours=30))
    drill(tmp_path, stem, NOW - timedelta(days=1))
    late = backup_alerts(read_backups(tmp_path, NOW), NOW)
    assert list(late) == ["ops.backup_overdue"] and late["ops.backup_overdue"].severity == "WARNING"
    later = backup_alerts(
        read_backups(tmp_path, NOW + timedelta(hours=24)), NOW + timedelta(hours=24)
    )
    assert later["ops.backup_overdue"].severity == "CRITICAL"


def test_missing_files_and_wrong_sizes_are_incomplete(tmp_path: Path) -> None:
    stem = make_set(tmp_path, NOW - timedelta(hours=2), skip="config.tar.age")
    report = read_backups(tmp_path, NOW)
    assert report.newest is not None and report.newest.problems == (
        f"{stem}.config.tar.age is missing",
    )
    assert backup_alerts(report, NOW)["ops.backup_incomplete"].severity == "CRITICAL"
    for f in tmp_path.iterdir():
        f.unlink()
    make_set(tmp_path, NOW - timedelta(hours=2), lie=True)
    report = read_backups(tmp_path, NOW)
    assert report.newest is not None and "the manifest says" in report.newest.problems[0]


def test_a_set_still_being_written_is_not_yet_judged(tmp_path: Path) -> None:
    older = make_set(tmp_path, NOW - timedelta(hours=20))
    make_set(tmp_path, NOW - timedelta(minutes=1), skip="manifest.json")  # moving into place
    report = read_backups(tmp_path, NOW)
    assert report.newest is not None and report.newest.stem == older and not report.newest.problems


def test_drills_are_due_monthly_and_a_failed_one_is_critical(tmp_path: Path) -> None:
    stem = make_set(tmp_path, NOW - timedelta(hours=2))
    never = backup_alerts(read_backups(tmp_path, NOW), NOW)
    assert never["ops.restore_drill_due"].severity == "WARNING"
    drill(tmp_path, stem, NOW - timedelta(days=40))
    assert "ops.restore_drill_due" in backup_alerts(read_backups(tmp_path, NOW), NOW)
    drill(tmp_path, stem, NOW - timedelta(hours=1), result="FAIL")
    failed = backup_alerts(read_backups(tmp_path, NOW), NOW)["ops.restore_drill_due"]
    assert failed.severity == "CRITICAL" and "FAILED" in failed.title
    with (tmp_path / "restore-drills.jsonl").open("a") as f:
        f.write("not json\n")  # an unreadable line does not hide the last good record
    assert read_backups(tmp_path, NOW).drill is not None


def test_no_directory_or_no_sets_is_critical(tmp_path: Path) -> None:
    gone = read_backups(tmp_path / "absent", NOW)
    assert gone.error is not None and gone.newest is None
    assert backup_alerts(gone, NOW)["ops.backup_overdue"].severity == "CRITICAL"
    empty = backup_alerts(BackupReport(str(tmp_path), 0, None, None), NOW)
    assert empty["ops.backup_overdue"].title == "No backup found"
