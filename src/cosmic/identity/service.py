"""Authentication, sessions, MFA and authorization checks.

Authorization is enforced here (service layer), not only in web handlers, so the
portal, JSON API and Telegram bot share one implementation (ARC-03).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import any_, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor, ActorType, record
from cosmic.core.config import Settings
from cosmic.core.errors import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    StepUpRequiredError,
    ValidationFailedError,
)
from cosmic.core.ids import uuid7
from cosmic.db.schema import sessions, users
from cosmic.identity import mfa
from cosmic.identity.capabilities import (
    STEP_UP_CAPABILITIES,
    Capability,
    Role,
    capabilities_for_roles,
)
from cosmic.identity.passwords import (
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    username: str
    display_name: str
    roles: tuple[str, ...]
    capabilities: frozenset[Capability]
    session_id: uuid.UUID | None
    csrf_token: str | None
    mfa_enabled: bool
    mfa_verified_at: datetime | None
    telegram_user_id: int | None = None
    channel: str = "PORTAL"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def actor(self) -> Actor:
        return Actor(
            ActorType.TELEGRAM if self.channel == "TELEGRAM" else ActorType.USER, str(self.user_id)
        )

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def step_up_fresh(self, now: datetime, minutes: int) -> bool:
        return self.mfa_verified_at is not None and now - self.mfa_verified_at <= timedelta(
            minutes=minutes
        )


def require(
    principal: Principal, capability: Capability, *, now: datetime, settings: Settings
) -> None:
    """Raise unless the principal holds ``capability`` (and fresh MFA where required)."""
    if not principal.has(capability):
        raise PermissionDeniedError(f"missing capability {capability.value}")
    if capability in STEP_UP_CAPABILITIES:
        if not principal.mfa_enabled:
            raise StepUpRequiredError("enrol multi-factor authentication before this action")
        if principal.channel == "PORTAL" and not principal.step_up_fresh(
            now, settings.step_up_minutes
        ):
            raise StepUpRequiredError("confirm your authenticator code to continue")


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


async def create_user(
    conn: AsyncConnection,
    *,
    username: str,
    display_name: str,
    password: str,
    roles: list[Role],
    now: datetime,
    actor: Actor,
) -> uuid.UUID:
    username = username.strip().lower()
    if not (3 <= len(username) <= 64) or not all(c.isalnum() or c in "._-" for c in username):
        raise ValidationFailedError("username must be 3-64 characters: letters, digits, . _ -")
    validate_password(password, username)
    if not roles:
        raise ValidationFailedError("at least one role is required")
    exists = await conn.scalar(select(users.c.id).where(users.c.username == username))
    if exists is not None:
        raise ConflictError("username already exists")
    user_id = uuid7()
    await conn.execute(
        insert(users).values(
            id=user_id,
            username=username,
            display_name=display_name.strip() or username,
            password_hash=hash_password(password),
            roles=[r.value for r in roles],
            password_changed_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await record(
        conn,
        action="user.created",
        actor=actor,
        occurred_at=now,
        entity_type="user",
        entity_id=user_id,
        payload={"username": username, "roles": [r.value for r in roles]},
    )
    return user_id


async def count_owners(conn: AsyncConnection) -> int:
    value = await conn.scalar(
        select(func.count())
        .select_from(users)
        .where(literal(Role.OWNER.value) == any_(users.c.roles))
    )
    return int(value or 0)


@dataclass(frozen=True)
class LoginResult:
    session_token: str
    session_id: uuid.UUID
    user_id: uuid.UUID
    mfa_enabled: bool


@dataclass(frozen=True)
class LoginFailure:
    """Failed sign-in. Returned (not raised) so lockout counters and audit commit."""

    message: str

    def raise_(self) -> None:
        raise AuthenticationError(self.message)


async def login(
    conn: AsyncConnection,
    *,
    username: str,
    password: str,
    now: datetime,
    settings: Settings,
    ip_address: str | None,
    user_agent: str | None,
) -> LoginResult | LoginFailure:
    """Verify credentials and open a session.

    Failures are returned, not raised, so the caller's transaction commits the
    failed-attempt counter, lockout and audit record before reporting the error.
    """
    generic = LoginFailure("invalid username or password")
    username = username.strip().lower()[:64]
    row = (
        await conn.execute(select(users).where(users.c.username == username).with_for_update())
    ).first()
    if row is None:
        verify_password(None, password)  # equalise timing
        return generic
    if row.locked_until is not None and row.locked_until > now:
        verify_password(None, password)
        await record(
            conn,
            action="auth.login_locked",
            actor=Actor.user(row.id),
            occurred_at=now,
            entity_type="user",
            entity_id=row.id,
            payload={"ip": ip_address},
        )
        return LoginFailure("account temporarily locked; try again later")
    if not row.is_active or not verify_password(row.password_hash, password):
        failed = row.failed_logins + 1
        values: dict[str, object] = {"failed_logins": failed, "updated_at": now}
        if failed >= MAX_FAILED_LOGINS:
            values["locked_until"] = now + timedelta(minutes=LOCKOUT_MINUTES)
            values["failed_logins"] = 0
        await conn.execute(update(users).where(users.c.id == row.id).values(**values))
        await record(
            conn,
            action="auth.login_failed",
            actor=Actor.user(row.id),
            occurred_at=now,
            entity_type="user",
            entity_id=row.id,
            payload={"ip": ip_address, "locked": "locked_until" in values},
        )
        return generic
    updates: dict[str, object] = {"failed_logins": 0, "locked_until": None, "updated_at": now}
    if needs_rehash(row.password_hash):
        updates["password_hash"] = hash_password(password)
    await conn.execute(update(users).where(users.c.id == row.id).values(**updates))
    token = secrets.token_urlsafe(32)
    session_id = uuid7()
    await conn.execute(
        insert(sessions).values(
            id=session_id,
            user_id=row.id,
            token_hash=_token_hash(token),
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=settings.session_absolute_hours),
            ip_address=(ip_address or "")[:64] or None,
            user_agent=(user_agent or "")[:256] or None,
        )
    )
    await record(
        conn,
        action="auth.login",
        actor=Actor.user(row.id),
        occurred_at=now,
        entity_type="session",
        entity_id=session_id,
        payload={"ip": ip_address},
    )
    return LoginResult(token, session_id, row.id, bool(row.mfa_enabled))


async def load_principal(
    conn: AsyncConnection, *, token: str, now: datetime, settings: Settings
) -> Principal | None:
    """Resolve a session token. Expired, idle or revoked sessions resolve to None."""
    if not token or len(token) > 128:
        return None
    row = (
        await conn.execute(
            select(
                sessions.c.id.label("session_id"),
                sessions.c.csrf_token,
                sessions.c.expires_at,
                sessions.c.last_seen_at,
                sessions.c.mfa_verified_at,
                sessions.c.revoked_at,
                users.c.id.label("user_id"),
                users.c.username,
                users.c.display_name,
                users.c.roles,
                users.c.is_active,
                users.c.mfa_enabled,
                users.c.telegram_user_id,
            )
            .join(users, users.c.id == sessions.c.user_id)
            .where(sessions.c.token_hash == _token_hash(token))
        )
    ).first()
    if row is None or row.revoked_at is not None or not row.is_active:
        return None
    idle_limit = timedelta(minutes=settings.session_idle_minutes)
    if row.expires_at <= now or now - row.last_seen_at > idle_limit:
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == row.session_id, sessions.c.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason="expired")
        )
        return None
    if now - row.last_seen_at > timedelta(seconds=30):
        await conn.execute(
            update(sessions).where(sessions.c.id == row.session_id).values(last_seen_at=now)
        )
    return Principal(
        user_id=row.user_id,
        username=row.username,
        display_name=row.display_name,
        roles=tuple(row.roles),
        capabilities=capabilities_for_roles(row.roles),
        session_id=row.session_id,
        csrf_token=row.csrf_token,
        mfa_enabled=bool(row.mfa_enabled),
        mfa_verified_at=row.mfa_verified_at,
        telegram_user_id=row.telegram_user_id,
    )


async def logout(conn: AsyncConnection, *, principal: Principal, now: datetime) -> None:
    await conn.execute(
        update(sessions)
        .where(sessions.c.id == principal.session_id, sessions.c.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason="logout")
    )
    await record(
        conn,
        action="auth.logout",
        actor=principal.actor,
        occurred_at=now,
        entity_type="session",
        entity_id=principal.session_id,
    )


async def revoke_all_sessions(
    conn: AsyncConnection, *, user_id: uuid.UUID, now: datetime, actor: Actor, reason: str
) -> int:
    result = await conn.execute(
        update(sessions)
        .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason[:200])
    )
    await record(
        conn,
        action="auth.sessions_revoked",
        actor=actor,
        occurred_at=now,
        entity_type="user",
        entity_id=user_id,
        payload={"reason": reason, "count": result.rowcount},
    )
    return int(result.rowcount)


# ---- MFA -------------------------------------------------------------------


async def begin_mfa_enrolment(
    conn: AsyncConnection, *, principal: Principal, settings: Settings, now: datetime
) -> tuple[str, str]:
    """Store a new (not yet enabled) encrypted secret; return (secret, otpauth URI)."""
    if principal.mfa_enabled:
        raise ConflictError("multi-factor authentication is already enabled")
    secret = mfa.generate_secret()
    await conn.execute(
        update(users)
        .where(users.c.id == principal.user_id, users.c.mfa_enabled.is_(False))
        .values(
            mfa_secret_enc=mfa.encrypt_secret(settings.secret_key_bytes, secret),
            mfa_last_step=None,
            updated_at=now,
        )
    )
    return secret, mfa.provisioning_uri(secret, principal.username)


async def _verify_user_code(
    conn: AsyncConnection, *, user_id: uuid.UUID, code: str, settings: Settings, now: datetime
) -> bool:
    row = (
        await conn.execute(
            select(users.c.mfa_secret_enc, users.c.mfa_last_step)
            .where(users.c.id == user_id)
            .with_for_update()
        )
    ).first()
    if row is None or row.mfa_secret_enc is None:
        return False
    secret = mfa.decrypt_secret(settings.secret_key_bytes, bytes(row.mfa_secret_enc))
    step = mfa.verify_code(secret, code, now, row.mfa_last_step)
    if step is None:
        return False
    await conn.execute(
        update(users).where(users.c.id == user_id).values(mfa_last_step=step, updated_at=now)
    )
    return True


async def confirm_mfa_enrolment(
    conn: AsyncConnection, *, principal: Principal, code: str, settings: Settings, now: datetime
) -> None:
    if principal.mfa_enabled:
        raise ConflictError("multi-factor authentication is already enabled")
    if not await _verify_user_code(
        conn, user_id=principal.user_id, code=code, settings=settings, now=now
    ):
        raise ValidationFailedError("the code did not match; check your authenticator's clock")
    await conn.execute(
        update(users)
        .where(users.c.id == principal.user_id)
        .values(mfa_enabled=True, updated_at=now)
    )
    await conn.execute(
        update(sessions).where(sessions.c.id == principal.session_id).values(mfa_verified_at=now)
    )
    await record(
        conn,
        action="auth.mfa_enabled",
        actor=principal.actor,
        occurred_at=now,
        entity_type="user",
        entity_id=principal.user_id,
    )


async def step_up(
    conn: AsyncConnection, *, principal: Principal, code: str, settings: Settings, now: datetime
) -> bool:
    """Verify a TOTP code and mark the session as recently verified.

    Returns False on a wrong/replayed code (audited; the caller commits, then reports).
    """
    if not principal.mfa_enabled:
        raise StepUpRequiredError("enrol multi-factor authentication first")
    ok = await _verify_user_code(
        conn, user_id=principal.user_id, code=code, settings=settings, now=now
    )
    await record(
        conn,
        action="auth.mfa_verified" if ok else "auth.mfa_failed",
        actor=principal.actor,
        occurred_at=now,
        entity_type="session",
        entity_id=principal.session_id,
    )
    if not ok:
        return False
    await conn.execute(
        update(sessions).where(sessions.c.id == principal.session_id).values(mfa_verified_at=now)
    )
    return True


async def verify_totp_for_user(
    conn: AsyncConnection, *, user_id: uuid.UUID, code: str, settings: Settings, now: datetime
) -> bool:
    """TOTP check without a session (Telegram second factor)."""
    return await _verify_user_code(conn, user_id=user_id, code=code, settings=settings, now=now)
