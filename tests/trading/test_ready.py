"""Is the simulated gold auto-trader ready? (S52): the home page's first panel walks the owner
from "fees and markets not set" to "ready to start" to "running", one next step at a time. It
judges as the places that decide do: the worker's own heartbeat, the mode, the account-level
risk checks, the auto-trader's daily bars, the quotes and the HFM bridge, and the runs as Start
with $100 sorts them. HFM's daily break and weekend are not problems; a silent bridge is."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cosmic.audit.service import Actor
from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import worker_heartbeats
from cosmic.db.schema_market import fetch_runs, instruments, quotes, sources
from cosmic.db.schema_trading import bridge_state, equity_snapshots
from cosmic.identity.capabilities import Role
from cosmic.notify.alerts import Alert
from cosmic.ops.incidents import Category, raise_incident
from cosmic.reporting import ready
from cosmic.trading import mandates, rehearsal
from cosmic.trading.accounts import set_reconciliation_status
from cosmic.trading.modes import DESCRIPTIONS, Mode
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login, make_user
from tests.trading.test_rehearsal import set_binance_fee
from tests.trading_env import Env, build_env

PAXG = "SPOT:PAXG/USDT@BINANCE"
CENT = "CFD:XAU/USD@HFM_CENT"
BINANCE = rehearsal.REHEARSALS["binance"].name
HFM = rehearsal.REHEARSALS["hfm-cent"].name
WED = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)  # 08:00 in New York (summer time)
OFFSET = 7_200_000  # the broker's server time, two hours ahead of UTC


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def _history(env: Env, days: int, last: datetime | None = None) -> None:
    """``days`` completed daily PAXG bars on Binance, the newest opened ``last`` (yesterday)."""
    last = last or mandates.day_start(env.clock.now()) - timedelta(days=1)
    now = env.clock.now()
    async with env.db.transaction() as conn:
        inst = await conn.scalar(
            select(instruments.c.id).where(instruments.c.canonical_key == PAXG)
        )
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == "binance_public"))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run,
                source_id=src,
                kind="test_bars",
                started_at=now,
                finished_at=now,
                status="SUCCESS",
            )
        )
        await conn.execute(
            text(
                "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, low, "
                "close, volume, is_complete, fetch_run_id, ingested_at, updated_at) VALUES (:i, :s, "
                "'1d', :t, 4000, 4010, 3990, 4005, 1, true, :r, :n, :n) ON CONFLICT DO NOTHING"
            ),
            [
                {"i": inst, "s": src, "t": last - timedelta(days=k), "r": run, "n": now}
                for k in range(days)
            ],
        )


async def _quote(env: Env, key: str, source: str, at: datetime) -> None:
    async with env.db.transaction() as conn:
        inst = await conn.scalar(select(instruments.c.id).where(instruments.c.canonical_key == key))
        src = await conn.scalar(select(sources.c.id).where(sources.c.key == source))
        run = uuid7()
        await conn.execute(
            fetch_runs.insert().values(
                id=run,
                source_id=src,
                kind="test_quote",
                started_at=at,
                finished_at=at,
                status="SUCCESS",
            )
        )
        await conn.execute(
            pg_insert(quotes)
            .values(
                id=uuid7(),
                instrument_id=inst,
                source_id=src,
                bid=Decimal("4100.00"),
                ask=Decimal("4100.50"),
                last=Decimal("4100.00"),
                source_ts=at,
                ingested_at=at,
                fetch_run_id=run,
            )
            .on_conflict_do_nothing()
        )


async def _bridge(
    env: Env, at: datetime, *, tick: datetime | None = None, connected: bool = True
) -> None:
    """The HFM bridge's report received at ``at``, with HFM's newest gold tick at ``tick``."""
    snapshot: dict[str, Any] = {"terminal": {"connected": connected}}
    if tick is not None:
        snapshot["server_utc_offset_ms"] = OFFSET
        snapshot["ticks"] = {"XAUUSD": {"time_msc": int(tick.timestamp() * 1000) + OFFSET}}
    async with env.db.transaction() as conn:
        await conn.execute(
            pg_insert(bridge_state)
            .values(bridge="hfm-demo", received_at=at, snapshot=snapshot)
            .on_conflict_do_update(
                index_elements=[bridge_state.c.bridge],
                set_={"received_at": at, "snapshot": snapshot},
            )
        )


async def _beat(env: Env, process: str = "worker") -> None:
    now = env.clock.now()
    async with env.db.transaction() as conn:
        await conn.execute(
            pg_insert(worker_heartbeats)
            .values(worker_id=f"test-{process}", process=process, started_at=now, last_beat_at=now)
            .on_conflict_do_update(
                index_elements=[worker_heartbeats.c.worker_id], set_={"last_beat_at": now}
            )
        )


async def _feeds(env: Env) -> None:
    """Everything arriving now: the worker, Binance's and HFM's prices, the bridge."""
    now = env.clock.now()
    await _beat(env)
    await _quote(env, PAXG, "binance_public", now)
    await _quote(env, CENT, "hfm_cent_terms", now)
    await _bridge(env, now, tick=now)


async def _ready(env: Env) -> ready.Ready:
    async with env.db.connect() as conn:
        return await ready.build(conn, env.clock.now())


async def _start(env: Env, venue: str, *, reconcile: bool = True) -> None:
    """Start it as Start with $100 does; the worker then reconciles a new account at once."""
    async with env.db.transaction() as conn:
        await rehearsal.start(
            conn,
            principal=env.principal(),
            broker=env.svc.paper,
            rule="turtle_55_20",
            now=env.clock.now(),
            settings=env.settings,
            venue=venue,
            auto_renew=True,
        )
    if reconcile:
        await env.svc.reconcile_all(trigger="test")


async def _end(env: Env, venue: str, status: str) -> None:
    async with env.db.transaction() as conn:
        acct = await rehearsal.account(conn, venue)
        assert acct is not None
        (m,) = await mandates.recent_mandates(conn, limit=1, account_id=acct.id)
        await mandates.end(
            conn,
            mandate_id=m.id,
            status=status,
            by=mandates.ACTOR,
            reason="test",
            now=env.clock.now(),
        )


async def _ready_env(env: Env) -> None:
    """A weekday with a year of daily gold, the fees and markets set, everything arriving."""
    env.clock.set(WED)
    await _history(env, ready.MIN_DAILY)
    await set_binance_fee(env, cent=True)
    await _feeds(env)


def _line(r: ready.Ready, start: str) -> ready.Check:
    (c,) = [c for c in r.checks if c.text.startswith(start)]
    return c


async def test_from_nothing_set_to_ready_to_start_to_running(env: Env) -> None:
    env.clock.set(WED)
    await _history(env, ready.MIN_DAILY)
    await _feeds(env)
    r = await _ready(env)
    assert r.status == "NEEDS YOU" and r.next_link == "/rehearsal#ready-title"
    assert r.next_label == "Get them ready" and "Get them ready in one step" in r.next_step
    assert r.checks[0] == ready.Check(
        True, "The system is running and may trade simulated money", "system"
    )
    assert [c.ok for c in r.checks if c.kind == "price"] == [True, True]
    assert _line(r, f"{BINANCE}: its fee").text.endswith("its fee is not set")
    assert [c.ok for c in r.checks if c.text.endswith("not started yet")] == [None, None]
    await set_binance_fee(env, cent=True)  # what "Get them ready" applies
    r = await _ready(env)
    assert r.status == "READY TO START" and r.next_link == "/rehearsal#go-title"
    assert r.next_label == "Start them" and "tick “Renew it by itself”" in r.next_step
    assert f"start {BINANCE} and {HFM}:" in r.next_step
    await _start(env, "binance", reconcile=False)
    r = await _ready(env)  # not refused while the worker reconciles its new account
    assert r.status == "READY TO START" and _line(r, f"{BINANCE}: started").ok is None
    await env.svc.reconcile_all(trigger="test")
    r = await _ready(env)
    assert r.status == "READY TO START"  # the other one is still to start, and only it is named
    assert f"start {HFM}:" in r.next_step and BINANCE not in r.next_step
    (running,) = r.runs
    assert running.renews and running.rule == "Turtle system 2 (55/20 breakout)"
    assert running.money == Decimal(100) and running.currency == "USDT"
    assert running.until == WED + timedelta(days=rehearsal.DAYS)
    assert _line(r, f"{BINANCE}: running").text.endswith(
        "running, rule Turtle system 2 (55/20 breakout), renews itself"
    )
    await _start(env, "hfm-cent")
    r = await _ready(env)
    assert r.status == "RUNNING" and r.next_link == "/rehearsal"
    assert r.next_label == "See the rehearsals" and r.next_step.startswith("Nothing to do")
    assert all(c.ok for c in r.checks) and len(r.runs) == 2


async def test_the_system_itself_comes_first(env: Env) -> None:
    await _ready_env(env)
    async with env.db.transaction() as conn:
        await conn.execute(worker_heartbeats.delete())
    await _beat(env, "bot")  # the Telegram bot's heartbeat is not the worker's
    r = await _ready(env)
    assert r.status == "PROBLEM" and r.next_link == "/ops" and "worker" in r.next_step
    assert r.checks[0].ok is False and r.checks[0].text.startswith("The worker")
    await _beat(env)
    assert (await _ready(env)).status == "READY TO START"
    env.clock.advance(seconds=61)  # as the home page's attention list: a minute without a beat
    assert (await _ready(env)).status == "PROBLEM"
    await _feeds(env)
    await env.svc.emergency_stop(
        actor=Actor.user(env.owner.user_id), reason="test emergency", principal=env.principal()
    )
    r = await _ready(env)
    assert r.status == "NEEDS YOU" and r.next_link == "/ops"
    assert "recovery checklist" in r.next_step
    assert r.checks[0].text == (
        "New trades are not allowed now: the mode is EMERGENCY_STOP. "
        + DESCRIPTIONS[Mode.EMERGENCY_STOP]
    )


async def test_a_run_the_risk_checks_refuse_is_not_shown_running(env: Env) -> None:
    await _ready_env(env)
    await _start(env, "binance")
    await _start(env, "hfm-cent")
    assert (await _ready(env)).status == "RUNNING"
    # R08: a best of 120 puts the 100 now 16.67 % below it, past the policy's 10 %
    async with env.db.transaction() as conn:
        acct = await rehearsal.account(conn, "binance")
        assert acct is not None
        await conn.execute(
            equity_snapshots.insert().values(
                id=uuid7(),
                account_id=acct.id,
                taken_at=WED - timedelta(hours=1),
                equity=Decimal(120),
                cash=Decimal(120),
                positions_value=Decimal(0),
                price_label="DELAYED",
                is_simulated=True,
            )
        )
    r = await _ready(env)
    assert r.status == "NEEDS YOU" and r.next_link == "/settings/risk"
    assert f"refuses new trades on {BINANCE}" in r.next_step
    c = _line(r, f"{BINANCE}: running, but")
    assert c.ok is False and "drawdown 16.67% reached limit" in c.text and c.text.endswith("(R08)")
    assert _line(r, f"{HFM}: running").ok  # the other account is not refused
    # R03: a failed reconciliation refuses the HFM side too, and Operations comes first
    async with env.db.transaction() as conn:
        acct = await rehearsal.account(conn, "hfm-cent")
        assert acct is not None
        await set_reconciliation_status(
            conn, account_id=acct.id, status="FAILED", now=env.clock.now()
        )
    r = await _ready(env)
    assert r.status == "NEEDS YOU" and r.next_link == "/ops" and HFM in r.next_step
    c = _line(r, f"{HFM}: running, but")
    assert c.ok is False and "reconciliation is FAILED" in c.text and c.text.endswith("(R03)")
    # R15: an open critical security incident refuses every new trade, on every account
    async with env.db.transaction() as conn:
        await raise_incident(
            conn,
            dedup_key="test.security",
            category=Category.SECURITY,
            alert=Alert(
                title="test",
                severity="CRITICAL",
                what_happened="a test",
                verified="a test",
                uncertain="none",
                risk_now="none",
                action_taken="none",
                owner_action="none",
            ),
            now=env.clock.now(),
        )
    r = await _ready(env)
    assert r.status == "NEEDS YOU" and r.next_link == "/alerts"
    (c,) = [c for c in r.checks if c.kind == "incident"]
    assert c.ok is False and "SECURITY" in c.text


async def test_an_ended_run_is_not_waiting_to_start(env: Env) -> None:
    await _ready_env(env)
    await _start(env, "binance")
    await _start(env, "hfm-cent")
    await _end(env, "binance", "STOPPED")
    r = await _ready(env)
    assert r.status == "RUNNING"  # a run stopped at its floor asks nothing; the other runs
    c = _line(r, f"{BINANCE}: stopped for good")
    assert c.ok is None and "below 80.00: that is its result" in c.text
    await _end(env, "hfm-cent", "REVOKED")
    r = await _ready(env)
    assert r.status == "READY TO START" and r.next_label == "Renew"
    assert f"renew {HFM} from the money it holds now" in r.next_step and BINANCE not in r.next_step
    assert _line(r, f"{HFM}: you ended its run").ok is None
    env.clock.advance(minutes=1)  # started again a little later
    await _feeds(env)
    await _start(env, "hfm-cent")
    await _end(env, "hfm-cent", "STOPPED")
    r = await _ready(env)
    assert r.status == "ENDED" and r.next_step.startswith("Each rehearsal stopped for good")
    assert not r.runs


async def test_a_run_that_ends_renews_itself_or_says_why_not(env: Env) -> None:
    await _ready_env(env)
    await _start(env, "binance")
    await _start(env, "hfm-cent")
    end = WED + timedelta(days=rehearsal.DAYS, minutes=1)
    env.clock.set(end)
    await _history(env, 1)
    await _feeds(env)
    r = await _ready(env)
    assert r.status == "RUNNING"  # the worker ends and renews both within minutes
    assert _line(r, f"{BINANCE}: its run has just ended").text.endswith(
        "it renews itself within minutes"
    )
    async with env.db.transaction() as conn:
        await mandates.expire_due(conn, end)
    await set_binance_fee(env)  # HFM Cent's fee and market removed: its renewal needs the owner
    r = await _ready(env)
    assert r.status == "NEEDS YOU" and r.next_label == "See why"
    assert r.next_step.startswith(f"{HFM} could not renew itself")
    c = _line(r, f"{HFM}: its run ended and could not renew itself")
    assert c.ok is False and c.text.endswith("the HFM_CENT fee is no longer set under Risk policy")
    assert _line(r, f"{BINANCE}: its run has ended").text.endswith(
        "it renews itself within minutes"
    )


async def _prices(env: Env, t: datetime) -> dict[str, ready.Check]:
    async with env.db.connect() as conn:
        checks = await ready._prices(conn, t)
    return {"binance" if c.text.startswith(BINANCE) else "hfm": c for c in checks}


async def test_prices_as_the_auto_trader_and_the_hfm_bridge_see_them(env: Env) -> None:
    env.clock.set(WED)
    await _history(env, 1)
    await _quote(env, PAXG, "binance_public", WED)
    await _bridge(env, WED)
    got = await _prices(env, WED)
    assert got["binance"].text.endswith(
        f"only 1 of the {ready.MIN_DAILY} days of gold prices the auto-trader needs"
    )
    await _history(env, ready.MIN_DAILY)
    got = await _prices(env, WED)
    assert got["binance"].ok and got["hfm"].ok is False
    assert got["hfm"].text.endswith("no HFM gold price has arrived yet")
    await _quote(env, CENT, "hfm_cent_terms", WED)
    await _bridge(env, WED, tick=WED)
    got = await _prices(env, WED)
    assert got["hfm"].ok and got["hfm"].text.endswith(
        "daily gold prices up to date, live prices arriving"
    )
    # five quiet minutes, a late Binance poll: no problem yet, both retried
    t = WED + timedelta(minutes=5)
    await _bridge(env, t, tick=WED)
    got = await _prices(env, t)
    assert (
        got["binance"].ok is None and "the latest live price came 5 min ago" in got["binance"].text
    )
    assert got["hfm"].ok is None and "no new HFM gold price for 5 min" in got["hfm"].text
    # twenty: at 08:20 in New York that is no break, and Binance is not arriving
    t = WED + timedelta(minutes=20)
    await _bridge(env, t, tick=WED)
    got = await _prices(env, t)
    assert got["hfm"].ok is False and "no new gold price for 20 min" in got["hfm"].text
    assert got["hfm"].text.endswith("a market holiday, or a frozen terminal")
    assert got["binance"].text.endswith(
        "live prices are not arriving from Binance (the latest 20 min ago)"
    )
    # HFM ticking but its prices not stored here; the bridge silent; its terminal cut off
    await _bridge(env, t, tick=t)
    assert (await _prices(env, t))["hfm"].text.endswith("reach the bridge but are not stored here")
    await _bridge(env, t - timedelta(seconds=20), tick=t)
    assert (await _prices(env, t))["hfm"].text.endswith("the HFM bridge is not reporting")
    await _bridge(env, t, tick=WED, connected=False)
    assert (await _prices(env, t))["hfm"].text.endswith("terminal is not connected to HFM")

    # in time order from here (a stored price newer than the moment would be one from its future)
    async def at(
        t: datetime, hfm_last: datetime, *, history: bool = True
    ) -> dict[str, ready.Check]:
        """At ``t``: Binance's price now; HFM's last price stored and seen by the bridge."""
        env.clock.set(t)
        if history:
            await _history(env, 2)
        await _quote(env, PAXG, "binance_public", t)
        await _quote(env, CENT, "hfm_cent_terms", hfm_last)
        await _bridge(env, t, tick=hfm_last)
        return await _prices(env, t)

    # HFM's daily break, from 17:00 in New York (summer time here)
    got = await at(
        datetime(2026, 10, 14, 21, 30, tzinfo=UTC), datetime(2026, 10, 14, 20, 59, tzinfo=UTC)
    )
    assert got["binance"].ok and got["hfm"].ok is None
    assert "HFM's daily gold break" in got["hfm"].text
    # the weekend from Friday 17:00 in New York: closed, but the bridge must still report
    friday = datetime(2026, 10, 16, 20, 59, tzinfo=UTC)
    for t in (
        datetime(2026, 10, 16, 21, 30, tzinfo=UTC),
        datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    ):
        got = await at(t, friday)
        assert got["hfm"].ok and got["hfm"].text.endswith("HFM's gold is closed for the weekend"), t
    await _bridge(env, t - timedelta(hours=1), tick=friday)
    got = await _prices(env, t)
    assert got["hfm"].ok is False and "the bridge must run before it reopens" in got["hfm"].text
    # just after the Sunday reopening, 18:00 in New York: waiting for the first price
    got = await at(datetime(2026, 10, 18, 22, 30, tzinfo=UTC), friday)
    assert got["hfm"].ok is None and "just reopened after the weekend" in got["hfm"].text
    # winter time: 16:30 in New York is no break, 17:30 is
    got = await at(
        datetime(2026, 11, 4, 21, 30, tzinfo=UTC), datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
    )
    assert got["hfm"].ok is False and "no new gold price for 30 min" in got["hfm"].text
    got = await at(
        datetime(2026, 11, 4, 22, 30, tzinfo=UTC),
        datetime(2026, 11, 4, 21, 59, tzinfo=UTC),
        history=False,
    )
    assert got["hfm"].ok is None and "HFM's daily gold break" in got["hfm"].text
    friday = datetime(2026, 11, 6, 21, 59, tzinfo=UTC)
    got = await at(datetime(2026, 11, 8, 23, 30, tzinfo=UTC), friday)
    assert got["hfm"].ok is None and "just reopened after the weekend" in got["hfm"].text
    # a daily bar the auto-trader would call stale comes first, whatever the live prices
    late = datetime(2026, 11, 11, 23, 30, tzinfo=UTC)
    await _quote(env, PAXG, "binance_public", late)
    got = await _prices(env, late)
    assert got["binance"].text.endswith("daily gold prices are not up to date")


async def test_the_home_page_shows_it_to_the_owner_only(env: Env, settings: Settings) -> None:
    app = create_app(settings, db=env.db, clock=env.clock)
    await _beat(env, "bot")  # the bot beats, the worker does not
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/")).text
    assert "Your $100 gold auto-trader" in html and "PROBLEM" in html
    assert "✗ The worker that runs the auto-trader is not running" in html
    assert '<a class="button" href="/ops">Open Operations</a>' in html
    assert "• Binance gold (PAXG): not started yet" in html
    assert "stops for good before one more full loss could take it below 80.00" in html
    assert "Where things are" in html and "<code>/rehearsal</code>" in html
    # nor does System health count the bot's heartbeat as the worker's
    assert re.search(
        r"Worker heartbeat</th><td><span class=\"tag tag-bad\">UNAVAILABLE</span> none", html
    )
    for role in (Role.ANALYST, Role.VIEWER):
        user = await make_user(env.db, env.clock, username=role.value.lower(), roles=[role])
        async with client_for(app) as c:
            await full_login(c, user, env.clock)
            html = (await c.get("/")).text
        assert "Your $100 gold auto-trader" not in html, role


async def test_a_failing_panel_leaves_the_page_working(
    env: Env, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(conn: Any, _now: datetime) -> ready.Ready:
        await conn.execute(text("SELECT 1/0"))
        raise AssertionError("unreachable")

    monkeypatch.setattr(ready, "build", broken)
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = await c.get("/")
    assert page.status_code == 200
    assert "Whether it is ready could not be checked just now (the error is logged)" in page.text
    assert "Worker heartbeat" in page.text  # the rest of the page is there
