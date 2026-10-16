"""The worker's record of its scheduled jobs: last success, failures in a row, missed schedules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cosmic.ops.job_health import JobBook, schedules_of
from cosmic.worker.registry import Periodic

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def test_failures_count_in_a_row_until_a_success() -> None:
    book = JobBook(T0)
    for i in range(3):
        book.failure("market.ingest_quotes", T0 + timedelta(seconds=i), "ReadTimeout: slow")
    assert book.failing()["market.ingest_quotes"].failures_in_a_row == 3
    book.success("market.ingest_quotes", T0, T0 + timedelta(seconds=2))
    s = book.kinds["market.ingest_quotes"]
    assert book.failing() == {} and s.failures_in_a_row == 0 and s.last_duration_s == 2.0


def test_a_missed_schedule_counts_from_the_last_success_or_the_workers_start() -> None:
    book = JobBook(T0)
    schedules = {"trading.reconcile": 300, "research.backtest": 604_800}
    assert book.overdue(schedules, T0 + timedelta(minutes=16)) == {}  # within 3 × 5 min + grace
    late = book.overdue(schedules, T0 + timedelta(minutes=18))
    assert list(late) == ["trading.reconcile"]  # never succeeded since the start 18 min ago
    book.seed({"trading.reconcile": T0 + timedelta(minutes=17)})  # recorded in the database
    assert book.overdue(schedules, T0 + timedelta(minutes=18)) == {}
    book.seed({"trading.reconcile": T0})  # an older record never replaces a newer one
    assert book.kinds["trading.reconcile"].last_success_at == T0 + timedelta(minutes=17)


def test_the_heartbeat_entry_names_each_state() -> None:
    book = JobBook(T0)
    book.success("a", T0, T0 + timedelta(seconds=1))
    fake_key = "gsk_" + "x" * 30  # built here so the repository's secret scan stays quiet
    for _ in range(3):
        book.failure("b", T0, f"HTTPStatusError: 401 for key {fake_key}")
    info = book.as_info({"a": 60, "b": 60, "c": 60}, T0 + timedelta(minutes=10))
    assert info["a"]["state"] == "OVERDUE"  # its success is 10 min old: more than 3 × 1 min
    assert info["b"]["state"] == "FAILING" and fake_key not in str(info["b"]["last_error"])
    assert info["c"] == {
        "every_s": 60,
        "last_success_at": None,
        "last_duration_s": None,
        "failures_in_a_row": 0,
        "last_error": None,
        "state": "OVERDUE",
    }


def test_schedules_take_the_shortest_interval_of_enabled_ones() -> None:
    periodic = [
        Periodic("market.ingest_quotes", 60, 50, {"source": "a"}),
        Periodic("market.ingest_quotes", 30, 50, {"source": "b"}),
        Periodic("off", 10, 50, {}, lambda _ctx: False),
    ]
    assert schedules_of(periodic, object()) == {"market.ingest_quotes": 30}
