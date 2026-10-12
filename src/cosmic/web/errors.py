"""Exception handlers: safe messages, correlation IDs, no stack traces to clients."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from cosmic.core.errors import (
    AuthenticationError,
    ConflictError,
    CosmicError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    StepUpRequiredError,
    ValidationFailedError,
)
from cosmic.core.logs import get_correlation_id, get_logger
from cosmic.web.deps import LoginRequired, MfaRequired, is_api, state

log = get_logger(__name__)

STATUS = {
    AuthenticationError: 401,
    StepUpRequiredError: 403,
    PermissionDeniedError: 403,
    NotFoundError: 404,
    ConflictError: 409,
    ValidationFailedError: 422,
    RateLimitedError: 429,
}


def _status_for(exc: CosmicError) -> int:
    for cls in type(exc).__mro__:
        if cls in STATUS:
            return STATUS[cls]
    return 400


def render_error(
    request: Request, status: int, code: str, message: str, verify_next: str | None = None
) -> Response:
    cid = get_correlation_id()
    if is_api(request):
        return JSONResponse(
            {"error": {"code": code, "message": message, "correlation_id": cid}}, status_code=status
        )
    st = state(request)
    principal = getattr(request.state, "principal", None)
    return st.templates.TemplateResponse(
        request,
        "error.html",
        {
            "status": status,
            "code": code,
            "message": message,
            "correlation_id": cid,
            "principal": principal,
            "verify_next": verify_next,
        },
        status_code=status,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def _login(request: Request, _exc: LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(MfaRequired)
    async def _mfa(request: Request, exc: MfaRequired) -> Response:
        return RedirectResponse("/mfa" if exc.enrolled else "/mfa/enroll", status_code=303)

    @app.exception_handler(StepUpRequiredError)
    async def _step_up(request: Request, exc: StepUpRequiredError) -> Response:
        if is_api(request):
            return render_error(request, 403, exc.code, exc.message)
        if request.method != "GET":  # offer the code, then return to the form (same origin only)
            back = urlsplit(request.headers.get("referer", ""))
            same = back.netloc in ("", request.headers.get("host", ""))
            path = back.path if same and back.path.startswith("/") else "/"
            return render_error(request, 403, exc.code, exc.message, verify_next=quote(path))
        return RedirectResponse(f"/mfa?next={request.url.path}", status_code=303)

    @app.exception_handler(CosmicError)
    async def _cosmic(request: Request, exc: CosmicError) -> Response:
        status = _status_for(exc)
        if status >= 400:
            log.info("request_rejected", code=exc.code, status=status, path=request.url.path)
        return render_error(request, status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) for e in exc.errors()})
        return render_error(
            request, 422, "validation_failed", "invalid input: " + ", ".join(fields)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        message = "not found" if exc.status_code == 404 else "request could not be processed"
        return render_error(request, exc.status_code, "http_error", message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        log.error("unhandled_error", path=request.url.path, exc_info=exc)
        return render_error(
            request,
            500,
            "internal_error",
            "internal error; the operator can trace it with the correlation ID",
        )
