"""The $100 rehearsal (the $100 Gold Plan, S37): simulated money treated exactly like the real
$100 to come, started in one action with the owner's code, and the page that explains it."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, StepUpRequiredError
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_execution import trading_mandates
from cosmic.reporting import rehearsal as page
from cosmic.trading import mandates, rehearsal
from cosmic.trading.accounts import get_account, paper_account_for_venue
from cosmic.trading.policy import DEFAULT_POLICY, change_policy
from cosmic.trading.risk.context import PositionState
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.trading_env import Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def set_binance_fee(env: Env, bps: str = "10", *, cent: bool = False) -> None:
    """The owner's policy change: the Binance fee, and with ``cent`` HFM Cent's too and its
    market allowed (S38)."""
    base = DEFAULT_POLICY.model_dump(mode="python")
    fees: tuple[dict[str, object], ...] = ({"venue": "BINANCE", "bps": D(bps)},)
    params = base | {"venue_fees": fees}
    if cent:
        params |= {
            "venue_fees": (*fees, {"venue": "HFM_CENT", "bps": D(0)}),
            "allowed_instruments": (*base["allowed_instruments"], "CFD:XAU/USD@HFM_CENT"),
            "allowed_venues": (*base["allowed_venues"], "HFM_CENT"),
        }
    async with env.db.transaction() as conn:
        await change_policy(
            conn,
            principal=env.principal(),
            new_params=params,
            reason="Binance regular fee for the rehearsal",
            now=env.clock.now(),
            settings=env.settings,
        )


async def start(
    env: Env, *, fresh: bool = True, rule: str = "turtle_55_20", venue: str = "binance"
) -> None:
    async with env.db.transaction() as conn:
        await rehearsal.start(
            conn,
            principal=env.principal(mfa_fresh=fresh),
            broker=env.svc.paper,
            rule=rule,
            now=env.clock.now(),
            settings=env.settings,
            venue=venue,
        )


# ---- starting it ----------------------------------------------------------------------------------


async def test_it_starts_only_with_the_fee_set_and_a_fresh_code(env: Env) -> None:
    with pytest.raises(ConflictError, match="set the BINANCE fee first"):
        await start(env)
    await set_binance_fee(env)
    with pytest.raises(StepUpRequiredError):
        await start(env, fresh=False)
    await start(env)
    async with env.db.connect() as conn:
        acct = await get_account(conn, alias=rehearsal.ALIAS)
        m = (await mandates.active_mandates(conn, env.clock.now()))[-1]
        auto_paper = await paper_account_for_venue(conn, "BINANCE")
    assert (acct.kind, acct.venue_key, acct.base_currency) == ("PAPER", "BINANCE", "USDT")
    assert m.account_id == acct.id and m.timeframe == "1d" and m.strategy_key == "turtle_55_20"
    assert (m.capital, m.max_loss_per_trade, m.total_limit) == (D(100), D("2.00"), D(20))
    assert m.instruments == (rehearsal.MARKET,) and m.mode == "AUTO"
    assert (m.expires_at - m.granted_at).days == 90
    assert auto_paper != acct.id  # library proposals never land in the rehearsal account
    with pytest.raises(ConflictError, match="already running"):
        await start(env)


async def test_renewing_starts_from_the_money_now_and_keeps_the_floor(env: Env) -> None:
    await set_binance_fee(env)
    await start(env)
    async with env.db.transaction() as conn:
        m = (await mandates.active_mandates(conn, env.clock.now()))[-1]
        await mandates.end(
            conn,
            mandate_id=m.id,
            status="EXPIRED",
            by=mandates.ACTOR,
            reason="t",
            now=env.clock.now(),
        )
    env.clock.advance(seconds=1)
    await start(env, rule="ema_200_trend")
    async with env.db.connect() as conn:
        m = (await mandates.active_mandates(conn, env.clock.now()))[-1]
    assert m.strategy_key == "ema_200_trend" and m.capital == D(100)
    assert m.total_limit == D(20)  # 100 now, floor 80


async def test_a_rehearsal_that_stopped_itself_is_not_restarted(env: Env) -> None:
    await set_binance_fee(env)
    await start(env)
    async with env.db.transaction() as conn:
        m = (await mandates.active_mandates(conn, env.clock.now()))[-1]
        await mandates.end(
            conn,
            mandate_id=m.id,
            status="STOPPED",
            by=mandates.ACTOR,
            reason="t",
            now=env.clock.now(),
        )
    with pytest.raises(ConflictError, match="not restarted"):
        await start(env)


# ---- the page's arithmetic ------------------------------------------------------------------------


def test_possible_years_are_reproducible_and_ordered() -> None:
    pnls = [10.0, -2.0, -2.0, -2.0]
    a = page.resample(pnls, 6.0, 1, start=100.0, floor=80.0, seed=7)
    b = page.resample(pnls, 6.0, 1, start=100.0, floor=80.0, seed=7)
    assert a == b and a.bad <= a.typical <= a.good
    flat = page.resample([], 6.0, 1, start=100.0, floor=80.0, seed=7)
    assert (flat.bad, flat.good, flat.stop_chance) == (D("100.00"), D("100.00"), 0.0)
    doomed = page.resample([-5.0], 12.0, 1, start=100.0, floor=80.0, seed=7)
    assert doomed.stop_chance == 1.0  # four losses of 5 reach the floor within a year


def test_money_grows_with_the_rate_and_the_monthly_addition() -> None:
    assert page.grow(D(100), D(0), 0.10, 1) == D("110.00")
    assert page.grow(D(100), D(10), 0.0, 1) == D("220.00")


# ---- the page and the guard on the other route --------------------------------------------------


async def test_the_page_says_what_to_do_first_and_starts_it(env: Env, settings: Settings) -> None:
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page_html = (await c.get("/rehearsal?monthly=25")).text
        assert "Start with $100" in page_html and "set the fee first" in page_html
        assert "1. Where $100 can trade gold" in page_html
        assert "HISTORICAL SIMULATION" in page_html
        await set_binance_fee(env)
        page_html = (await c.get("/rehearsal")).text
        assert "your BINANCE fee is set, 10 bps a side" in page_html
        r = await c.post(
            "/rehearsal/start", data={"csrf_token": csrf_from(page_html), "rule": "turtle_20_10"}
        )
        assert r.status_code == 303 and r.headers["location"].endswith(
            "account=rehearsal-binance-100"
        )
        page_html = (await c.get("/rehearsal")).text
        assert "Money now" in page_html and "RUNNING" in page_html
        setup = (await c.get("/trader/setup?account=rehearsal-binance-100&timeframe=1d")).text
        r = await c.post(  # the general setup cannot bypass the rehearsal's floor
            "/trader/start",
            data={
                "csrf_token": csrf_from(setup),
                "money": "100",
                "care": "BOLD",
                "mode": "AUTO",
                "account": rehearsal.ALIAS,
                "timeframe": "1d",
                "rule": "turtle_20_10",
                "instruments": rehearsal.MARKET,
            },
        )
        assert r.status_code == 422 and "Start with $100" in r.text
    async with env.db.connect() as conn:
        rows = list(await conn.execute(select(trading_mandates.c.strategy_key)))
    assert [r.strategy_key for r in rows] == ["turtle_20_10"]


# ---- review fixes: the floor cannot be bypassed, renewal is safe -------------------------------


async def test_only_the_rehearsal_may_grant_on_its_account(env: Env, settings: Settings) -> None:
    await set_binance_fee(env)
    await start(env)
    async with env.db.transaction() as conn:
        m = (await mandates.active_mandates(conn, env.clock.now()))[-1]
        await mandates.end(
            conn,
            mandate_id=m.id,
            status="REVOKED",
            by=mandates.ACTOR,
            reason="t",
            now=env.clock.now(),
        )
    terms = mandates.MandateTerms(
        account_alias=rehearsal.ALIAS,
        strategy_key="turtle_20_10",
        instruments=(rehearsal.MARKET,),
        max_loss_per_trade=D(30),
        max_trades_per_day=5,
        max_open_positions=2,
        daily_loss_limit=D(60),
        days=30,
    )
    with pytest.raises(ConflictError, match="only from Start with \\$100"):
        async with env.db.transaction() as conn:  # the Operations form's path
            await mandates.grant(
                conn, principal=env.principal(), terms=terms, now=env.clock.now(), settings=settings
            )
    async with env.db.connect() as conn:
        choices = (await mandates.view(conn, env.clock.now()))["choices"]
    assert rehearsal.ALIAS not in [c["alias"] for c in choices]
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        ops = (await c.get("/ops")).text
        r = await c.post(
            "/ops/paper-account",
            data={"csrf_token": csrf_from(ops), "alias": "rehearsal-x", "capital": "10000"},
        )
        assert r.status_code == 422 and "only from Start with $100" in r.text
        page_html = (await c.get(f"/trader?account={rehearsal.ALIAS}")).text
        assert 'href="/rehearsal"' in page_html  # its settings live on its own page


async def test_renewal_rounds_down_and_waits_for_the_open_trade(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_binance_fee(env)
    await start(env)
    async with env.db.transaction() as conn:
        m = (await mandates.active_mandates(conn, env.clock.now()))[-1]
        await mandates.end(
            conn,
            mandate_id=m.id,
            status="EXPIRED",
            by=mandates.ACTOR,
            reason="t",
            now=env.clock.now(),
        )
    real = rehearsal.account_state

    async def holding(*a: object, **k: object) -> object:
        s = await real(*a, **k)  # type: ignore[arg-type]
        held = PositionState(rehearsal.MARKET, D("0.01"), D(4000))
        return replace(s, positions=(held,))

    monkeypatch.setattr(rehearsal, "account_state", holding)
    with pytest.raises(ConflictError, match="still holds a trade"):
        await start(env)

    async def odd_cents(*a: object, **k: object) -> object:
        s = await real(*a, **k)  # type: ignore[arg-type]
        return replace(s, equity=D("98.4567"))

    monkeypatch.setattr(rehearsal, "account_state", odd_cents)
    monkeypatch.setattr(mandates, "account_state", odd_cents)
    env.clock.advance(seconds=1)
    await start(env)
    async with env.db.connect() as conn:
        m = (await mandates.active_mandates(conn, env.clock.now()))[-1]
    assert m.capital == D("98.45") and m.total_limit == D("18.45")  # floor 80 kept


async def test_the_reminder_sends_the_rehearsal_to_its_own_page(env: Env) -> None:
    await set_binance_fee(env)
    await start(env)
    env.clock.advance(days=89, hours=12)
    async with env.db.transaction() as conn:
        await mandates.remind_renewals(conn, env.clock.now())
    async with env.db.connect() as conn:
        texts = [
            str(r.payload.get("text"))
            for r in await conn.execute(select(notifications.c.payload))
            if "ends on" in str(r.payload.get("text"))
        ]
    assert texts and all("renew it under Start with $100" in t for t in texts)


# ---- the HFM Cent side (S38) ----------------------------------------------------------------------


async def test_the_hfm_cent_rehearsal_needs_its_fee_and_its_market_allowed(env: Env) -> None:
    await set_binance_fee(env)
    with pytest.raises(ConflictError, match="set the HFM_CENT fee first"):
        await start(env, venue="hfm-cent")
    env.clock.advance(seconds=1)
    await set_binance_fee(env, cent=True)
    await start(env, venue="hfm-cent", rule="turtle_20_10")
    async with env.db.connect() as conn:
        acct = await get_account(conn, alias=rehearsal.REHEARSALS["hfm-cent"].alias)
        m = (await mandates.active_mandates(conn, env.clock.now()))[-1]
    assert (acct.kind, acct.venue_key, acct.base_currency) == ("PAPER", "HFM_CENT", "USD")
    assert m.instruments == ("CFD:XAU/USD@HFM_CENT",) and m.timeframe == "1d"
    assert (m.capital, m.max_loss_per_trade, m.total_limit) == (D(100), D("2.00"), D(20))
    await start(env)  # and the Binance one beside it, each on its own account
    async with env.db.connect() as conn:
        assert len(await mandates.active_mandates(conn, env.clock.now())) == 2
