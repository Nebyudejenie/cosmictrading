"""Fill prices against the price expected when the order went out (trading/measured.py, S39):
the arithmetic, the quote each fill is compared with, paper kept apart from a real venue, and
where it is shown."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from cosmic.core.config import Settings
from cosmic.core.money import BPS
from cosmic.db.engine import Database
from cosmic.db.schema_execution import fills, orders
from cosmic.reporting import daily
from cosmic.research.library import expect
from cosmic.trading import measured
from cosmic.trading.accounts import get_account
from cosmic.web.app import create_app
from tests.helpers import client_for, full_login
from tests.trading_env import Env, build_env

D = Decimal
AT = datetime(2026, 10, 28, tzinfo=UTC)
ACCOUNT = uuid.UUID(int=1)


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


def slip(
    side: str,
    expected: str,
    fill: str,
    *,
    venue: str = "BINANCE",
    environment: str = "LIVE",
    purpose: str = "ENTRY",
) -> measured.Slip:
    simulated = environment != "LIVE"
    return measured.Slip(
        venue, ACCOUNT, environment, simulated, purpose, side, D(expected), D(fill), AT
    )


def test_the_replays_assumption_is_the_one_measured_against() -> None:
    assert D(str(expect.SLIPPAGE)) * BPS == measured.REPLAY_SLIPPAGE_BPS


def test_a_fill_costs_what_it_paid_beyond_the_price_when_sent() -> None:
    assert slip("BUY", "100", "100.05").bps == D("5.00")  # paid 5 bps above the ask
    assert slip("SELL", "100", "99.90").bps == D("10.00")  # got 10 bps below the bid
    assert slip("SELL", "100", "100.10").bps == D("-10.00")  # did better: negative
    assert slip("BUY", "4000", "3999").bps == D("-2.50")
    assert slip("BUY", "0", "1").bps == 0  # no price to compare with


def test_paper_a_demo_and_real_money_are_kept_apart() -> None:
    q = measured.fill_quality(
        [
            slip("BUY", "100", "100.05", environment="PAPER"),
            slip("BUY", "100", "100.02"),
            slip("SELL", "100", "99.80", purpose="STOP_LOSS"),
            slip("BUY", "100", "100.10"),
            slip("BUY", "100", "100.00", venue="HFM", environment="DEMO"),
        ]
    )
    by = {(x.venue, x.environment): x for x in q}
    real = by[("BINANCE", "LIVE")]
    assert (real.fills, real.median_bps, real.worst_bps) == (2, D("6.00"), D(10))
    assert (real.stops, real.stop_median_bps, real.stop_worst_bps) == (1, D(20), D(20))
    assert real.label == "MEASURED" and not real.paper
    paper = by[("BINANCE", "PAPER")]
    assert (paper.fills, paper.label, paper.paper) == (1, "SIMULATED", True)
    demo = by[("HFM", "DEMO")]  # virtual money on the venue's own prices: never shown as real
    assert (demo.median_bps, demo.label, demo.paper) == (0, "SIMULATED", False)


def test_advice_only_from_enough_venue_fills_far_from_the_assumption() -> None:
    def q(median: str, fills: int = 5, environment: str = "LIVE") -> measured.FillQuality:
        return measured.FillQuality(
            "HFM", environment, environment != "LIVE", fills, D(median), D(median), 0
        )

    worse = measured.slippage_advice(q("12"))
    assert worse is not None and "Your HFM fills cost a median 12.00 bps" in worse
    assert "flatter" in worse
    better = measured.slippage_advice(q("-1"))
    assert better is not None and "cautious" in better
    assert measured.slippage_advice(q("9.99")) is None  # within 5 bps of the 5 assumed
    assert measured.slippage_advice(q("30", fills=2)) is None  # too few fills to judge
    assert measured.slippage_advice(q("30", environment="PAPER")) is None  # paper adds it itself
    demo = measured.slippage_advice(q("12", environment="DEMO"))
    assert demo is not None and "Your HFM demo fills" in demo and "flatter" in demo
    # a demo filling better than assumed proves nothing about a real account
    assert measured.slippage_advice(q("-1", environment="DEMO")) is None


def test_a_stop_that_gaps_is_shown_apart_and_never_judges_the_replays() -> None:
    """The replays fill a gapped stop at the gapped open: a gap is not slippage."""
    gapped = [slip("SELL", "2620", "2599", purpose="STOP_LOSS", environment="DEMO")] * 3
    sent = [slip("BUY", "2600", "2600.5", environment="DEMO")] * 3
    (q,) = measured.fill_quality(gapped + sent)
    assert (q.fills, q.median_bps, q.stops, q.stop_median_bps) == (3, D("1.92"), 3, D("80.15"))
    assert measured.slippage_advice(q) is None
    (only,) = measured.fill_quality(gapped)
    assert (only.fills, only.median_bps, only.worst_bps) == (0, None, None)
    assert measured.slippage_advice(only) is None


async def test_a_paper_entry_is_measured_against_the_ask_when_sent(env: Env) -> None:
    env.clock.advance(seconds=1)
    await env.set_market("82990.00", "83000.00")
    env.clock.advance(seconds=5)  # the check reads that quote, a few seconds old
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(2)  # sent at the 83,000 ask, filled by the paper simulator
    async with env.db.connect() as conn:
        slips = await measured.fill_slips(conn)
        account = await get_account(conn, alias="paper-main")
        paid = await measured.costs_paid(conn, account.id)
        fee = await conn.scalar(select(fills.c.fee).join(orders, orders.c.id == fills.c.order_id))
    assert len(slips) == 1
    s = slips[0]
    assert (s.purpose, s.side, s.environment, s.simulated, s.venue) == (
        "ENTRY",
        "BUY",
        "PAPER",
        True,
        "COINBASE",
    )
    assert s.expected == D("83000.00")  # the ask its pre-submission check recorded
    await env.set_market("83490.00", "83500.00")  # a newer stored price, as it was sent
    async with env.db.connect() as conn:  # sent at once: the check's price, not the store's
        assert [x.expected for x in await measured.fill_slips(conn)] == [D("83000.00")]
    assert s.fill == D("83000.00")  # the limit caps the simulator's 5 bps
    assert s.bps == 0
    assert paid == fee and paid > 0
    q = measured.fill_quality(slips)
    assert [x.label for x in q] == ["SIMULATED"]
    assert measured.slippage_advice(q[0]) is None


async def test_an_order_sent_late_is_measured_against_the_price_when_it_went_out(
    env: Env,
) -> None:
    """An exit can wait for its stop to be cancelled, or a venue to come back: the price may
    move meanwhile, and that move is not the fill's slippage."""
    env.clock.advance(seconds=1)
    await env.set_market("82990.00", "83000.00")
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(2)
    env.clock.advance(hours=2)
    await env.set_market("83990.00", "84010.00")
    sent = env.clock.now()
    async with env.db.transaction() as conn:
        await conn.execute(update(orders).values(submitted_at=sent))
    async with env.db.connect() as conn:
        slips = await measured.fill_slips(conn)
    assert [s.expected for s in slips] == [D("84010.00")]  # the ask when it went out
    assert slips[0].bps == D("-120.22")
    async with env.db.transaction() as conn:  # no stored price near when it went out
        await conn.execute(update(orders).values(submitted_at=sent + timedelta(hours=1)))
    async with env.db.connect() as conn:
        assert await measured.fill_slips(conn) == []
        assert await measured.fill_slips(conn, since=sent + timedelta(hours=2)) == []


