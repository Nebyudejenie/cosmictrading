"""Fees measured from the owner's own fills (trading/measured.py): never from the paper
simulator, and shown on the Risk policy page where the fee is set."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.trading import measured
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading_env import Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def test_paper_fills_are_never_measured(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(2)  # filled by the paper simulator, at the policy's own fee
    async with env.db.connect() as conn:
        assert await measured.measured_fees(conn) == {}
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/settings/risk")).text
    assert "No real venue has filled a trade yet" in page


def test_the_advice_names_the_fee_to_set() -> None:
    hfm = measured.MeasuredFee("HFM", 4, D("16000"), D("0"))
    assert hfm.bps == 0
    hint = measured.advice(hfm, D(60))
    assert hint is not None and "Set Fee per venue HFM=0 " in hint and "60.00 bps" in hint
    assert measured.advice(measured.MeasuredFee("HFM", 0, D(0), D(0)), D(60)) is None
    assert measured.advice(hfm, D("4.99")) is None  # within 5 bps: nothing to tell


async def test_the_risk_page_shows_a_measured_fee_and_what_to_set(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(conn: object) -> dict[str, measured.MeasuredFee]:
        return {"HFM": measured.MeasuredFee("HFM", 3, D("12000"), D("0"))}

    monkeypatch.setattr(measured, "measured_fees", fake)
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/settings/risk")).text
    assert "Measured from your own trades" in page
    assert "HFM: 0.00 bps a side over 3 fill(s)" in page and "Set Fee per venue HFM=0 " in page
