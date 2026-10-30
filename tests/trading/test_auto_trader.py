"""The demo auto-trader under a mandate (ADR-030): sizing, limits, approval, safety."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from cosmic.core.config import Settings
from cosmic.core.errors import PermissionDeniedError, StepUpRequiredError
from cosmic.core.ids import uuid7
from cosmic.db.engine import Database
from cosmic.db.schema import notifications
from cosmic.db.schema_execution import approvals, orders, trade_intents, trading_mandates
from cosmic.db.schema_market import fetch_runs, instruments, sources
from cosmic.research.library import auto
from cosmic.trading import killswitch, mandates
from cosmic.trading.mandates import Mandate, MandateTerms, Usage, entry_refusal
from tests.trading_env import BTC, Env, build_env

D = Decimal
REF = "paper-main [SIMULATED · PAPER]"


@pytest.fixture
async def env(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    # The test market is Coinbase paper: let the auto-trader read its own hourly bars.
    monkeypatch.setitem(mandates.AUTO_SIGNAL_SERIES, BTC, BTC)
    return await build_env(settings, db)


def terms(**kw: Any) -> MandateTerms:
    base: dict[str, Any] = {
        "account_alias": "paper-main",
        "strategy_key": "turtle_20_10",
        "instruments": (BTC,),
        "max_loss_per_trade": D(30),
        "max_trades_per_day": 5,
        "max_open_positions": 2,
        "daily_loss_limit": D(60),
        "days": 30,
    }
    return MandateTerms(**{**base, **kw})


async def grant(env: Env, **kw: Any) -> uuid.UUID:
    async with env.db.transaction() as conn:
        return await mandates.grant(
            conn,
            principal=env.principal(),
            terms=terms(**kw),
            now=env.clock.now(),
            settings=env.settings,
        )


async def hourly_bars(env: Env, closes: list[float], *, last_open: datetime) -> None:
    """Completed 1-hour bars ending with the bar opened at ``last_open`` (high/low ±20)."""
    async with env.db.transaction() as conn:
        inst = (
            await conn.execute(select(instruments.c.id).where(instruments.c.canonical_key == BTC))
        ).scalar_one()
        src = (
            await conn.execute(
                select(sources.c.id).where(sources.c.key == "coinbase_exchange_public")
            )
        ).scalar_one()
        run = uuid7()
        now = env.clock.now()
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
        first = last_open - timedelta(hours=len(closes) - 1)
        for i, c in enumerate(closes):
            t = first + timedelta(hours=i)
            await conn.execute(
                text(
                    "INSERT INTO bars (instrument_id, source_id, timeframe, open_time, open, high, "
                    "low, close, volume, is_complete, fetch_run_id, ingested_at, updated_at) "
                    "VALUES (:i, :s, '1h', :t, :c, :h, :l, :c, 1, true, :r, :n, :n) "
                    "ON CONFLICT (instrument_id, source_id, timeframe, open_time) DO UPDATE SET "
                    "open = :c, high = :h, low = :l, close = :c"
                ),
                {
                    "i": inst,
                    "s": src,
                    "t": t,
                    "c": D(str(c)),
                    "h": D(str(c + 20)),
                    "l": D(str(c - 20)),
                    "r": run,
                    "n": now,
                },
            )


def breakout(base: float = 82_900.0) -> list[float]:
    """70 quiet bars, then a close above the prior 20-bar high on the last one."""
    return [base + (10 if i % 2 else -10) for i in range(69)] + [base + 200]


async def kinds(env: Env) -> list[str]:
    async with env.db.connect() as conn:
        return [r.kind for r in await conn.execute(select(notifications.c.kind))]


# ---- pure rules ---------------------------------------------------------------------------------


def test_size_never_risks_more_than_the_mandate_allows() -> None:
    # HFM bitcoin: 0.01 lots, stop $1,150 below → 0.02 fits $30 (0.03 would risk ~$34.6).
    qty, per_unit = auto.size_for_max_loss(
        max_loss=D(30),
        entry=D(81_900),
        stop_limit=D(80_750),
        fee_bps=D(0),
        lot_size=D("0.01"),
        min_qty=D("0.01"),
        max_order_notional=D(5000),
    )
    assert qty == D("0.02") and qty * per_unit <= D(30)
    # HFM gold: 1 oz, stop $28 below plus fees on both sides fits; $35 below does not.
    ok, unit = auto.size_for_max_loss(
        max_loss=D(30),
        entry=D(4135),
        stop_limit=D(4107),
        fee_bps=D(2),
        lot_size=D(1),
        min_qty=D(1),
        max_order_notional=D(5000),
    )
    assert ok == D(1) and unit <= D(30)
    none, _ = auto.size_for_max_loss(
        max_loss=D(30),
        entry=D(4135),
        stop_limit=D(4100),
        fee_bps=D(2),
        lot_size=D(1),
        min_qty=D(1),
        max_order_notional=D(5000),
    )
    assert none == 0  # skipped, never resized past the limit
    capped, _ = auto.size_for_max_loss(
        max_loss=D(1000),
        entry=D(80_000),
        stop_limit=D(79_900),
        fee_bps=D(0),
        lot_size=D("0.0001"),
        min_qty=None,
        max_order_notional=D(500),
    )
    assert capped * D(80_000) <= D(500)  # the policy's order limit still binds


def test_each_mandate_limit_refuses_a_new_entry() -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=None)
    m = Mandate(
        uuid.uuid4(),
        uuid.uuid4(),
        "turtle_20_10",
        "1h",
        (BTC,),
        D(30),
        5,
        2,
        D(60),
        "ACTIVE",
        uuid.uuid4(),
        now,
        now + timedelta(days=1),
        None,
        None,
    )
    fresh = Usage(0, 0, D(0))
    assert entry_refusal(m, fresh, D("29.99")) is None
    assert "no protective stop" in str(entry_refusal(m, fresh, None))
    assert "exceeds 30.00" in str(entry_refusal(m, fresh, D("30.01")))
    assert "5 auto trades are used" in str(entry_refusal(m, Usage(5, 0, D(0)), D(10)))
    assert "2 position(s) are open" in str(entry_refusal(m, Usage(0, 2, D(0)), D(10)))
    assert "reached the 60.00 limit" in str(entry_refusal(m, Usage(0, 0, D(-60)), D(10)))


# ---- granting -----------------------------------------------------------------------------------


async def test_granting_needs_a_fresh_code_and_a_known_rule(env: Env) -> None:
    async with env.db.transaction() as conn:
        with pytest.raises(StepUpRequiredError):
            await mandates.grant(
                conn,
                principal=env.principal(mfa_fresh=False),
                terms=terms(),
                now=env.clock.now(),
                settings=env.settings,
            )
    with pytest.raises(Exception, match="not available to the auto-trader"):
        await grant(env, strategy_key="golden_cross_50_200")
    with pytest.raises(Exception, match="no signal series"):
        await grant(env, instruments=("SPOT:ETH/USD@COINBASE",))
    await grant(env)
    with pytest.raises(Exception, match="already has an active mandate"):
        await grant(env)
    assert "mandate_changed" in await kinds(env)


async def test_the_database_refuses_a_mandate_for_real_money(env: Env) -> None:
    """Even a code path that skipped every check cannot mandate a LIVE account without the
    owner's live activation (ADR-034; tests/trading/test_live_locks.py has the rest)."""
    async with env.db.connect() as conn:
        venue = await conn.scalar(text("SELECT venue_id FROM accounts WHERE alias='paper-main'"))
        owner = env.owner.user_id
    with pytest.raises(Exception, match="must ask before each trade"):
        async with env.db.transaction() as conn:
            live = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, alias, kind, venue_id, broker_adapter, "
                    "base_currency, status, reconciliation_status, created_at, updated_at, "
                    "key_fingerprint) VALUES (:id, 'real-one', 'LIVE', :v, 'binance_spot_live', "
                    "'USD', 'ACTIVE', 'UNKNOWN', now(), now(), '0123456789abcdef')"
                ),
                {"id": live, "v": venue},
            )
            await conn.execute(
                trading_mandates.insert().values(
                    id=uuid7(),
                    account_id=live,
                    strategy_key="turtle_20_10",
                    timeframe="1h",
                    instruments=[BTC],
                    max_loss_per_trade=D(30),
                    max_trades_per_day=5,
                    max_open_positions=2,
                    daily_loss_limit=D(60),
                    status="ACTIVE",
                    granted_by=owner,
                    granted_at=env.clock.now(),
                    expires_at=env.clock.now() + timedelta(days=1),
                )
            )
    mid = await grant(env)
    with pytest.raises(Exception, match="terms are immutable"):
        async with env.db.transaction() as conn:
            await conn.execute(
                trading_mandates.update()
                .where(trading_mandates.c.id == mid)
                .values(max_loss_per_trade=D(3000))
            )


