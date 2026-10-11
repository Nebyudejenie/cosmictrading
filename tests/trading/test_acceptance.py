"""V6 §1831 required automated acceptance tests (paper trading; live is unreachable)."""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update

from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, notifications
from cosmic.db.schema_execution import (
    approvals,
    fills,
    order_events,
    orders,
    paper_balances,
    paper_orders,
    trade_intents,
)
from cosmic.db.schema_trading import accounts, positions, risk_check_runs
from cosmic.ops.incidents import Category, Severity, raise_incident
from cosmic.trading import authority
from cosmic.trading.accounts import get_account
from cosmic.trading.approvals import decide, issue_challenge
from cosmic.trading.brokers.paper import PaperConfig
from cosmic.trading.execution import ExecutionService
from cosmic.trading.modes import Mode
from cosmic.trading.policy import DEFAULT_POLICY, change_policy
from tests.trading_env import BTC, Env, build_env

D = Decimal


@pytest.fixture
async def env(settings: Settings, db: Database) -> Env:
    return await build_env(settings, db)


async def intent_status(env: Env, intent_id: object) -> str:
    async with env.db.connect() as conn:
        return str(
            (
                await conn.execute(
                    select(trade_intents.c.status).where(trade_intents.c.id == intent_id)
                )
            ).scalar_one()
        )


async def test_happy_path_paper_trade_end_to_end(env: Env) -> None:
    p = await env.propose()
    assert p.status == "PROPOSED", p.problems
    await env.approve(p.intent_id)
    await env.cycle()  # execute → ACKNOWLEDGED (no fill yet: the venue matches on tick)
    await env.cycle()  # tick fills → sync → FILLED → stop placed
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.purpose == "ENTRY"))).one()
        pos = (await conn.execute(select(positions))).one()
        stop = (await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))).one()
    assert entry.status == "FILLED" and entry.filled_qty == D("0.004")
    assert entry.avg_fill_price == D("83000.00")  # ask × (1 + 5 bps slippage) capped at the limit
    assert pos.quantity == D("0.004") and pos.protection_state == "ACTIVE"
    assert stop.status == "ACKNOWLEDGED" and stop.quantity == D("0.004") and stop.reduce_only
    assert await intent_status(env, p.intent_id) == "EXECUTED"
    result = await env.svc.reconciler.reconcile(env.account_id, trigger="test")
    assert result.passed, result.mismatches


async def test_at01_research_only_rejects_orders(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db, mode=Mode.RESEARCH_ONLY)
    p = await env.propose()
    assert p.status == "NON_EXECUTABLE"
    assert any(x.startswith("R01") for x in p.problems)
    await env.cycle()
    assert await env.count(paper_orders) == 0


async def test_at01_mode_change_after_approval_blocks(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    async with env.db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=env.principal(),
            target=Mode.RESEARCH_ONLY,
            reason="stop trading for the day",
            now=env.clock.now(),
            settings=env.settings,
        )
    await env.cycle()
    assert await env.count(paper_orders) == 0
    assert await intent_status(env, p.intent_id) == "BLOCKED"


async def test_at02_paper_cannot_reach_live(env: Env) -> None:
    async with env.db.transaction() as conn:
        await conn.execute(
            accounts.update()
            .where(accounts.c.id == env.account_id)
            .values(kind="LIVE", broker_adapter="live-x")
        )
        acct = await get_account(conn, account_id=env.account_id)
    with pytest.raises(ConflictError, match="no broker adapter"):
        env.svc.brokers.for_account(acct)
    paper_src = (
        Path(__file__).resolve().parents[2] / "src" / "cosmic" / "trading" / "brokers" / "paper.py"
    )
    imported = {
        n.names[0].name
        for n in ast.walk(ast.parse(paper_src.read_text()))
        if isinstance(n, ast.Import)
    }
    imported |= {
        n.module or ""
        for n in ast.walk(ast.parse(paper_src.read_text()))
        if isinstance(n, ast.ImportFrom)
    }
    assert not {
        m
        for m in imported
        if m.split(".")[0] in {"httpx", "requests", "aiohttp", "socket", "urllib"}
    }
    assert env.svc.paper.capabilities().is_live is False


