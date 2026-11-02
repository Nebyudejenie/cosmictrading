"""A $100 rehearsal that renews itself (S49): chosen with the owner's code when a run starts, it
continues on the same rule when the run ends, from the money it holds then, never with a lower
floor; a trade it still holds is taken over by the new run. Never after a run that stopped
itself or was revoked, never once the owner stopped it, not while new trades are paused; each
rehearsal on its own; and the database checks every renewal keeps the owner's terms."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, PermissionDeniedError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, notifications
from cosmic.db.schema_execution import trading_mandates
from cosmic.identity.capabilities import Role
from cosmic.trading import mandates, rehearsal
from cosmic.trading.risk.context import PositionState
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user, principal_for
from tests.trading.test_rehearsal import set_binance_fee
from tests.trading_env import Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    e = await build_env(settings, db)
    await set_binance_fee(e)
    return e


async def _start(
    env: Env, *, auto_renew: bool = True, rule: str = "turtle_55_20", venue: str = "binance"
) -> None:
    async with env.db.transaction() as conn:
        await rehearsal.start(
            conn,
            principal=env.principal(),
            broker=env.svc.paper,
            rule=rule,
            now=env.clock.now(),
            settings=env.settings,
            venue=venue,
            auto_renew=auto_renew,
        )


async def _runs(env: Env, venue: str = "binance") -> list[mandates.Mandate]:
    async with env.db.connect() as conn:
        acct = await rehearsal.account(conn, venue)
        assert acct is not None
        found = await mandates.recent_mandates(conn, limit=10, account_id=acct.id)
    return sorted(found, key=lambda m: m.granted_at)


async def _end_of_run(env: Env, venue: str = "binance") -> list[Any]:
    """The run ends at its expiry, and the auto-trader's housekeeping runs."""
    last = (await _runs(env, venue))[-1]
    env.clock.set(max(env.clock.now(), last.expires_at + timedelta(minutes=5)))
    return await _housekeeping(env)


async def _housekeeping(env: Env) -> list[Any]:
    async with env.db.transaction() as conn:
        await mandates.expire_due(conn, env.clock.now())
        return await rehearsal.renew_due(conn, env.clock.now())


async def _told(env: Env, run: mandates.Mandate) -> list[str]:
    async with env.db.connect() as conn:
        return [
            str(t)
            for (t,) in await conn.execute(
                select(notifications.c.payload["text"].astext).where(
                    notifications.c.dedup_key.like(f"mandate:{run.id}:not_renewed%"),
                    notifications.c.channel == "PORTAL",
                )
            )
        ]


def _hold(monkeypatch: pytest.MonkeyPatch, *, equity: str, worst: str | None) -> None:
    """The rehearsal holds a trade worth its equity's mark, its worst case at its stop given."""
    real = rehearsal.account_state

    async def holding(*a: object, **k: object) -> object:
        s = await real(*a, **k)  # type: ignore[arg-type]
        return replace(
            s,
            positions=(PositionState(rehearsal.MARKET, D("0.01"), D(4000)),),
            equity=D(equity),
        )

    monkeypatch.setattr(rehearsal, "account_state", holding)
    if worst is not None:
        monkeypatch.setattr(rehearsal.rooms, "open_risk", lambda _s: D(worst))


async def test_a_run_renews_itself_once_on_the_same_rule_and_floor(env: Env) -> None:
    await _start(env)
    first = (await _runs(env))[0]
    assert first.auto_renew and first.renews is None
    renewed = await _end_of_run(env)
    assert len(renewed) == 1
    runs = await _runs(env)
    assert [m.status for m in runs] == ["EXPIRED", "ACTIVE"]
    new = runs[1]
    assert (new.renews, new.auto_renew, new.granted_by) == (first.id, True, first.granted_by)
    assert (new.strategy_key, new.timeframe, new.instruments) == (
        first.strategy_key,
        first.timeframe,
        first.instruments,
    )
    assert new.capital is not None and new.total_loss_limit is not None
    assert new.capital - new.total_loss_limit == rehearsal.FLOOR  # the same floor of $80
    assert new.expires_at - new.granted_at == timedelta(days=rehearsal.DAYS)
    async with env.db.transaction() as conn:  # the next housekeeping: nothing more
        assert await rehearsal.renew_due(conn, env.clock.now()) == []
        actions = [
            r.action
            for r in await conn.execute(
                select(audit_events.c.action).where(audit_events.c.entity_id == str(new.id))
            )
        ]
        told = await conn.scalar(
            select(notifications.c.payload["text"].astext).where(
                notifications.c.dedup_key.like(f"mandate:{new.id}:granted%"),
                notifications.c.channel == "PORTAL",
            )
        )
    assert actions == ["mandate.renewed"]
    assert told is not None and "renewed itself, as you chose when you started it" in told
    assert (await _end_of_run(env)) != []  # and again at the next end


