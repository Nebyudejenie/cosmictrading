from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, PermissionDeniedError, StepUpRequiredError
from cosmic.db.engine import Database
from cosmic.db.schema import incidents
from cosmic.db.schema_trading import authority_transitions
from cosmic.identity.capabilities import Role
from cosmic.trading import authority
from cosmic.trading.modes import Mode
from tests.helpers import make_user, principal_for


async def test_default_is_research_only(db: Database) -> None:
    async with db.connect() as conn:
        state = await authority.get_state(conn)
    assert state.mode is Mode.RESEARCH_ONLY and not state.live_enabled


async def test_enable_paper_requires_mfa_and_reason(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    owner = await make_user(db, clock)
    stale = principal_for(owner, clock, mfa_fresh=False)
    with pytest.raises(StepUpRequiredError):
        async with db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=stale,
                target=Mode.PAPER_TRADING,
                reason="start paper trading",
                now=clock.now(),
                settings=settings,
            )
    p = principal_for(owner, clock)
    with pytest.raises(ConflictError, match="reason"):
        async with db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=p,
                target=Mode.PAPER_TRADING,
                reason="go",
                now=clock.now(),
                settings=settings,
            )
    async with db.transaction() as conn:
        state = await authority.change_mode(
            conn,
            principal=p,
            target=Mode.PAPER_TRADING,
            reason="start paper trading",
            now=clock.now(),
            settings=settings,
        )
    assert state.mode is Mode.PAPER_TRADING and state.version == 2
    async with db.connect() as conn:
        rows = list(await conn.execute(select(authority_transitions)))
    assert [(r.from_mode, r.to_mode) for r in rows] == [("RESEARCH_ONLY", "PAPER_TRADING")]


@pytest.mark.parametrize("target", [Mode.APPROVAL_REQUIRED, Mode.STRATEGY_AUTHORIZED])
async def test_live_modes_unreachable(
    db: Database, settings: Settings, clock: FrozenClock, target: Mode
) -> None:
    p = principal_for(await make_user(db, clock), clock)
    with pytest.raises(ConflictError) as exc:
        async with db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=p,
                target=target,
                reason="try to go live now",
                now=clock.now(),
                settings=settings,
            )
    msg = str(exc.value)
    assert "no live broker adapter" in msg and "gate I has not passed" in msg
    assert "COSMIC_MAX_AUTHORITY_MODE" in msg
    async with db.connect() as conn:
        assert (await authority.get_state(conn)).mode is Mode.RESEARCH_ONLY


async def test_restricting_needs_capability_but_no_mfa(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    owner = principal_for(await make_user(db, clock), clock)
    async with db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=owner,
            target=Mode.PAPER_TRADING,
            reason="start paper trading",
            now=clock.now(),
            settings=settings,
        )
    viewer = principal_for(
        await make_user(db, clock, username="viewer", roles=[Role.VIEWER]), clock, roles=("VIEWER",)
    )
    with pytest.raises(PermissionDeniedError):
        async with db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=viewer,
                target=Mode.RESEARCH_ONLY,
                reason="viewer tries",
                now=clock.now(),
                settings=settings,
            )
    analyst = principal_for(
        await make_user(db, clock, username="analyst", roles=[Role.ANALYST]),
        clock,
        roles=("ANALYST",),
        mfa_fresh=False,
    )
    async with db.transaction() as conn:
        state = await authority.change_mode(
            conn,
            principal=analyst,
            target=Mode.PROTECTION_ONLY,
            reason="analyst sees a problem",
            now=clock.now(),
            settings=settings,
        )
    assert state.mode is Mode.PROTECTION_ONLY


async def test_emergency_stop_not_via_mode_change(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    p = principal_for(await make_user(db, clock), clock)
    with pytest.raises(ConflictError, match="emergency stop control"):
        async with db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=p,
                target=Mode.EMERGENCY_STOP,
                reason="stop it all",
                now=clock.now(),
                settings=settings,
            )
    async with db.transaction() as conn:
        await authority.system_restrict(
            conn, target=Mode.EMERGENCY_STOP, reason="test stop", now=clock.now(), actor=p.actor
        )
    with pytest.raises(ConflictError, match="recovery checklist"):
        async with db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=p,
                target=Mode.RESEARCH_ONLY,
                reason="leave stop",
                now=clock.now(),
                settings=settings,
            )


async def test_protection_only_exit_requires_reconciled_accounts(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    from decimal import Decimal

    from cosmic.sources.ingest import ensure_registry
    from cosmic.trading.accounts import create_paper_account

    p = principal_for(await make_user(db, clock), clock)
    async with db.transaction() as conn:
        await ensure_registry(conn, clock.now())
        await create_paper_account(
            conn,
            alias="paper-main",
            venue_key="COINBASE",
            base_currency="USD",
            initial_capital=Decimal("10000"),
            now=clock.now(),
            actor=p.actor,
        )
        await authority.system_restrict(
            conn, target=Mode.PROTECTION_ONLY, reason="recon failed", now=clock.now(), actor=p.actor
        )
    with pytest.raises(ConflictError, match="paper-main reconciliation is UNKNOWN"):
        async with db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=p,
                target=Mode.PAPER_TRADING,
                reason="resume paper trading",
                now=clock.now(),
                settings=settings,
            )


async def test_config_cap_forces_protection(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    p = principal_for(await make_user(db, clock), clock)
    async with db.transaction() as conn:
        await authority.change_mode(
            conn,
            principal=p,
            target=Mode.PAPER_TRADING,
            reason="start paper trading",
            now=clock.now(),
            settings=settings,
        )
    capped = settings.model_copy(update={"max_authority_mode": "RESEARCH_ONLY"})
    async with db.transaction() as conn:
        state = await authority.enforce_cap(conn, settings=capped, now=clock.now())
    assert state.mode is Mode.PROTECTION_ONLY
    async with db.connect() as conn:
        inc = (
            await conn.execute(
                select(incidents).where(incidents.c.dedup_key == "authority.cap_breach")
            )
        ).one()
    assert inc.severity == "CRITICAL"
    with pytest.raises(ConflictError, match="caps authority"):
        async with db.transaction() as conn:
            await authority.change_mode(
                conn,
                principal=p,
                target=Mode.PAPER_TRADING,
                reason="resume paper trading",
                now=clock.now(),
                settings=capped,
            )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO authority_state (id, mode, version, changed_at, changed_by, reason) VALUES (2,'PAPER_TRADING',1,now(),'x','x')",
        "DELETE FROM authority_state",
        "UPDATE authority_transitions SET to_mode = 'APPROVAL_REQUIRED'",
        "UPDATE authority_state SET mode = 'LIVE'",
    ],
)
async def test_authority_tables_protected(db: Database, statement: str) -> None:
    with pytest.raises(DBAPIError):
        async with db.transaction() as conn:
            await conn.execute(text(statement))