async def test_at03_no_submission_without_approval(env: Env) -> None:
    p = await env.propose()
    assert p.status == "PROPOSED"
    for _ in range(3):
        await env.cycle()
    assert await env.count(orders) == 0 and await env.count(paper_orders) == 0
    await env.approve(p.intent_id)
    await env.cycle()
    assert await env.count(paper_orders) == 1


async def test_at04_approval_single_use_and_not_replayable(env: Env) -> None:
    p = await env.propose()
    pr = env.principal()
    async with env.db.transaction() as conn:
        ch = await issue_challenge(
            conn, principal=pr, intent_id=p.intent_id, channel="PORTAL", now=env.clock.now()
        )
    async with env.db.transaction() as conn:
        await decide(
            conn,
            principal=pr,
            token=ch.token,
            packet_hash=ch.packet_hash,
            decision="APPROVE",
            now=env.clock.now(),
            settings=env.settings,
        )
    with pytest.raises(ConflictError, match="already used"):
        async with env.db.transaction() as conn:
            await decide(
                conn,
                principal=pr,
                token=ch.token,
                packet_hash=ch.packet_hash,
                decision="APPROVE",
                now=env.clock.now(),
                settings=env.settings,
            )
    with pytest.raises(ConflictError):
        async with env.db.transaction() as conn:  # approving an already-approved intent again
            await issue_challenge(
                conn, principal=pr, intent_id=p.intent_id, channel="PORTAL", now=env.clock.now()
            )
    await env.cycle()
    await env.cycle()
    assert await env.count(orders, orders.c.purpose == "ENTRY") == 1
    async with env.db.connect() as conn:
        status = (await conn.execute(select(approvals.c.status))).scalar_one()
    assert status == "CONSUMED"
    # The consumed approval cannot drive another execution.
    out = await env.svc.execution.execute_intent(p.intent_id)
    assert out.code == "SKIPPED"
    assert await env.count(paper_orders) == 2  # entry + protective stop, nothing more


async def test_at04_wrong_operator_channel_or_hash(env: Env) -> None:
    from tests.helpers import make_user, principal_for

    p = await env.propose()
    pr = env.principal()
    async with env.db.transaction() as conn:
        ch = await issue_challenge(
            conn, principal=pr, intent_id=p.intent_id, channel="PORTAL", now=env.clock.now()
        )
    other = principal_for(await make_user(env.db, env.clock, username="owner2"), env.clock)
    for kwargs, msg in (
        ({"principal": other, "packet_hash": ch.packet_hash}, "another operator"),
        ({"principal": pr, "packet_hash": "0" * 64}, "do not match"),
        (
            {
                "principal": pr,
                "packet_hash": ch.packet_hash,
                "channel": "TELEGRAM",
                "second_factor_ok": True,
                "challenge_id": ch.challenge_id,  # portal challenge replayed via Telegram
            },
            "another operator or channel",
        ),
    ):
        with pytest.raises(ConflictError, match=msg):
            async with env.db.transaction() as conn:
                await decide(
                    conn,
                    token=ch.token,
                    decision="APPROVE",
                    now=env.clock.now(),
                    settings=env.settings,
                    **kwargs,
                )  # type: ignore[arg-type]
    assert await intent_status(env, p.intent_id) == "PROPOSED"


