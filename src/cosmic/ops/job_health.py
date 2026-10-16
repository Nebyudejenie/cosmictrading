"""What the worker knows about its scheduled jobs (the owner's directive, Phase 8).

For each kind: the last success and how long it took, and the failures in a row. The worker
records every run; the book is published in its heartbeat (Operations shows it). Two rules:
- **missed schedule:** no success within three intervals (counted from the worker's start
  when the kind has not succeeded since);
- **repeated failure:** three failures in a row.

Error texts are scrubbed before they are kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.core.redaction import scrub_text
from cosmic.db.schema import jobs

OVERDUE_INTERVALS = 3
GRACE = timedelta(minutes=2)  # a busy queue may start a job a little late
FAILING_AFTER = 3


@dataclass
class KindStats:
    last_success_at: datetime | None = None
    last_duration_s: float | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    failures_in_a_row: int = 0


class JobBook:
    def __init__(self, started_at: datetime) -> None:
        self.started_at = started_at
        self.kinds: dict[str, KindStats] = {}

    def _stats(self, kind: str) -> KindStats:
        return self.kinds.setdefault(kind, KindStats())

    def success(self, kind: str, started: datetime, finished: datetime) -> None:
        s = self._stats(kind)
        s.last_success_at = finished
        s.last_duration_s = max(0.0, (finished - started).total_seconds())
        s.failures_in_a_row = 0

    def failure(self, kind: str, at: datetime, error: str) -> None:
        s = self._stats(kind)
        s.last_failure_at = at
        s.last_error = scrub_text(error)[:200]
        s.failures_in_a_row += 1

    def seed(self, last_success: Mapping[str, datetime]) -> None:
        """Last successes recorded before this worker started (durations are not known)."""
        for kind, at in last_success.items():
            s = self._stats(kind)
            if s.last_success_at is None or at > s.last_success_at:
                s.last_success_at = at

    def overdue(self, schedules: Mapping[str, int], now: datetime) -> dict[str, timedelta]:
        """Kinds without a success within three intervals: kind → time since the last one."""
        out: dict[str, timedelta] = {}
        for kind, every in schedules.items():
            since = self.kinds.get(kind, KindStats()).last_success_at or self.started_at
            if now - since > timedelta(seconds=every * OVERDUE_INTERVALS) + GRACE:
                out[kind] = now - since
        return out

    def failing(self) -> dict[str, KindStats]:
        return {k: s for k, s in self.kinds.items() if s.failures_in_a_row >= FAILING_AFTER}

    def as_info(self, schedules: Mapping[str, int], now: datetime) -> dict[str, Any]:
        """Plain JSON for the heartbeat: one entry per scheduled kind."""
        late = self.overdue(schedules, now)
        out: dict[str, Any] = {}
        for kind, every in sorted(schedules.items()):
            s = self.kinds.get(kind, KindStats())
            out[kind] = {
                "every_s": every,
                "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
                "last_duration_s": s.last_duration_s,
                "failures_in_a_row": s.failures_in_a_row,
                "last_error": s.last_error,
                "state": (
                    "FAILING"
                    if s.failures_in_a_row >= FAILING_AFTER
                    else ("OVERDUE" if kind in late else "OK")
                ),
            }
        return out


def schedules_of(periodic: Any, ctx: Any) -> dict[str, int]:
    """Each enabled scheduled kind and its shortest interval (several schedules may share one)."""
    out: dict[str, int] = {}
    for p in periodic:
        if p.enabled(ctx):
            out[p.kind] = min(out.get(p.kind, p.interval_seconds), p.interval_seconds)
    return out


async def last_successes(conn: AsyncConnection, kinds: list[str]) -> dict[str, datetime]:
    rows = await conn.execute(
        select(jobs.c.kind, func.max(jobs.c.finished_at).label("at"))
        .where(jobs.c.status == "DONE", jobs.c.kind.in_(kinds))
        .group_by(jobs.c.kind)
    )
    return {r.kind: r.at for r in rows if r.at is not None}