# ---- trading ------------------------------------------------------------------------------------


async def test_a_breakout_trades_under_the_mandate_and_the_breakdown_sells(env: Env) -> None:
    mid = await grant(env)
    last = env.clock.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    await hourly_bars(env, breakout(), last_open=last)
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    assert await auto.run(env.db, env.clock) == []  # the same bar is handled once
    async with env.db.connect() as conn:
        intent = (await conn.execute(select(trade_intents))).one()
        appr = (await conn.execute(select(approvals))).one()
        packet = await conn.scalar(text("SELECT packet FROM approval_packets"))
    assert intent.status == "APPROVED" and intent.details["mandate_id"] == str(mid)
    assert (appr.channel, appr.mandate_id, appr.operator_user_id) == (
        "MANDATE",
        mid,
        env.owner.user_id,
    )
    assert D(str(packet["risk"]["estimated_max_loss"])) <= D(30)
    sent = await kinds(env)
    assert "auto_trade" in sent and "approval_requested" not in sent  # nothing to approve
    await env.cycle(2)
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.purpose == "ENTRY"))).one()
    assert entry.status == "FILLED"
    # Two hours later the price closes below the prior 10-bar low: sell what the rule bought.
    env.clock.advance(hours=2)
    # Fresh prices above the protective stop (stale prices are never used; below the stop, the
    # stop would sell first and the rule's exit would find nothing left to sell).
    await env.set_market("83000.00", "83000.50")
    later = last + timedelta(hours=2)
    await hourly_bars(env, [*breakout(), 83_150.0, 82_000.0], last_open=later)
    assert await auto.run(env.db, env.clock) == [f"{BTC}:APPROVED"]
    await env.cycle(2)
    async with env.db.connect() as conn:
        sides = [
            (r.side, r.status)
            for r in await conn.execute(select(trade_intents).order_by(trade_intents.c.created_at))
        ]
        exit_order = (await conn.execute(select(orders).where(orders.c.purpose == "EXIT"))).one()
    assert sides[0][0] == "BUY" and sides[1][0] == "SELL"
    assert exit_order.status == "FILLED"


