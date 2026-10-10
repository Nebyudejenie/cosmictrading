"""Request-scoped helpers: state, principal resolution, auth gates, CSRF."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from cosmic.core.errors import AuthenticationError, RateLimitedError
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, load_principal, require
from cosmic.web.security import UNSAFE_METHODS, check_csrf, check_origin
from cosmic.web.state import AppState


class LoginRequired(Exception):
    """Raised for unauthenticated portal requests (redirects to /login)."""


class MfaRequired(Exception):
    """Raised when the session has not completed MFA (redirects to /mfa or /mfa/enroll)."""

    def __init__(self, enrolled: bool) -> None:
        super().__init__("mfa required")
        self.enrolled = enrolled


def state(request: Request) -> AppState:
    app_state: AppState = request.app.state.cosmic
    return app_state


def is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def optional_principal(request: Request) -> Principal | None:
    if hasattr(request.state, "principal"):
        cached: Principal | None = request.state.principal
        return cached
    st = state(request)
    token = request.cookies.get(st.settings.cookie_name)
    principal: Principal | None = None
    if token:
        async with st.db.transaction() as conn:
            principal = await load_principal(
                conn, token=token, now=st.clock.now(), settings=st.settings
            )
    request.state.principal = principal
    return principal


async def session_principal(request: Request) -> Principal:
    """Authenticated session (MFA may still be pending)."""
    principal = await optional_principal(request)
    if principal is None:
        if is_api(request):
            raise AuthenticationError("authentication required")
        raise LoginRequired()
    return principal


async def portal_principal(request: Request) -> Principal:
    """Fully authenticated: session + MFA enrolled + MFA verified for this session."""
    principal = await session_principal(request)
    if not principal.mfa_enabled or principal.mfa_verified_at is None:
        if is_api(request):
            raise AuthenticationError("multi-factor authentication required")
        raise MfaRequired(enrolled=principal.mfa_enabled)
    st = state(request)
    if is_api(request) and not st.api_limiter.allow(f"s:{principal.session_id}"):
        raise RateLimitedError("too many requests")
    return principal


async def verify_csrf(request: Request, principal: Principal, form: Any | None = None) -> None:
    if request.method not in UNSAFE_METHODS:
        return
    check_origin(request)
    provided = request.headers.get("x-csrf-token")
    if provided is None and form is not None:
        value = form.get("csrf_token")
        provided = value if isinstance(value, str) else None
    check_csrf(principal.csrf_token, provided)


async def authorize(request: Request, capability: Capability) -> Principal:
    principal = await portal_principal(request)
    st = state(request)
    require(principal, capability, now=st.clock.now(), settings=st.settings)
    return principal
