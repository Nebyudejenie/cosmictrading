from __future__ import annotations

import pytest
from sqlalchemy import select

from cosmic.core.clock import FrozenClock
from cosmic.core.config import Settings
from cosmic.core.errors import (
    ConflictError,
    PermissionDeniedError,
    StepUpRequiredError,
    ValidationFailedError,
)
from cosmic.db.engine import Database
from cosmic.db.schema import audit_events, users
from cosmic.identity import mfa
from cosmic.identity import service as identity
from cosmic.identity.capabilities import Capability, Role
from tests.helpers import PASSWORD, make_user


async def _login(
    db: Database,
    settings: Settings,
    clock: FrozenClock,
    password: str = PASSWORD,
    username: str = "owner",
) -> identity.LoginResult | identity.LoginFailure:
    async with db.transaction() as conn:
        return await identity.login(
            conn,
            username=username,
            password=password,
            now=clock.now(),
            settings=settings,
            ip_address="127.0.0.1",
            user_agent="pytest",
        )


async def test_password_policy_and_duplicates(db: Database, clock: FrozenClock) -> None:
    await make_user(db, clock)
    with pytest.raises(ConflictError):
        await make_user(db, clock)
    with pytest.raises(ValidationFailedError):
        async with db.transaction() as conn:
            await identity.create_user(
                conn,
                username="weak",
                display_name="w",
                password="short",
                roles=[Role.VIEWER],
                now=clock.now(),
                actor=identity.Actor.system("t"),
            )


async def test_login_success_and_session(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    await make_user(db, clock)
    result = await _login(db, settings, clock)
    assert isinstance(result, identity.LoginResult)
    async with db.transaction() as conn:
        p = await identity.load_principal(
            conn, token=result.session_token, now=clock.now(), settings=settings
        )
    assert p is not None and p.username == "owner" and Capability.APPROVE_TRADE in p.capabilities
    assert p.mfa_verified_at is None


async def test_failed_logins_lock_account_and_are_audited(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    await make_user(db, clock)
    for _ in range(identity.MAX_FAILED_LOGINS):
        assert isinstance(
            await _login(db, settings, clock, password="wrong-password-xx"), identity.LoginFailure
        )
    locked = await _login(db, settings, clock)  # correct password, but locked
    assert isinstance(locked, identity.LoginFailure) and "locked" in locked.message
    async with db.connect() as conn:
        actions = [r.action for r in await conn.execute(select(audit_events.c.action))]
    assert actions.count("auth.login_failed") == identity.MAX_FAILED_LOGINS
    assert "auth.login_locked" in actions
    clock.advance(minutes=identity.LOCKOUT_MINUTES + 1)
    assert isinstance(await _login(db, settings, clock), identity.LoginResult)


async def test_unknown_user_generic_failure(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    result = await _login(db, settings, clock, username="nobody")
    assert (
        isinstance(result, identity.LoginFailure)
        and result.message == "invalid username or password"
    )


async def test_session_expiry_idle_and_revocation(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    user = await make_user(db, clock)
    result = await _login(db, settings, clock)
    assert isinstance(result, identity.LoginResult)
    clock.advance(minutes=settings.session_idle_minutes + 1)
    async with db.transaction() as conn:
        assert (
            await identity.load_principal(
                conn, token=result.session_token, now=clock.now(), settings=settings
            )
            is None
        )
    result2 = await _login(db, settings, clock)
    assert isinstance(result2, identity.LoginResult)
    async with db.transaction() as conn:
        n = await identity.revoke_all_sessions(
            conn,
            user_id=user.user_id,
            now=clock.now(),
            actor=identity.Actor.system("t"),
            reason="test",
        )
    assert n == 1
    async with db.transaction() as conn:
        assert (
            await identity.load_principal(
                conn, token=result2.session_token, now=clock.now(), settings=settings
            )
            is None
        )
        assert (
            await identity.load_principal(conn, token="garbage", now=clock.now(), settings=settings)
            is None
        )


async def test_mfa_enrolment_step_up_and_require(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    await make_user(db, clock)
    result = await _login(db, settings, clock)
    assert isinstance(result, identity.LoginResult)
    async with db.transaction() as conn:
        p = await identity.load_principal(
            conn, token=result.session_token, now=clock.now(), settings=settings
        )
        assert p is not None
        with pytest.raises(StepUpRequiredError):
            identity.require(p, Capability.APPROVE_TRADE, now=clock.now(), settings=settings)
        secret, uri = await identity.begin_mfa_enrolment(
            conn, principal=p, settings=settings, now=clock.now()
        )
    assert uri.startswith("otpauth://totp/")
    async with db.connect() as conn:
        stored = (await conn.execute(select(users.c.mfa_secret_enc))).scalar_one()
    assert secret.encode() not in bytes(stored)
    async with db.transaction() as conn:
        await identity.confirm_mfa_enrolment(
            conn,
            principal=p,
            code=mfa.code_at(secret, clock.now()),
            settings=settings,
            now=clock.now(),
        )
    async with db.transaction() as conn:
        p = await identity.load_principal(
            conn, token=result.session_token, now=clock.now(), settings=settings
        )
    assert p is not None and p.mfa_enabled and p.mfa_verified_at == clock.now()
    identity.require(p, Capability.APPROVE_TRADE, now=clock.now(), settings=settings)
    clock.advance(minutes=settings.step_up_minutes + 1)
    with pytest.raises(StepUpRequiredError):
        identity.require(p, Capability.APPROVE_TRADE, now=clock.now(), settings=settings)
    identity.require(p, Capability.VIEW, now=clock.now(), settings=settings)  # no step-up needed
    async with db.transaction() as conn:
        ok = await identity.step_up(
            conn,
            principal=p,
            code=mfa.code_at(secret, clock.now()),
            settings=settings,
            now=clock.now(),
        )
    assert ok
    async with db.transaction() as conn:  # same code again → replay rejected
        again = await identity.step_up(
            conn,
            principal=p,
            code=mfa.code_at(secret, clock.now()),
            settings=settings,
            now=clock.now(),
        )
    assert not again


async def test_viewer_cannot_use_owner_capabilities(
    db: Database, settings: Settings, clock: FrozenClock
) -> None:
    await make_user(db, clock, username="viewer", roles=[Role.VIEWER])
    result = await _login(db, settings, clock, username="viewer")
    assert isinstance(result, identity.LoginResult)
    async with db.transaction() as conn:
        p = await identity.load_principal(
            conn, token=result.session_token, now=clock.now(), settings=settings
        )
    assert p is not None
    for cap in (
        Capability.APPROVE_TRADE,
        Capability.READ_ACCOUNT,
        Capability.ADMINISTER_SYSTEM,
        Capability.ACTIVATE_EMERGENCY_STOP,
    ):
        with pytest.raises(PermissionDeniedError):
            identity.require(p, cap, now=clock.now(), settings=settings)