async def test_the_risk_page_and_the_briefing_show_fill_prices(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(env.settings, db=env.db, clock=env.clock)
    async with client_for(app) as c:
        await full_login(c, env.owner, env.clock)
        page = (await c.get("/settings/risk")).text
        assert "Fill prices against the price when sent" in page
        assert "No fill in the last 90 days could be measured" in page
        env.clock.advance(seconds=1)
        await env.set_market("82990.00", "83000.00")
        p = await env.propose()
        await env.approve(p.intent_id)
        await env.cycle(2)
        page = (await c.get("/settings/risk")).text
        assert "No fill in the last" not in page
        assert "the paper simulator" in page and "SIMULATED" in page
        assert "The replays assume 5 bps a fill" in page
        async with env.db.connect() as conn:
            lines = await daily.briefing_lines(conn, env.clock.now())
        assert not any("fills cost a median" in line for line in lines)  # paper: no advice

        demo = [slip("BUY", "100", "100.12", venue="HFM", environment="DEMO")] * 3

        async def fake(conn: object, **kw: object) -> list[measured.Slip]:
            return demo

        monkeypatch.setattr(measured, "fill_slips", fake)
        page = (await c.get("/settings/risk")).text
        assert "the venue's demo fills, virtual money" in page
        assert "own fills, real money" not in page
        assert "Your HFM demo fills cost a median 12.00 bps" in page
        async with env.db.connect() as conn:
            lines = await daily.briefing_lines(conn, env.clock.now())
        assert any(
            line.startswith("- [SIMULATED · DEMO] Your HFM demo fills cost a median 12.00 bps")
            for line in lines
        )
