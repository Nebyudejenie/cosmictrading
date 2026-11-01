"""Get both rehearsals ready in one step (S46): one risk-policy change, with the owner's code,
applies the fees and lists the rehearsals need, and only the advised limits the owner ticks;
worked out from the policy the page showed, never from one changed since."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.db.engine import Database
from cosmic.db.schema_market import instruments
from cosmic.db.schema_trading import risk_policies
from cosmic.identity.capabilities import Role
from cosmic.reporting import autotrader
from cosmic.reporting import rehearsal as page
from cosmic.trading.policy import DEFAULT_POLICY, RiskPolicyParams, active_policy, change_policy
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login, make_user
from tests.trading.test_rehearsal import set_binance_fee
from tests.trading_env import Env, build_env

D = Decimal
BOTH = {"binance", "hfm-cent"}


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


def _form(html: str, **data: object) -> dict[str, object]:
    version = re.search(r'name="version" value="(\d+)"', html)
    assert version is not None
    return {"csrf_token": csrf_from(html), "version": version.group(1), **data}


def test_what_the_policy_lacks_and_what_is_advised() -> None:
    changes = page.ready_changes(DEFAULT_POLICY, BOTH)
    assert [(c.key, c.needed) for c in changes] == [
        ("fee:BINANCE", True),  # Binance gold itself is allowed by default
        ("fee:HFM_CENT", True),
        ("allow:hfm-cent", True),
        ("advise:max_open_risk_pct", False),
        ("advise:max_loss_streak", False),
        ("advise:max_drawdown_pct", False),
    ]
    whats = {c.key: c.what for c in changes}
    assert whats["fee:BINANCE"] == (
        "Binance fee a side, as you enter it below: it applies to every Binance account, a "
        "real-money one included"
    )
    assert (
        whats["fee:HFM_CENT"] == "HFM_CENT fee a side: 0 bps (a Cent account charges no commission)"
    )
    assert whats["allow:hfm-cent"].endswith(": CFD:XAU/USD@HFM_CENT, HFM_CENT")  # USD is allowed
    streak = autotrader.losses_to_own_stop()
    assert whats["advise:max_loss_streak"].startswith(f"max_loss_streak: 4 → {streak} (")
    assert "a real-money one included" in whats["advise:max_drawdown_pct"]
    # a rehearsal whose market cannot be traded here is left out
    assert [c.key for c in page.ready_changes(DEFAULT_POLICY, {"binance"}) if c.needed] == [
        "fee:BINANCE"
    ]
    # The page's notes and the form give the same values.
    per_trade = page.start_per_trade()
    notes = " ".join(autotrader.policy_notes(DEFAULT_POLICY, D(100), D(100), per_trade))
    advice = autotrader.policy_advice(DEFAULT_POLICY, D(100), per_trade)
    assert f"{advice['max_drawdown_pct']} % lets its own stop decide" in notes
    assert f"{advice['max_open_risk_pct']} % would allow" in notes
    assert advice == {"max_open_risk_pct": D(4), "max_loss_streak": streak, "max_drawdown_pct": 25}


def test_only_ticked_advice_applies_and_the_fee_is_checked() -> None:
    params, done = page.ready_params(
        DEFAULT_POLICY,
        binance_bps=D("7.5"),
        advised={"advise:max_loss_streak", "advise:max_daily_loss", "other"},
        available=BOTH,
    )
    assert params["venue_fees"] == (
        {"venue": "BINANCE", "bps": D("7.5")},
        {"venue": "HFM_CENT", "bps": D(0)},
    )
    assert "CFD:XAU/USD@HFM_CENT" in params["allowed_instruments"]
    assert "HFM_CENT" in params["allowed_venues"]
    assert params["max_loss_streak"] == autotrader.losses_to_own_stop()
    assert params["max_drawdown_pct"] == DEFAULT_POLICY.max_drawdown_pct  # not ticked
    assert params["max_daily_loss"] == DEFAULT_POLICY.max_daily_loss  # not one it offers
    assert done[:2] == ["BINANCE fee 7.5 bps", "HFM_CENT fee 0 bps"]
    with pytest.raises(ValidationFailedError, match="enter your Binance fee"):
        page.ready_params(DEFAULT_POLICY, binance_bps=None, advised=set(), available=BOTH)
    for bad in (D(-1), D(101), D("NaN")):
        with pytest.raises(ValidationFailedError, match="between 0 and 100"):
            page.ready_params(DEFAULT_POLICY, binance_bps=bad, advised=set(), available=BOTH)
    # a fee already set is never touched, whatever is posted
    set_fee = RiskPolicyParams.model_validate(
        DEFAULT_POLICY.model_dump(mode="python")
        | {"venue_fees": ({"venue": "BINANCE", "bps": D("7.5")},)}
    )
    params, done = page.ready_params(set_fee, binance_bps=D(10), advised=set(), available=BOTH)
    assert {f["venue"]: f["bps"] for f in params["venue_fees"]}["BINANCE"] == D("7.5")
    assert "BINANCE fee" not in " ".join(done)


async def test_the_owner_gets_both_ready_with_one_change(env: Env, settings: Settings) -> None:
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/rehearsal")).text
        assert "Get them ready in one step" in html and 'name="binance_fee"' in html
        r = await c.post(  # the fee is required while it is not set
            "/rehearsal/ready", data=_form(html, binance_fee="")
        )
        assert r.status_code == 422 and "enter your Binance fee" in r.text
        r = await c.post(
            "/rehearsal/ready",
            data=_form(
                html,
                binance_fee="10",
                advise=["advise:max_loss_streak", "advise:max_drawdown_pct"],
            ),
        )
        assert r.status_code == 303 and r.headers["location"] == "/rehearsal#ready-title"
        async with env.db.connect() as conn:
            _, p = await active_policy(conn)
            reason = await conn.scalar(
                select(risk_policies.c.reason).where(risk_policies.c.active.is_(True))
            )
        assert {f.venue: f.bps for f in p.venue_fees} == {"BINANCE": D(10), "HFM_CENT": D(0)}
        assert "CFD:XAU/USD@HFM_CENT" in p.allowed_instruments
        assert (p.max_loss_streak, p.max_drawdown_pct) == (autotrader.losses_to_own_stop(), 25)
        assert p.max_open_risk_pct == DEFAULT_POLICY.max_open_risk_pct  # not ticked
        assert reason.startswith("Start with $100, getting the rehearsals ready: BINANCE fee 10")
        r = await c.post("/rehearsal/ready", data=_form(html))  # the old page, again
        assert r.status_code == 409 and "changed since this page was shown" in r.text
        html = (await c.get("/rehearsal")).text
        assert "✓ Binance gold (PAXG): your BINANCE fee is set" in html
        assert "✓ Your risk policy allows CFD:XAU/USD@HFM_CENT" in html
        assert "Advised limits for the rehearsals" in html and 'name="binance_fee"' not in html
        assert 'value="advise:max_open_risk_pct"' in html
        r = await c.post("/rehearsal/ready", data=_form(html))
        assert r.status_code == 409 and "nothing to change" in r.text
        env.clock.advance(minutes=settings.step_up_minutes + 1)  # the code is no longer fresh
        r = await c.post("/rehearsal/ready", data=_form(html, advise="advise:max_open_risk_pct"))
        assert r.status_code == 403
    async with env.db.connect() as conn:
        _, p = await active_policy(conn)
    assert p.max_open_risk_pct == DEFAULT_POLICY.max_open_risk_pct


async def test_a_wrong_fee_changes_nothing_and_an_analyst_sees_no_form(
    env: Env, settings: Settings
) -> None:
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/rehearsal")).text
        for fee in ("lots", "-1", "-0", "1e-30", "1_0", "150.5", "7.555"):
            r = await c.post("/rehearsal/ready", data=_form(html, binance_fee=fee))
            assert r.status_code == 422, (fee, r.status_code)
        r = await c.post("/rehearsal/ready", data=_form(html, binance_fee="150"))
        assert r.status_code == 422 and "between 0 and 100" in r.text
    async with env.db.connect() as conn:
        _, p = await active_policy(conn)
    assert p.venue_fees == ()
    analyst = await make_user(env.db, env.clock, username="analyst", roles=[Role.ANALYST])
    async with client_for(app) as c:
        await full_login(c, analyst, env.clock)
        html = (await c.get("/rehearsal")).text
        assert "Before they start" in html and "/rehearsal/ready" not in html


async def test_a_rehearsal_that_cannot_trade_here_never_blocks_the_other(
    env: Env, settings: Settings
) -> None:
    async with env.db.transaction() as conn:
        await conn.execute(
            update(instruments)
            .where(instruments.c.canonical_key == "CFD:XAU/USD@HFM_CENT")
            .values(tradable=False)
        )
    app = create_app(settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        html = (await c.get("/rehearsal")).text
        assert "Left out: HFM Cent terms, on HFM&#39;s gold prices" in html
        r = await c.post("/rehearsal/ready", data=_form(html, binance_fee="10"))
        assert r.status_code == 303
    async with env.db.connect() as conn:
        _, p = await active_policy(conn)
    assert {f.venue for f in p.venue_fees} == {"BINANCE"}
    assert "CFD:XAU/USD@HFM_CENT" not in p.allowed_instruments


async def test_a_fee_already_set_is_left_alone(env: Env, settings: Settings) -> None:
    await set_binance_fee(env, "7.5")
    async with env.db.connect() as conn:
        _, p = await active_policy(conn)
    assert [c.key for c in page.ready_changes(p, BOTH) if c.key.startswith("fee:")] == [
        "fee:HFM_CENT"
    ]


async def test_a_change_worked_out_from_an_older_version_is_refused(env: Env) -> None:
    """A double click or a second tab: the policy is checked again under its lock."""
    async with env.db.connect() as conn:
        version, p = await active_policy(conn)
    params = p.model_dump(mode="python") | {"max_loss_streak": 9}
    async with env.db.transaction() as conn:
        with pytest.raises(ConflictError, match="changed since this page was shown"):
            await change_policy(
                conn,
                principal=env.principal(),
                new_params=params,
                reason="a change from an old page",
                now=env.clock.now(),
                settings=env.settings,
                expected_version=version + 1,
            )
    async with env.db.transaction() as conn:
        assert (
            await change_policy(
                conn,
                principal=env.principal(),
                new_params=params,
                reason="the same change, from the page now",
                now=env.clock.now(),
                settings=env.settings,
                expected_version=version,
            )
            == version + 1
        )