async def test_at05_expired_approval_cannot_execute(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    env.clock.advance(seconds=DEFAULT_POLICY.approval_ttl_seconds + 1)
    await env.set_market("82986.50", "82986.51")  # data is fresh; only the approval is stale
    from cosmic.trading.approvals import expire_sweep

    out = await env.svc.execution.execute_intent(p.intent_id)
    assert out.code == "BLOCKED"
    async with env.db.transaction() as conn:
        await expire_sweep(conn, now=env.clock.now())
    assert await env.count(paper_orders) == 0
    assert await intent_status(env, p.intent_id) == "REQUIRES_REAPPROVAL"


async def test_at05_challenge_and_intent_expiry(env: Env) -> None:
    p = await env.propose()
    pr = env.principal()
    async with env.db.transaction() as conn:
        ch = await issue_challenge(
            conn, principal=pr, intent_id=p.intent_id, channel="PORTAL", now=env.clock.now()
        )
    env.clock.advance(minutes=6)
    with pytest.raises(ConflictError, match="expired"):
        async with env.db.transaction() as conn:
            await decide(
                conn,
                principal=env.principal(),
                token=ch.token,
                packet_hash=ch.packet_hash,
                decision="APPROVE",
                now=env.clock.now(),
                settings=env.settings,
            )
    env.clock.advance(hours=2)
    from cosmic.trading.approvals import expire_sweep

    async with env.db.transaction() as conn:
        await expire_sweep(conn, now=env.clock.now())
    assert await intent_status(env, p.intent_id) == "EXPIRED"


async def test_at05_materially_changed_conditions(env: Env) -> None:
    # (a) price leaves the approved band after approval → no order, re-approval required
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.set_market("84000.00", "84000.01")
    await env.cycle()
    assert await env.count(paper_orders) == 0
    assert await intent_status(env, p.intent_id) == "REQUIRES_REAPPROVAL"
    # (b) risk policy changes after approval → approval no longer valid
    env.clock.advance(seconds=1)
    await env.set_market("82986.50", "82986.51")
    # BTC/BUY is still pending re-approval, so a second BTC buy is refused as a duplicate (R12).
    dup = await env.propose(price_band_low=D("82500.00"))
    assert dup.status == "NON_EXECUTABLE" and any(x.startswith("R12") for x in dup.problems)
    p2 = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2508.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={},
    )
    await env.approve(p2.intent_id)
    async with env.db.transaction() as conn:
        await change_policy(
            conn,
            principal=env.principal(),
            new_params=DEFAULT_POLICY.model_dump(mode="python") | {"max_order_notional": D("400")},
            reason="tighten order size after review",
            now=env.clock.now(),
            settings=env.settings,
        )
    await env.cycle()
    assert await env.count(paper_orders) == 0
    assert await intent_status(env, p2.intent_id) == "REQUIRES_REAPPROVAL"


