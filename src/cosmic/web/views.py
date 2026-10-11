"""Shared rendering: navigation, principal, CSRF token and clock in every portal page."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response

from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal
from cosmic.web.deps import state

NAV: tuple[tuple[str, str, Capability], ...] = (
    ("/approvals", "Approvals", Capability.READ_ACCOUNT),
    ("/portfolio", "Portfolio", Capability.READ_ACCOUNT),
    ("/orders", "Orders", Capability.READ_ACCOUNT),
    ("/research", "Research", Capability.VIEW),
    ("/strategies", "Strategies", Capability.VIEW),
    ("/market", "Market", Capability.READ_MARKET_DATA),
    ("/sources", "Sources", Capability.READ_MARKET_DATA),
    ("/ai", "AI", Capability.VIEW),
    ("/ops", "Operations", Capability.VIEW),
    ("/golive", "Go-live", Capability.VIEW),
    ("/settings/risk", "Risk policy", Capability.READ_ACCOUNT),
    ("/audit", "Audit", Capability.READ_ACCOUNT),
)


def nav_for(principal: Principal) -> list[dict[str, str]]:
    return [{"href": h, "label": label} for h, label, cap in NAV if principal.has(cap)]


async def page_context(request: Request, principal: Principal) -> dict[str, Any]:
    st = state(request)
    ctx: dict[str, Any] = {
        "principal": principal,
        "csrf_token": principal.csrf_token,
        "nav_items": nav_for(principal),
        "now": st.clock.now(),
    }
    hook = getattr(request.app.state, "authority_banner", None)
    if hook is not None:
        ctx["authority"] = await hook(request)
    return ctx


async def render(
    request: Request, template: str, principal: Principal, status_code: int = 200, **extra: Any
) -> Response:
    st = state(request)
    ctx = await page_context(request, principal)
    ctx.update(extra)
    return st.templates.TemplateResponse(request, template, ctx, status_code=status_code)