async def test_the_limits_skip_trades_and_say_why(env: Env) -> None:
    await grant(env, max_loss_per_trade=D("0.01"))
    last = env.clock.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    await hourly_bars(env, breakout(), last_open=last)
    assert await auto.run(env.db, env.clock) == [f"{BTC}:NO_TRADE"]
    async with env.db.connect() as conn:
        skipped = await conn.scalar(
            select(notifications.c.payload).where(notifications.c.kind == "auto_trade_skipped")
        )
        assert await conn.scalar(select(trade_intents.c.id)) is None
    # Too small to trade: on this market the venue's minimum order value binds first (the
    # smallest-lot case is in test_size_never_risks_more_than_the_mandate_allows).
    assert "smallest trade" in skipped["reason"] or "minimum order value" in skipped["reason"]
    assert skipped["account_ref"] == REF


async def test_the_day_limit_blocks_an_entry(env: Env) -> None:
    mid = await grant(env)
    async with env.db.connect() as conn:
        m = await mandates.get_mandate(conn, mid)
    # As if today's trades were already used.
    used = Usage(m.max_trades_per_day, 0, D(0))
    assert entry_refusal(m, used, D(1)) is not None


async def test_stopping_or_an_emergency_stop_means_no_auto_trade(env: Env) -> None:
    mid = await grant(env)
    async with env.db.transaction() as conn:
        assert await mandates.revoke(
            conn, principal=env.principal(), mandate_id=mid, reason="test", now=env.clock.now()
        )
    last = env.clock.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    await hourly_bars(env, breakout(), last_open=last)
    assert await auto.run(env.db, env.clock) == []
    with pytest.raises(Exception, match="an ended mandate stays ended"):
        async with env.db.transaction() as conn:
            await conn.execute(
                trading_mandates.update()
                .where(trading_mandates.c.id == mid)
                .values(status="ACTIVE", ended_at=None, ended_by=None, end_reason=None)
            )
    # A new mandate, then the emergency stop: the entry is refused by the mode.
    await grant(env)
    async with env.db.transaction() as conn:
        await killswitch.activate(
            conn,
            actor=env.principal().actor,
            principal=env.principal(),
            reason="test",
            now=env.clock.now(),
        )
    env.clock.advance(hours=1)
    await hourly_bars(env, [*breakout(), 83_300.0], last_open=last + timedelta(hours=1))
    out = await auto.run(env.db, env.clock)
    async with env.db.connect() as conn:
        intent = (await conn.execute(select(trade_intents))).first()
    assert out in ([], [f"{BTC}:NON_EXECUTABLE"], [f"{BTC}:REFUSED"])
    assert intent is None or intent.status in ("NON_EXECUTABLE", "BLOCKED")
    async with env.db.connect() as conn:
        assert await conn.scalar(select(approvals.c.id)) is None  # nothing approved


