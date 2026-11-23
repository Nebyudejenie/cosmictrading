"""The recommendation for the setup now (S117): TAKE only when even the low end of the record's
95 % range clears break-even after the spread; never a recommendation on an untested reward."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from cosmic.manual import advice
from cosmic.manual.insight import NO_TRADE, Insight, Probability

AT = datetime(2026, 11, 23, 13, 0, tzinfo=UTC)


def _idea(p: str, low: str, high: str, *, spread_stop: str = "9.00", no: tuple = ()) -> Insight:  # type: ignore[type-arg]
    entry = D("4000.30")
    return Insight(
        AT,
        "your live Exness account's price",
        NO_TRADE if no else "BUY",
        "BUY",
        "the 15-minute momentum just turned up",
        entry=entry,
        stop=entry - D(spread_stop),
        target=entry + 2 * D(spread_stop),
        rr=D(2),
        probability=Probability(False, D(p), 124, D(low), D(high), "2022-11 to 2026-11", "calm"),
        no_trade=no,
    )


def test_calm_buys_clear_break_even_even_at_the_low_end() -> None:
    a = advice.advise(_idea("0.444", "0.360", "0.532"), D(2), D("0.30"))
    assert a.level == advice.TAKE, a.why
    assert a.breakeven == (1 + D("0.30") / D(9)) / 3  # 34.4 % after a 0.30 spread on a 9.00 stop
    assert "even its low end is above" in a.why and not a.verified


def test_above_break_even_but_not_proven_is_the_owners_call() -> None:
    a = advice.advise(_idea("0.347", "0.269", "0.434"), D(2), D("0.30"))
    assert a.level == advice.YOUR_CALL and "no proven edge" in a.why


def test_below_break_even_a_wide_spread_or_a_reason_to_stand_aside_is_skip() -> None:
    assert advice.advise(_idea("0.306", "0.232", "0.392"), D(2), D("0.30")).level == advice.SKIP
    # 44.4 % but a spread of 3.00 on a 9.00 stop: break-even 44.4 %, not cleared.
    wide = advice.advise(_idea("0.444", "0.360", "0.532"), D(2), D("3.00"))
    assert wide.level in (advice.SKIP, advice.YOUR_CALL) and wide.level != advice.TAKE
    aside = advice.advise(
        _idea("0.444", "0.360", "0.532", no=("under 2 hours to the weekly close",)), D(2), None
    )
    assert aside.level == advice.SKIP and "weekly close" in aside.why


def test_no_setup_and_an_untested_reward() -> None:
    none = Insight(AT, "x", NO_TRADE, None, "the rule sees no setup now")
    assert advice.advise(none, D(2), None).level == advice.SKIP
    untested = advice.advise(_idea("0.444", "0.360", "0.532"), D(3), D("0.30"))
    assert untested.level == advice.UNTESTED and "1:3" in untested.why


async def test_the_alert_job_builds_only_once_after_each_new_candle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S117: the worker also runs the robots, so the page (about 0.2 s of CPU) is built only
    after a new 15-minute candle, never on every 15-second look."""
    from datetime import timedelta
    from typing import Any

    from cosmic.core.clock import FrozenClock
    from cosmic.manual import jobs
    from cosmic.worker.registry import Registry

    candle = {"at": AT}
    built: list[int] = []

    async def newest(_: Any) -> datetime:
        return candle["at"]

    async def watch(*_: Any) -> str | None:
        built.append(1)
        return None

    monkeypatch.setattr(jobs, "newest_candle", newest)
    monkeypatch.setattr(jobs, "setup_watch", watch)
    registry = Registry()
    clock = FrozenClock(AT + timedelta(minutes=15, seconds=5))  # 5 s after the candle closed
    ctx: Any = type("Ctx", (), {"db": None, "clock": clock, "settings": None, "services": {}})()
    jobs.register(registry, ctx)
    handler = registry.handlers["manual.setup_watch"]
    await handler(ctx, {})
    assert built == []  # the robots' moment: nothing built yet
    clock.advance(seconds=20)
    for _ in range(3):
        await handler(ctx, {})
    assert len(built) == 1
    candle["at"] = AT + timedelta(minutes=15)
    clock.advance(minutes=15)
    await handler(ctx, {})
    await handler(ctx, {})
    assert len(built) == 2
