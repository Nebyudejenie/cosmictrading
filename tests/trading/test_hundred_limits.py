"""The owner's 100 USD demo and R08's account-wide limits (S100; the owner, 2026-11-18: "20 % and
10 in a row"). At 100 USD the defaults (10 % below the best, 4 losses in a row) stop every new
trade before the owner's own rules (2 USD a trade, a pause after 3 losses) would, and nothing
restarts them but a change of the policy. The home page says so while a 100 USD robot runs, and
the owner raises them with their code; a limit set higher is never lowered."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import desc, select

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events
from cosmic.trading.policy import DEFAULT_POLICY, active_policy, hundred_limits
from cosmic.web.app import create_app
from tests.helpers import client_for, csrf_from, full_login
from tests.mt5_emulator import MT5Emulator
from tests.trading.test_gold_m1 import _ready, _start
from tests.trading.test_hfm import bridge_running, build

pytestmark = pytest.mark.keeps_binance_paused

NOTICE = "The account-wide limits stop your 100 USD robot before your own rules"
BUTTON = "Use 20 % and 10 in a row"


async def test_the_owner_raises_the_account_wide_limits_for_their_100_usd_robot(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    emu = MT5Emulator()
    h = await build(settings, db, emu)
    async with bridge_running(db, h.env.clock, emu, tmp_path):
        await _ready(h, settings)
        app = create_app(settings, db=db, clock=h.env.clock)
        async with client_for(app) as c:
            await full_login(c, h.env.owner, h.env.clock)
            assert NOTICE not in (await c.get("/")).text  # no 100 USD robot yet
            assert (await _start(c)).status_code == 303  # the 1-minute robot with 100 USD
            home = (await c.get("/")).text
            assert NOTICE in home and BUTTON in home
            assert "10 % below its best (10.00 USD at 100.00)" in home
            assert "after 4 losses in a row (0 so far on hfm-demo)" in home
            async with db.connect() as conn:
                _, before = await active_policy(conn)
            r = await c.post("/gold/hfm/hundred-limits", data={"csrf_token": csrf_from(home)})
            assert r.status_code == 303 and r.headers["location"] == "/?limits=set"
            home = (await c.get("/?limits=set")).text
            assert "✓ Set: the risk policy now stops new trades only at 20 %" in home
            assert NOTICE not in home and BUTTON not in home
            again = await c.post("/gold/hfm/hundred-limits", data={"csrf_token": csrf_from(home)})
            assert again.status_code == 409  # nothing left to raise
        async with db.connect() as conn:
            _, policy = await active_policy(conn)
            reason = await conn.scalar(
                select(audit_events.c.payload["reason"].astext)
                .where(audit_events.c.action == "risk_policy.changed")
                .order_by(desc(audit_events.c.occurred_at))
                .limit(1)
            )
    assert (policy.max_drawdown_pct, policy.max_loss_streak) == (D(20), 10)
    assert reason is not None and "the owner's 100 USD demo" in reason
    # Everything else in the policy is as it was before the press.
    rest = {"max_drawdown_pct", "max_loss_streak"}
    assert {k: v for k, v in policy.model_dump().items() if k not in rest} == {
        k: v for k, v in before.model_dump().items() if k not in rest
    }


def test_a_limit_the_owner_set_higher_is_never_lowered() -> None:
    assert hundred_limits(DEFAULT_POLICY) == {"max_drawdown_pct": D(20), "max_loss_streak": 10}
    higher = DEFAULT_POLICY.model_copy(update={"max_drawdown_pct": D(30), "max_loss_streak": 4})
    assert hundred_limits(higher) == {"max_loss_streak": 10}
    both = DEFAULT_POLICY.model_copy(update={"max_drawdown_pct": D(25), "max_loss_streak": 12})
    assert hundred_limits(both) == {}
