from __future__ import annotations

from cosmic.worker.main import Watchdog


def test_watchdog_exits_only_when_the_loop_stalls() -> None:
    now = [1000.0]
    exits: list[int] = []
    wd = Watchdog(120, exit_fn=exits.append, clock=lambda: now[0])
    now[0] += 119
    assert wd.check() is False
    wd.progress()
    now[0] += 120
    assert wd.check() is False and exits == []  # exactly at the limit is still fine
    now[0] += 1
    assert wd.check() is True and exits == [70]  # stalled: exit so Docker restarts it