async def test_a_trade_still_open_is_taken_over_with_its_worst_case_kept_apart(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _start(env)
    _hold(monkeypatch, equity="103.2567", worst="3.404")
    assert len(await _end_of_run(env)) == 1
    new = (await _runs(env))[-1]
    assert new.capital == D("103.25")  # rounded down
    # its worst case rounded up and kept apart: the floor is 80 plus it, never lower
    assert new.total_loss_limit == D("103.25") - 80 - D("3.41")
    assert new.capital - new.total_loss_limit > rehearsal.FLOOR


async def test_at_its_floor_it_is_not_renewed_and_the_owner_is_told_once(
    env: Env, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _start(env)
    run = (await _runs(env))[0]
    _hold(monkeypatch, equity="81.00", worst="0")
    assert await _end_of_run(env) == []
    env.clock.advance(minutes=5)
    assert await _housekeeping(env) == []  # tried again: told only once
    told = await _told(env, run)
    assert len(told) == 1 and "could not renew itself" in told[0]
    assert "its floor of 80.00 leaves no room for another trade" in told[0]
    async with env.db.connect() as conn:
        why = await rehearsal.renewal_blocker(conn, "binance", env.clock.now())
    assert why is not None and why.startswith("it needs you: the rehearsal holds 81.00")
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/rehearsal")).text
    assert "Its run has ended; it needs you: the rehearsal holds 81.00" in html


async def test_an_entry_on_its_way_waits_quietly_and_paused_trading_waits_too(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _start(env)
    run = (await _runs(env))[0]
    in_flight = {"yes": True}

    async def entry_in_flight(_conn: Any, _account_id: Any) -> bool:
        return in_flight["yes"]

    monkeypatch.setattr(rehearsal, "_entry_in_flight", entry_in_flight)
    assert await _end_of_run(env) == []  # it renews by itself once the entry has settled
    await env.svc.emergency_stop(
        actor=Actor.user(env.owner.user_id), reason="test emergency", principal=env.principal()
    )
    in_flight["yes"] = False
    assert await _housekeeping(env) == []  # new trades are paused
    async with env.db.connect() as conn:
        why = await rehearsal.renewal_blocker(conn, "binance", env.clock.now())
    assert why is not None and why.startswith("new trades are paused (mode EMERGENCY_STOP")
    assert await _told(env, run) == []  # waiting is not news


async def test_one_rehearsal_never_undoes_the_others_renewal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_binance_fee(env, cent=True)
    await _start(env)
    await _start(env, venue="hfm-cent")
    binance = (await _runs(env))[0]
    real = mandates.renew

    async def renew(conn: Any, prev: Any, terms: Any, now: Any) -> Any:
        if prev.id == binance.id:
            raise RuntimeError("a fault in one rehearsal")
        return await real(conn, prev, terms, now)

    monkeypatch.setattr(mandates, "renew", renew)
    later = max(m.expires_at for m in [*await _runs(env), *await _runs(env, "hfm-cent")])
    env.clock.set(later + timedelta(minutes=5))
    renewed = await _housekeeping(env)
    assert len(renewed) == 1
    assert (await _runs(env, "hfm-cent"))[-1].status == "ACTIVE"
    told = await _told(env, binance)
    assert len(told) == 1 and "could not renew itself: an internal error (logged)" in told[0]


async def test_never_after_a_run_that_stopped_itself_or_was_revoked(env: Env) -> None:
    await _start(env)
    async with env.db.transaction() as conn:
        await mandates.end(
            conn,
            mandate_id=(await _runs(env))[0].id,
            status="STOPPED",
            by=Actor.system("mandate"),
            reason="its floor",
            now=env.clock.now(),
        )
        assert await rehearsal.renew_due(conn, env.clock.now()) == []


async def test_never_after_the_owner_revoked_the_run(env: Env) -> None:
    await _start(env)
    async with env.db.transaction() as conn:
        await mandates.revoke(
            conn,
            principal=env.principal(),
            mandate_id=(await _runs(env))[0].id,
            reason="stopping it",
            now=env.clock.now(),
        )
        assert await rehearsal.renew_due(conn, env.clock.now()) == []


async def test_no_renewal_unless_chosen(env: Env) -> None:
    await _start(env, auto_renew=False)
    assert await _end_of_run(env) == []
    async with env.db.transaction() as conn:
        with pytest.raises(ConflictError, match="only a run that ended at its expiry"):
            await mandates.renew(
                conn,
                (await _runs(env))[-1],
                rehearsal.run_terms(rehearsal.FIRST, D(100), "turtle_55_20", True),
                env.clock.now(),
            )


async def test_stop_renewing_needs_no_fresh_code_and_reaches_a_run_just_renewed(
    env: Env,
) -> None:
    await _start(env)
    old = (await _runs(env))[0]
    viewer = await make_user(env.db, env.clock, username="viewer", roles=[Role.VIEWER])
    async with env.db.transaction() as conn:
        with pytest.raises(PermissionDeniedError):
            await mandates.stop_renewing(
                conn,
                principal=principal_for(viewer, env.clock, roles=("VIEWER",)),
                mandate_id=old.id,
                reason="not mine",
                now=env.clock.now(),
            )
    assert len(await _end_of_run(env)) == 1  # it renewed itself before the owner's stop landed
    new = (await _runs(env))[-1]
    async with env.db.transaction() as conn:
        stopped = await mandates.stop_renewing(
            conn,
            principal=env.principal(mfa_fresh=False),  # removing authority: no fresh code
            mandate_id=old.id,
            reason="I will choose the next rule myself",
            now=env.clock.now(),
        )
        with pytest.raises(ConflictError, match="already stopped"):
            await mandates.stop_renewing(
                conn,
                principal=env.principal(),
                mandate_id=new.id,
                reason="again",
                now=env.clock.now(),
            )
    assert stopped == new.id  # the stop reached the run that continues it
    assert (await _runs(env))[-1].status == "ACTIVE"  # the run itself goes on
    assert await _end_of_run(env) == []
    async with env.db.transaction() as conn:  # the database refuses it too
        with pytest.raises(DBAPIError, match="keeps the owner's terms"):
            await conn.execute(trading_mandates.insert().values(id=uuid7(), **_renewal(new, env)))


async def test_the_database_keeps_a_renewal_to_the_owners_terms(env: Env) -> None:
    await _start(env)
    first = (await _runs(env))[0]
    env.clock.set(first.expires_at + timedelta(minutes=5))
    async with env.db.transaction() as conn:
        await mandates.expire_due(conn, env.clock.now())
    base = _renewal(first, env)
    for change in (
        {"strategy_key": "ema_200_trend"},  # another rule
        {"timeframe": "1h"},
        {"instruments": ["CFD:XAU/USD@HFM_CENT"]},
        {"mode": "ASK"},
        {"risk_pct": D("1")},
        {"max_trades_per_day": first.max_trades_per_day + 1},
        {"max_open_positions": first.max_open_positions + 1},
        {"capital": None},
        {"total_loss_limit": D(30)},  # a lower floor
        {"max_loss_per_trade": D("9.00")},  # more than the care level allows
        {"daily_loss_limit": D("50.00")},  # a looser daily stop for its loss per trade
        {"expires_at": env.clock.now() + timedelta(days=91)},  # a longer run
        {"auto_renew": False},  # a renewal keeps renewing itself, as chosen
    ):
        async with env.db.transaction() as conn:
            with pytest.raises(DBAPIError, match="keeps the owner's terms"):
                await conn.execute(trading_mandates.insert().values(id=uuid7(), **(base | change)))
    async with env.db.transaction() as conn:  # the owner's terms: accepted, once
        await conn.execute(trading_mandates.insert().values(id=uuid7(), **base))
    async with env.db.transaction() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(
                trading_mandates.insert().values(id=uuid7(), **(base | {"status": "REVOKED"}))
            )


async def test_the_database_continues_only_the_latest_run(env: Env) -> None:
    await _start(env)
    first = (await _runs(env))[0]
    env.clock.set(first.expires_at + timedelta(minutes=5))
    async with env.db.transaction() as conn:
        await mandates.expire_due(conn, env.clock.now())
    await _start(env, auto_renew=False)  # the owner started a new run by hand
    async with env.db.transaction() as conn:
        with pytest.raises(DBAPIError, match="continues only the latest run"):
            await conn.execute(
                trading_mandates.insert().values(
                    id=uuid7(), **(_renewal(first, env) | {"status": "REVOKED"})
                )
            )


async def test_only_a_rehearsal_renews_itself(env: Env) -> None:
    terms = mandates.terms_from_money(
        account_alias="paper-main",
        money=D(1000),
        care="BOLD",
        mode="AUTO",
        instruments=(rehearsal.MARKET,),
        strategy_key="turtle_55_20",
        timeframe=mandates.DAILY,
        days=90,
    ).model_copy(update={"auto_renew": True})
    async with env.db.transaction() as conn:
        with pytest.raises(ConflictError, match="only a \\$100 rehearsal renews itself"):
            await mandates.grant(
                conn,
                principal=env.principal(),
                terms=terms,
                now=env.clock.now(),
                settings=env.settings,
            )


def _renewal(prev: mandates.Mandate, env: Env) -> dict[str, Any]:
    """A renewal of ``prev`` row by row, on the owner's terms, as the database sees it."""
    return {
        "account_id": prev.account_id,
        "strategy_key": prev.strategy_key,
        "timeframe": prev.timeframe,
        "instruments": list(prev.instruments),
        "max_loss_per_trade": prev.max_loss_per_trade,
        "max_trades_per_day": prev.max_trades_per_day,
        "max_open_positions": prev.max_open_positions,
        "daily_loss_limit": prev.daily_loss_limit,
        "status": "ACTIVE",
        "granted_by": prev.granted_by,
        "granted_at": env.clock.now(),
        "expires_at": env.clock.now() + timedelta(days=90),
        "capital": prev.capital,
        "risk_pct": prev.risk_pct,
        "mode": prev.mode,
        "total_loss_limit": prev.total_loss_limit,
        "auto_renew": True,
        "renews": prev.id,
    }


async def test_the_page_offers_it_and_the_owner_stops_it(env: Env, settings: Settings) -> None:
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/rehearsal")).text
        assert 'name="auto_renew" value="yes"' in html
        r = await c.post(
            "/rehearsal/start",
            data={
                "csrf_token": csrf_from(html),
                "venue": "binance",
                "rule": "turtle_55_20",
                "auto_renew": "yes",
            },
        )
        assert r.status_code == 303
        html = (await c.get("/rehearsal")).text
        assert "then renews itself on the same rule" in html
        r = await c.post(
            "/rehearsal/stop-renewing", data={"csrf_token": csrf_from(html), "venue": "binance"}
        )
        assert r.status_code == 303
        html = (await c.get("/rehearsal")).text
        assert "then renews itself" not in html and "Stop renewing" not in html
    run = (await _runs(env))[0]
    assert run.auto_renew and run.status == "ACTIVE"
    async with env.db.connect() as conn:
        assert await mandates.renewal_stopped(conn, run.id)


async def test_the_reminder_says_what_happens_at_the_end(env: Env) -> None:
    await _start(env)
    run = (await _runs(env))[0]
    env.clock.set(run.expires_at - timedelta(hours=12))
    async with env.db.transaction() as conn:
        assert await mandates.remind_renewals(conn, env.clock.now()) == 1
        text = await conn.scalar(
            select(notifications.c.payload["text"].astext).where(
                notifications.c.dedup_key.like(f"mandate:{run.id}:renew:1%"),
                notifications.c.channel == "PORTAL",
            )
        )
    assert text is not None and "then renews itself, as you chose" in text
    assert "a trade it still holds is taken over by the new run" in text