async def test_at06_execution_matches_approved_terms_exactly(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle()
    async with env.db.connect() as conn:
        intent = (
            await conn.execute(select(trade_intents).where(trade_intents.c.id == p.intent_id))
        ).one()
        po = (await conn.execute(select(paper_orders))).one()
        o = (await conn.execute(select(orders))).one()
    assert (
        po.instrument_key,
        po.side,
        po.order_type,
        po.quantity,
        po.limit_price,
        po.time_in_force,
    ) == (
        BTC,
        intent.side,
        intent.order_type,
        intent.quantity,
        intent.limit_price,
        intent.time_in_force,
    )
    assert po.account_ref == intent.account_id == o.account_id
    assert o.client_order_id == po.client_order_id == f"ct-{intent.trade_code}-E"


async def test_at07_risk_veto_after_approval(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    async with env.db.transaction() as conn:
        await raise_incident(
            conn,
            dedup_key="sec.test",
            category=Category.SECURITY,
            severity=Severity.CRITICAL,
            title="suspicious login",
            now=env.clock.now(),
        )
    await env.cycle()
    assert await env.count(paper_orders) == 0
    assert await intent_status(env, p.intent_id) == "BLOCKED"
    async with env.db.connect() as conn:
        runs = list(
            await conn.execute(
                select(risk_check_runs)
                .where(risk_check_runs.c.intent_id == p.intent_id)
                .order_by(risk_check_runs.c.created_at)
            )
        )
    assert [r.stage for r in runs] == ["PRE_APPROVAL", "PRE_SUBMISSION"]
    assert runs[-1].decision == "FAIL"
    assert any(c["code"] == "R15" and c["status"] == "FAIL" for c in runs[-1].results)


async def test_at08_strategy_authority_locked(env: Env) -> None:
    from cosmic.identity.capabilities import SERVICE_CAPABILITIES, Capability, ServiceIdentity

    with pytest.raises(ConflictError):
        async with env.db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=env.principal(),
                target=Mode.STRATEGY_AUTHORIZED,
                reason="try strategy mandate",
                now=env.clock.now(),
                settings=env.settings,
            )
    assert (
        Capability.ENABLE_STRATEGY_AUTHORITY
        not in SERVICE_CAPABILITIES[ServiceIdentity.RESEARCH_WORKER]
    )
    async with env.db.connect() as conn:
        assert (await authority.get_state(conn)).mode is Mode.PAPER_TRADING


async def test_at09_no_duplicate_exposure(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    # Two execution attempts racing (e.g. duplicate callback + retry).
    outcomes = await asyncio.gather(
        env.svc.execution.execute_intent(p.intent_id),
        env.svc.execution.execute_intent(p.intent_id),
        return_exceptions=True,
    )
    codes = sorted(o.code if hasattr(o, "code") else type(o).__name__ for o in outcomes)
    assert codes.count("ACKNOWLEDGED") == 1
    assert await env.count(paper_orders) == 1
    # Duplicate fill reports from the venue are recorded once.
    await env.svc.paper.inject_fault("DUPLICATE_FILL_REPORT", 3)
    await env.cycle(2)
    assert (
        await env.count(
            fills, fills.c.order_id.in_(select(orders.c.id).where(orders.c.purpose == "ENTRY"))
        )
        == 1
    )
    async with env.db.connect() as conn:
        pos = (await conn.execute(select(positions.c.quantity))).scalar_one()
    assert pos == D("0.004")


async def test_at09_timeout_does_not_resubmit(env: Env) -> None:
    await env.svc.paper.inject_fault("TIMEOUT_AFTER_ACCEPT")
    p = await env.propose()
    await env.approve(p.intent_id)
    out = await env.svc.execution.execute_intent(p.intent_id)
    assert out.code == "UNKNOWN"
    for _ in range(3):
        await env.svc.execution.execute_intent(p.intent_id)
    assert await env.count(paper_orders) == 1  # never resubmitted
    await env.svc.execution.poll_active()  # reconciles by client order ID
    async with env.db.connect() as conn:
        o = (await conn.execute(select(orders))).one()
    assert o.status == "ACKNOWLEDGED" and o.broker_order_id is not None


async def test_at10_partial_fills_drive_exit_quantities(settings: Settings, db: Database) -> None:
    env = await build_env(settings, db, paper_config=PaperConfig(max_fill_fraction=D("0.5")))
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle()  # acknowledged
    await env.cycle()  # half filled → stop for half
    async with env.db.connect() as conn:
        entry = (await conn.execute(select(orders).where(orders.c.purpose == "ENTRY"))).one()
        stops = list(await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS")))
        pos = (await conn.execute(select(positions.c.quantity))).scalar_one()
    assert entry.status == "PARTIALLY_FILLED" and entry.filled_qty == D("0.002")
    assert pos == D("0.002")
    assert [s.quantity for s in stops] == [D("0.002")]  # permitted exit = filled quantity only
    await env.cycle()  # rest filled → second stop for the new delta
    async with env.db.connect() as conn:
        stops = list(
            await conn.execute(
                select(orders).where(orders.c.purpose == "STOP_LOSS").order_by(orders.c.created_at)
            )
        )
        pos = (await conn.execute(select(positions.c.quantity))).scalar_one()
    assert pos == D("0.004")
    assert sum(s.quantity for s in stops) == D("0.004")


async def test_at11_unknown_state_blocks_new_exposure(env: Env) -> None:
    await env.svc.paper.inject_fault("TIMEOUT_AFTER_ACCEPT")
    p = await env.propose()
    await env.approve(p.intent_id)
    assert (await env.svc.execution.execute_intent(p.intent_id)).code == "UNKNOWN"
    p2 = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2508.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={},
    )
    assert p2.status == "NON_EXECUTABLE"
    assert any(x.startswith("R03") for x in p2.problems)
    result = await env.svc.reconciler.reconcile(env.account_id, trigger="unknown_order")
    assert result.passed, result.mismatches
    p3 = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2508.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={},
    )
    assert p3.status == "PROPOSED", p3.problems


async def test_at12_kill_switch(env: Env) -> None:
    from cosmic.audit.service import Actor
    from cosmic.trading import killswitch

    filled = await env.propose()
    await env.approve(filled.intent_id)
    await env.cycle(2)  # filled + stop placed
    working = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2500.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={},
    )
    await env.approve(working.intent_id)
    await env.cycle()  # resting ETH entry below the ask: acknowledged, unfilled
    pending = await env.propose(quantity=D("0.001"), limit_price=D("82990.00"))
    assert pending.status == "PROPOSED"  # awaiting approval when the stop is pressed
    audit_before = await env.count(audit_events)
    act = await env.svc.emergency_stop(
        actor=Actor.user(env.owner.user_id), reason="test emergency", principal=env.principal()
    )
    assert act.entry_orders_to_cancel
    async with env.db.connect() as conn:
        state = await authority.get_state(conn)
        eth_entry = (
            await conn.execute(
                select(orders).where(
                    orders.c.client_order_id.like("%-E"),
                    orders.c.side == "BUY",
                    orders.c.quantity == D("0.1"),
                )
            )
        ).one()
        stop = (await conn.execute(select(orders).where(orders.c.purpose == "STOP_LOSS"))).one()
        pos = (
            await conn.execute(select(positions.c.quantity).where(positions.c.quantity > 0))
        ).scalar_one()
    assert state.mode is Mode.EMERGENCY_STOP
    assert eth_entry.status == "CANCELLED"  # unfilled entry cancelled, confirmed by the venue
    assert stop.status == "ACKNOWLEDGED"  # protective order NOT cancelled
    assert pos == D("0.004")  # position NOT liquidated
    assert await env.count(audit_events) > audit_before  # history preserved and extended
    assert await intent_status(env, pending.intent_id) == "BLOCKED"  # queued proposal rejected
    new = await env.propose(
        instrument_key="SPOT:ETH/USD@COINBASE",
        quantity=D("0.1"),
        limit_price=D("2508.00"),
        price_band_low=D("2490.00"),
        price_band_high=D("2525.00"),
        management_plan={},
    )
    assert new.status == "NON_EXECUTABLE"
    # Resume needs the checklist + operator confirmation and cannot exceed the pre-stop mode.
    with pytest.raises(ConflictError, match="positions"):
        async with env.db.transaction() as conn:
            await killswitch.resume(
                conn,
                principal=env.principal(),
                target=Mode.PAPER_TRADING,
                reason="incident reviewed and closed",
                positions_reviewed=False,
                now=env.clock.now(),
                settings=env.settings,
            )
    async with env.db.transaction() as conn:
        mode = await killswitch.resume(
            conn,
            principal=env.principal(),
            target=Mode.PAPER_TRADING,
            reason="incident reviewed and closed",
            positions_reviewed=True,
            now=env.clock.now(),
            settings=env.settings,
        )
    assert mode is Mode.PAPER_TRADING