async def test_a_mandate_approval_needs_an_active_mandate(env: Env) -> None:
    """The database refuses a MANDATE approval whose mandate has ended."""
    mid = await grant(env)
    p = await env.propose()
    async with env.db.transaction() as conn:
        await mandates.revoke(
            conn, principal=env.principal(), mandate_id=mid, reason="test", now=env.clock.now()
        )
    with pytest.raises(Exception, match="needs an active mandate"):
        async with env.db.transaction() as conn:
            packet = (
                await conn.execute(
                    text("SELECT id, packet_hash, policy_version FROM approval_packets")
                )
            ).one()
            await conn.execute(
                approvals.insert().values(
                    id=uuid7(),
                    intent_id=p.intent_id,
                    packet_id=packet.id,
                    packet_hash=packet.packet_hash,
                    decision="APPROVED",
                    operator_user_id=env.owner.user_id,
                    channel="MANDATE",
                    mandate_id=mid,
                    nonce=uuid7().hex,
                    policy_version=packet.policy_version,
                    decided_at=env.clock.now(),
                    expires_at=env.clock.now() + timedelta(minutes=5),
                    status="ACTIVE",
                )
            )


async def test_only_the_owner_with_approval_rights_can_stop_it(env: Env) -> None:
    mid = await grant(env)
    from cosmic.identity.capabilities import Role
    from tests.helpers import make_user, principal_for

    viewer = await make_user(env.db, env.clock, username="viewer1", roles=[Role.VIEWER])
    async with env.db.transaction() as conn:
        with pytest.raises(PermissionDeniedError):
            await mandates.revoke(
                conn,
                principal=principal_for(viewer, env.clock, roles=("VIEWER",)),
                mandate_id=mid,
                reason="x",
                now=env.clock.now(),
            )
