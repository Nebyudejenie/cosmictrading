"""Strategy library pages: the league table, a rule's evidence and the owner's switch."""

from __future__ import annotations

from decimal import Decimal

from cosmic.core.config import Settings
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema_research import library_runs, strategy_evaluations
from cosmic.identity.capabilities import Role
from cosmic.research.library import store
from cosmic.research.library.catalog import BY_KEY
from cosmic.research.library.engine import COINBASE_LIKE
from cosmic.web.app import create_app
from tests.e2e.test_trading_portal import _setup
from tests.helpers import client_for, csrf_from, full_login, make_user

WATCHED, REJECTED = "detzel_sma_10", "bll_vma_1_50"
METRICS = {
    "sharpe": 1.1,
    "cagr": 0.42,
    "max_drawdown": -0.55,
    "exposure": 0.5,
    "trades": 40,
    "win_rate": 0.45,
    "turnover_per_year": 6.0,
}


async def seed_run(db: Database, now: object) -> None:
    async with db.transaction() as conn:
        run_id = uuid7()
        await conn.execute(
            library_runs.insert().values(
                id=run_id,
                started_at=now,
                finished_at=now,
                code_version=store.CODE_VERSION,
                trials=195,
                costs={"name": COINBASE_LIKE.name, "basis": "ASSUMPTION"},
                series=[
                    {
                        "series": "SPOT:BTC/USD@BITSTAMP",
                        "from": "2011-08-18",
                        "to": "2026-10-09",
                        "bars": 5536,
                        "evaluated_from_bar": 365,
                        "validation_from_bar": 3467,
                        "tiers": {"WATCH": 1, "REJECTED": 1},
                        "buy_and_hold": {"full": METRICS},
                    }
                ],
                summary={},
                result_hash="test",
                created_at=now,
            )
        )
        for rank, (key, tier) in enumerate(((WATCHED, "WATCH"), (REJECTED, "REJECTED")), 1):
            vid = await store.ensure_spec(conn, BY_KEY[key], now)  # type: ignore[arg-type]
            result = {
                "strategy": key,
                "series": "SPOT:BTC/USD@BITSTAMP",
                "tier": tier,
                "costs": {"name": COINBASE_LIKE.name, "one_way_bps": 66.0, "basis": "ASSUMPTION"},
                "full": METRICS,
                "development": METRICS,
                "validation": METRICS,
                "recent": METRICS,
                "full_double_costs": METRICS,
                "timing_sharpe_annualised": 0.9,
                "timing_sharpe_development": 1.2,
                "timing_sharpe_validation": 0.4,
                "deflated_sharpe_probability": 0.21,
                "reasons": ["timing skill not significant"],
            }
            await conn.execute(
                strategy_evaluations.insert().values(
                    id=uuid7(),
                    library_run_id=run_id,
                    strategy_version_id=vid,
                    series="SPOT:BTC/USD@BITSTAMP",
                    instrument_id=None,
                    tier=tier,
                    rank=rank,
                    deflated_sharpe=Decimal("0.21"),
                    timing_sharpe=Decimal("0.9"),
                    result=result,
                    created_at=now,
                )
            )


async def test_library_pages_and_owner_switch(settings: Settings, db: Database) -> None:
    clock, _ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    owner = await make_user(db, clock)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        empty = await c.get("/strategies/library")
        assert empty.status_code == 200 and "No evaluation yet" in empty.text
        await seed_run(db, clock.now())
        page = (await c.get("/strategies/library")).text
        assert "Price above 10-day average" in page and "WATCH" in page and "REJECTED" in page
        assert "not evidence of future profit" in page
        # Directive Phase 7: no winner is shown as such, with the tests the rules failed.
        assert "NO QUALIFIED STRATEGY" in page and "never lowered to produce a winner" in page
        rule = await c.get(f"/strategies/library/{WATCHED}")
        assert rule.status_code == 200 and "Detzel" in rule.text
        assert "forward test, not a proven edge" in rule.text  # the acknowledgement box
        token = csrf_from(rule.text)
        form = {
            "csrf_token": token,
            "instrument": "SPOT:BTC/USD@COINBASE",
            "on": "1",
            "reason": "forward test on paper",
        }
        refused = await c.post(f"/strategies/library/{WATCHED}/switch", data=form)
        assert refused.status_code == 422 and "forward test" in refused.text  # no acknowledgement
        ok = await c.post(
            f"/strategies/library/{WATCHED}/switch", data={**form, "acknowledge": "yes"}
        )
        assert ok.status_code == 303, ok.text
        assert "<strong>ON</strong>" in (await c.get(f"/strategies/library/{WATCHED}")).text
        listing = (await c.get("/strategies/library")).text
        assert "Switched on for paper proposals (1 of at most 10)" in listing
        off = await c.post(
            f"/strategies/library/{WATCHED}/switch",
            data={**form, "on": "0", "reason": "enough for now"},
        )
        assert off.status_code == 303
        rejected = await c.get(f"/strategies/library/{REJECTED}")
        assert "Rejected rules cannot be switched on" in rejected.text
        forged = await c.post(
            f"/strategies/library/{REJECTED}/switch", data={**form, "acknowledge": "yes"}
        )
        assert forged.status_code == 409
        assert (await c.get("/strategies/library/no_such_rule")).status_code == 404


async def test_viewer_sees_but_cannot_switch(settings: Settings, db: Database) -> None:
    clock, _ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    viewer = await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    await seed_run(db, clock.now())
    async with client_for(app) as c:
        await full_login(c, viewer, clock)
        rule = await c.get(f"/strategies/library/{WATCHED}")
        assert rule.status_code == 200 and "Switch on for paper proposals" not in rule.text
        r = await c.post(
            f"/strategies/library/{WATCHED}/switch",
            data={
                "csrf_token": csrf_from(rule.text),
                "instrument": "SPOT:BTC/USD@COINBASE",
                "on": "1",
                "reason": "should not work",
                "acknowledge": "yes",
            },
        )
        assert r.status_code == 403