async def test_at14_stale_data_blocks(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    env.clock.advance(minutes=5)  # quotes are now older than the 120 s policy limit
    await env.cycle()
    assert await env.count(paper_orders) == 0
    assert await intent_status(env, p.intent_id) == "REQUIRES_REAPPROVAL"
    fresh = await env.propose()
    assert fresh.status == "NON_EXECUTABLE"
    assert any(x.startswith("R09") for x in fresh.problems)


async def test_at15_account_mismatch_blocks(env: Env) -> None:
    async with env.db.transaction() as conn:  # the venue shows BTC we never bought
        await conn.execute(
            paper_balances.insert().values(
                account_ref=env.account_id,
                asset="BTC",
                total=D("0.5"),
                hold=D("0"),
                updated_at=env.clock.now(),
            )
        )
    result = await env.svc.reconciler.reconcile(env.account_id, trigger="test")
    assert not result.passed
    assert any("POSITIONS BTC" in m for m in result.mismatches)
    async with env.db.connect() as conn:
        acct = await get_account(conn, account_id=env.account_id)
    assert acct.reconciliation_status == "FAILED"
    p = await env.propose()
    assert p.status == "NON_EXECUTABLE"
    assert any(x.startswith("R03") or x.startswith("R15") for x in p.problems)


async def test_at17_broker_confirmed_vs_local(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(2)
    async with env.db.connect() as conn:
        entry_id = (
            await conn.execute(select(orders.c.id).where(orders.c.purpose == "ENTRY"))
        ).scalar_one()
        evs = [
            (e.to_status, e.source)
            for e in await conn.execute(
                select(order_events)
                .where(order_events.c.order_id == entry_id)
                .order_by(order_events.c.seq)
            )
        ]
        kinds = {n.kind: n.payload for n in await conn.execute(select(notifications))}
    assert evs == [
        ("CREATED", "LOCAL"),
        ("VALIDATED", "LOCAL"),
        ("AUTHORIZED", "LOCAL"),
        ("SUBMITTING", "LOCAL"),
        ("SUBMITTED", "LOCAL"),
        ("ACKNOWLEDGED", "BROKER"),
        ("FILLED", "BROKER"),
    ]
    assert (
        kinds["order_fill"]["basis"] == "BROKER_CONFIRMED"
        and kinds["order_fill"]["simulated"] is True
    )


async def test_at18_owner_can_see_why(env: Env) -> None:
    p = await env.propose(quantity=D("0.02"))  # notional ≈ 1,660 > 500 order limit
    assert p.status == "NON_EXECUTABLE"
    async with env.db.connect() as conn:
        intent = (
            await conn.execute(select(trade_intents).where(trade_intents.c.id == p.intent_id))
        ).one()
        run = (
            await conn.execute(
                select(risk_check_runs).where(risk_check_runs.c.intent_id == p.intent_id)
            )
        ).one()
    assert "R05" in intent.status_reason
    checks = {c["code"]: c for c in run.results}
    assert checks["R05"]["status"] == "FAIL" and "limit 500" in checks["R05"]["message"]
    assert all(
        c["status"] == "PASS" for code, c in checks.items() if code not in ("R05", "R06", "R07")
    )
    assert run.context["market"]["quote_label"] == "DELAYED"


async def test_at20_restart_recovers_without_resubmitting(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)
    # Simulate a crash after the venue accepted the order but before the response was recorded.
    await env.svc.paper.inject_fault("TIMEOUT_AFTER_ACCEPT")
    original = env.svc.execution._record_unknown

    async def crash(*_a: object, **_k: object) -> object:
        raise RuntimeError("process killed")

    env.svc.execution._record_unknown = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await env.svc.execution.execute_intent(p.intent_id)
    env.svc.execution._record_unknown = original  # type: ignore[method-assign]
    async with env.db.connect() as conn:
        assert (await conn.execute(select(orders.c.status))).scalar_one() == "SUBMITTING"
    # "Restart": a fresh execution authority performs startup recovery.
    fresh = ExecutionService(env.db, env.clock, env.settings, env.svc.brokers)
    assert await fresh.startup_recovery() == 1
    async with env.db.connect() as conn:
        o = (await conn.execute(select(orders))).one()
    assert o.status == "ACKNOWLEDGED"  # resolved from venue evidence
    assert await env.count(paper_orders) == 1  # no second submission
    for _ in range(2):
        await fresh.run_cycle()
    assert await env.count(paper_orders, paper_orders.c.client_order_id.like("%-E")) == 1


async def test_at20_authorized_but_never_sent_is_closed(env: Env) -> None:
    p = await env.propose()
    await env.approve(p.intent_id)

    async def die(*_a: object, **_k: object) -> object:
        raise RuntimeError("process killed before Tx B")

    env.svc.execution._transmit = die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await env.svc.execution.execute_intent(p.intent_id)
    fresh = ExecutionService(env.db, env.clock, env.settings, env.svc.brokers)
    await fresh.startup_recovery()
    async with env.db.connect() as conn:
        o = (await conn.execute(select(orders))).one()
    assert o.status == "REJECTED"
    assert await env.count(paper_orders) == 0
    assert await intent_status(env, p.intent_id) == "EXECUTION_FAILED"
    async with env.db.connect() as conn:
        assert await env.svc.paper.list_open_orders(env.account_id) == []
        assert (await get_account(conn, account_id=env.account_id)).reconciliation_status == "OK"


async def test_reconciliation_detects_unexpected_venue_fill_order(env: Env) -> None:
    from cosmic.trading.brokers.base import SubmitRequest

    await env.svc.paper.submit_order(
        env.account_id,
        SubmitRequest(
            client_order_id="manual-outside-system",
            instrument_key=BTC,
            side="BUY",
            order_type="LIMIT",
            quantity=D("0.001"),
            limit_price=D("80000.00"),
            stop_price=None,
            time_in_force="GTC",
            expires_at=None,
        ),
    )
    result = await env.svc.reconciler.reconcile(env.account_id, trigger="test")
    assert not result.passed
    assert any("unknown internally" in m for m in result.mismatches)
    async with env.db.connect() as conn:
        _ = timedelta(0)
        assert (
            await get_account(conn, account_id=env.account_id)
        ).reconciliation_status == "FAILED"


async def test_kill_switch_works_without_a_valid_risk_policy(
    settings: Settings, db: Database, superuser_conn: object
) -> None:
    from cosmic.audit.service import Actor
    from cosmic.core.clock import FrozenClock
    from tests.trading_env import CAPTURE

    clock = FrozenClock(CAPTURE)
    async with db.transaction() as conn:  # no policy exists at all
        from cosmic.trading import killswitch

        act = await killswitch.activate(
            conn, actor=Actor.system("test"), reason="policy missing", now=clock.now()
        )
    assert act.already_stopped is False
    async with db.connect() as conn:
        assert (await authority.get_state(conn)).mode is Mode.EMERGENCY_STOP


async def test_resume_refused_until_a_reconciliation_after_the_stop_passes(env: Env) -> None:
    """The recovery checklist needs every active account reconciled *after* the stop."""
    from cosmic.audit.service import Actor
    from cosmic.trading import killswitch

    await env.svc.emergency_stop(
        actor=Actor.user(env.owner.user_id), reason="test emergency", principal=env.principal()
    )
    async with env.db.transaction() as conn:  # as if the stop-triggered reconciliation never ran
        await conn.execute(
            update(accounts)
            .where(accounts.c.id == env.account_id)
            .values(last_reconciled_at=env.clock.now() - timedelta(minutes=1))
        )
    with pytest.raises(ConflictError, match="reconciled successfully after the stop"):
        async with env.db.transaction() as conn:
            await killswitch.resume(
                conn,
                principal=env.principal(),
                target=Mode.PAPER_TRADING,
                reason="incident reviewed and closed",
                positions_reviewed=True,
                now=env.clock.now(),
                settings=env.settings,
            )
    env.clock.advance(seconds=5)
    await env.svc.reconciler.reconcile(env.account_id, trigger="operator")
    async with env.db.transaction() as conn:
        mode = await killswitch.resume(
            conn,
            principal=env.principal(),
            target=Mode.PAPER_TRADING,
            reason="incident reviewed and closed",
            positions_reviewed=True,
            now=env.clock.now(),
            settings=env.settings,
        )
    assert mode is Mode.PAPER_TRADING


async def test_approval_packet_contains_every_material_term(env: Env) -> None:
    """TRD-07/TRD-12 (V6 §1807): the hashed packet carries every term the owner approves."""
    from cosmic.trading.proposals import latest_packet, packet_hash

    p = await env.propose()
    async with env.db.connect() as conn:
        row = await latest_packet(conn, p.intent_id)
    pk = row.packet
    assert set(pk["plain_language"]) == {
        "action",
        "account",
        "why",
        "key_risk",
        "expires",
        "consequence_of_approving",
    }
    assert {"alias", "kind", "venue"} <= set(pk["account"]) and pk["account"]["kind"] == "PAPER"
    assert {"canonical_key", "venue", "base", "quote", "session"} <= set(pk["instrument"])
    assert {
        "side",
        "effect",
        "quantity",
        "order_type",
        "limit_price",
        "price_band",
        "max_slippage_bps",
        "time_in_force",
        "max_notional",
    } <= set(pk["order"])
    assert pk["costs"]["fee_basis"].startswith("ASSUMPTION")
    assert "Free AI does not make execution free" in pk["costs"]["note"]  # V6 §1834
    assert "not a guaranteed maximum loss" in pk["risk"]["warning"]  # V6 §1834
    assert {"estimated_max_loss", "warning", "position_after", "limit_semantics"} <= set(pk["risk"])
    assert pk["market_at_proposal"]["label"] == "DELAYED"
    for key in (
        "thesis",
        "horizon",
        "evidence",
        "counterarguments",
        "red_team",
        "scenarios",
        "invalidation",
        "entry_conditions",
        "management_plan",
        "prohibited_actions",
        "checks",
        "policy_version",
        "intent_expires_at",
        "approval_ttl_seconds",
        "mode",
        "trade_code",
    ):
        assert pk.get(key) not in (None, "", []), key
    # an entry is checked against R01–R16; R17 (reduce-only bound) applies to exits only
    assert {c["code"] for c in pk["checks"]} == {f"R{i:02d}" for i in range(1, 17)}
    assert pk["management_plan"]["atomic_bracket"].startswith("not available")
    assert row.packet_hash == packet_hash(pk)  # the stored hash is the hash of exactly this


async def test_audit_trail_covers_the_whole_trade(env: Env) -> None:
    """SEC-06/TRD-52: proposal, challenge, decision, authorisation, broker outcomes and the
    protective order are all in the hash-chained audit log, linked to their entities."""
    p = await env.propose()
    await env.approve(p.intent_id)
    await env.cycle(3)  # submit → acknowledged → filled → stop placed
    async with env.db.connect() as conn:
        rows = list(
            await conn.execute(
                select(
                    audit_events.c.action, audit_events.c.entity_type, audit_events.c.payload
                ).order_by(audit_events.c.seq)
            )
        )
    actions = [r.action for r in rows]
    trade = [a for a in actions if a.startswith(("trade.", "approval."))]
    assert trade == [
        "trade.proposed",
        "approval.challenge_issued",
        "approval.approved",
        "trade.order_authorized",
        "trade.order_acknowledged",  # broker-confirmed entry
        "trade.order_final",  # broker-confirmed fill
        "trade.protective_authorized",
        "trade.order_acknowledged",  # broker-confirmed protective stop
    ]
    assert all(r.entity_type for r in rows if r.action.startswith(("trade.", "approval.")))
