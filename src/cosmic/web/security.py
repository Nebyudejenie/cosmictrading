"""HTTP security: headers, correlation IDs, CSRF and origin checks."""

from __future__ import annotations

import hmac
import re
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from cosmic.core.errors import PermissionDeniedError
from cosmic.core.logs import correlation_id_var, new_correlation_id

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; "
    "base-uri 'none'; object-src 'none'"
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def security_headers_middleware(
    hsts: bool, asset_version: str = ""
) -> Callable[..., Awaitable[Response]]:
    async def middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        cid = incoming if _REQUEST_ID.match(incoming) else new_correlation_id()
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        h = response.headers
        h["X-Request-ID"] = cid
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        # "same-origin", not "no-referrer": with no-referrer browsers send `Origin: null` on
        # form posts, which the origin check (rightly) rejects. Nothing leaks cross-site.
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        if not request.url.path.startswith("/static/"):
            h["Cache-Control"] = "no-store"
        elif asset_version and request.query_params.get("v") == asset_version:
            h["Cache-Control"] = "public, max-age=31536000, immutable"  # URL changes with content
        else:  # without the current version, caches must revalidate (no edge default TTL)
            h["Cache-Control"] = "no-cache"
        if hsts:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    return middleware


def https_redirect_middleware() -> Callable[..., Awaitable[Response]]:
    """Requests the edge received over plain HTTP are redirected to HTTPS (308 keeps the
    method). Cloudflare sets X-Forwarded-Proto; direct requests (container healthchecks on
    127.0.0.1) carry no such header and are served as usual."""

    async def middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("x-forwarded-proto", "").lower() == "http":
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        return await call_next(request)

    return middleware


BRIDGE_PATHS = ("/bridge/v1/sync", "/bridge/v1/fast")


class BridgeOnlyMiddleware:
    """Requests from ``peers`` reach the HFM bridge's two addresses and nothing else (S91).

    The bridge's direct link ends in a relay on the server that passes its bytes to this
    process (deploy/compose.bridge-link.yaml). Whoever holds the link's key could ask that
    relay for any page, so the rule is kept here, on the address the connection itself came
    from (the relay's fixed address on the internal network; it is not a trusted proxy, so no
    header can change it): the two bridge addresses, by POST, and 404 for everything else, the
    sign-in page and the health check included. The bridge's token is checked as always."""

    def __init__(self, app: ASGIApp, peers: frozenset[str]) -> None:
        self.app = app
        self.peers = peers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        client = scope.get("client")
        if scope["type"] == "lifespan" or client is None or client[0] not in self.peers:
            await self.app(scope, receive, send)
            return
        allowed = (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") in BRIDGE_PATHS
        )
        if allowed:
            await self.app(scope, receive, send)
        elif scope["type"] == "http":
            body = b"not found\n"
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
        else:  # a websocket: refused before it opens
            await send({"type": "websocket.close", "code": 1008})


def check_origin(request: Request) -> None:
    """Reject cross-origin unsafe requests when the browser declares an Origin."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host", "")
    allowed = {f"https://{host}", f"http://{host}"}
    if origin not in allowed:
        raise PermissionDeniedError("cross-origin request rejected")


def check_csrf(expected: str | None, provided: str | None) -> None:
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        raise PermissionDeniedError("missing or invalid CSRF token; reload the page and retry")
