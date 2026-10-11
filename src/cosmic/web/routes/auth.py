"""Login, logout, MFA enrolment and verification."""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.core.errors import CosmicError, RateLimitedError
from cosmic.identity import service as identity
from cosmic.web.deps import client_ip, session_principal, state, verify_csrf
from cosmic.web.security import check_csrf, check_origin

router = APIRouter()
LOGIN_CSRF_COOKIE = "cosmic_login_csrf"


def _safe_next(value: str | None) -> str:
    if not value:
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _login_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    st = state(request)
    token = secrets.token_urlsafe(24)
    response = st.templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": token, "error": error, "principal": None},
        status_code=status_code,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=st.settings.cookie_secure,
        max_age=900,
        path="/login",
    )
    return response


@router.get("/login", include_in_schema=False)
async def login_form(request: Request) -> Response:
    return _login_page(request)


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request) -> Response:
    st = state(request)
    form = await request.form()
    check_origin(request)
    provided = form.get("csrf_token")
    check_csrf(
        request.cookies.get(LOGIN_CSRF_COOKIE), provided if isinstance(provided, str) else None
    )
    username = str(form.get("username", ""))[:64]
    password = str(form.get("password", ""))[:256]
    ip = client_ip(request)
    if not st.login_ip_limiter.allow(f"ip:{ip}") or not st.login_user_limiter.allow(
        f"u:{username.lower()}"
    ):
        raise RateLimitedError("too many sign-in attempts; wait a few minutes")
    async with st.db.transaction() as conn:
        result = await identity.login(
            conn,
            username=username,
            password=password,
            now=st.clock.now(),
            settings=st.settings,
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
        )
    if isinstance(result, identity.LoginFailure):
        return _login_page(request, result.message, status_code=401)
    response = RedirectResponse("/mfa" if result.mfa_enabled else "/mfa/enroll", status_code=303)
    response.set_cookie(
        st.settings.cookie_name,
        result.session_token,
        httponly=True,
        samesite="strict",
        secure=st.settings.cookie_secure,
        max_age=st.settings.session_absolute_hours * 3600,
        path="/",
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    st = state(request)
    principal = await session_principal(request)
    await verify_csrf(request, principal, await request.form())
    async with st.db.transaction() as conn:
        await identity.logout(conn, principal=principal, now=st.clock.now())
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(st.settings.cookie_name, path="/")
    return response


@router.get("/mfa/enroll", include_in_schema=False)
async def mfa_enroll_form(request: Request) -> Response:
    st = state(request)
    principal = await session_principal(request)
    if principal.mfa_enabled:
        return RedirectResponse("/mfa", status_code=303)
    async with st.db.transaction() as conn:
        secret, uri = await identity.begin_mfa_enrolment(
            conn, principal=principal, settings=st.settings, now=st.clock.now()
        )
    grouped = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    return st.templates.TemplateResponse(
        request,
        "mfa_enroll.html",
        {
            "principal": principal,
            "csrf_token": principal.csrf_token,
            "secret": grouped,
            "uri": uri,
            "error": None,
        },
    )


@router.post("/mfa/enroll", include_in_schema=False)
async def mfa_enroll_submit(request: Request) -> Response:
    st = state(request)
    principal = await session_principal(request)
    form = await request.form()
    await verify_csrf(request, principal, form)
    if not st.mfa_limiter.allow(f"mfa:{principal.user_id}"):
        raise RateLimitedError("too many attempts; wait a few minutes")
    try:
        async with st.db.transaction() as conn:
            await identity.confirm_mfa_enrolment(
                conn,
                principal=principal,
                code=str(form.get("code", "")),
                settings=st.settings,
                now=st.clock.now(),
            )
    except CosmicError as exc:
        return st.templates.TemplateResponse(
            request,
            "mfa_enroll.html",
            {
                "principal": principal,
                "csrf_token": principal.csrf_token,
                "secret": None,  # template value, not a credential  # nosec B105
                "uri": None,
                "error": exc.message + " — reload the page to get a new secret.",
            },
            status_code=422,
        )
    return RedirectResponse("/", status_code=303)


@router.get("/mfa", include_in_schema=False)
async def mfa_form(request: Request, next: str | None = None) -> Response:
    st = state(request)
    principal = await session_principal(request)
    if not principal.mfa_enabled:
        return RedirectResponse("/mfa/enroll", status_code=303)
    return st.templates.TemplateResponse(
        request,
        "mfa_verify.html",
        {
            "principal": principal,
            "csrf_token": principal.csrf_token,
            "next": _safe_next(next),
            "error": None,
        },
    )


@router.post("/mfa", include_in_schema=False)
async def mfa_submit(request: Request) -> Response:
    st = state(request)
    principal = await session_principal(request)
    form = await request.form()
    await verify_csrf(request, principal, form)
    target = _safe_next(str(form.get("next", "/")))
    if not st.mfa_limiter.allow(f"mfa:{principal.user_id}"):
        raise RateLimitedError("too many attempts; wait a few minutes")
    async with st.db.transaction() as conn:
        ok = await identity.step_up(
            conn,
            principal=principal,
            code=str(form.get("code", "")),
            settings=st.settings,
            now=st.clock.now(),
        )
    if not ok:
        return st.templates.TemplateResponse(
            request,
            "mfa_verify.html",
            {
                "principal": principal,
                "csrf_token": principal.csrf_token,
                "next": target,
                "error": "invalid or already used authenticator code",
            },
            status_code=401,
        )
    return RedirectResponse(target, status_code=303)
