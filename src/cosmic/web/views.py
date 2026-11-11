"""Shared rendering: navigation, principal, CSRF token and clock in every portal page."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response

from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal
from cosmic.web.deps import state

# HFM gold only (the owner, 2026-11-04): its pages first; everything else under "More",
# Binance's marked paused (kept, never deleted).
NAV: tuple[tuple[str, str, Capability], ...] = (
    ("/gold/analysis", "Gold analysis", Capability.READ_MARKET_DATA),
    ("/gold/sim", "My Gold Auto-Trader", Capability.READ_ACCOUNT),
    ("/hfm/settings", "HFM demo", Capability.READ_ACCOUNT),
    ("/brokers", "Broker connections", Capability.ADMINISTER_SYSTEM),
    ("/ai", "AI", Capability.VIEW),
    ("/settings/risk", "Risk settings", Capability.READ_ACCOUNT),
    ("/ops", "System health", Capability.VIEW),
)
MORE: tuple[tuple[str, str, Capability], ...] = (
    ("/alerts", "Alerts", Capability.VIEW),
    ("/approvals", "Approvals", Capability.READ_ACCOUNT),
    ("/portfolio", "Portfolio", Capability.READ_ACCOUNT),
    ("/orders", "Orders", Capability.READ_ACCOUNT),
    ("/golive", "Go-live", Capability.VIEW),
    ("/audit", "Audit", Capability.READ_ACCOUNT),
    ("/trader", "Auto-trader (rules)", Capability.READ_ACCOUNT),
    ("/research", "Research", Capability.VIEW),
    ("/search", "Search", Capability.VIEW),
    ("/strategies", "Strategies", Capability.VIEW),
    ("/market", "Market", Capability.READ_MARKET_DATA),
    ("/sources", "Sources", Capability.READ_MARKET_DATA),
    ("/rehearsal", "Binance rehearsal (paused)", Capability.READ_ACCOUNT),
    ("/gold", "Binance gold (paused)", Capability.READ_ACCOUNT),
)


def nav_for(
    principal: Principal, items: tuple[tuple[str, str, Capability], ...] = NAV
) -> list[dict[str, str]]:
    return [{"href": h, "label": label} for h, label, cap in items if principal.has(cap)]


async def page_context(request: Request, principal: Principal) -> dict[str, Any]:
    st = state(request)
    ctx: dict[str, Any] = {
        "principal": principal,
        "csrf_token": principal.csrf_token,
        "nav_items": nav_for(principal),
        "nav_more": nav_for(principal, MORE),
        "now": st.clock.now(),
        # privileged forms show an authenticator-code field when this is False
        "mfa_fresh": principal.step_up_fresh(st.clock.now(), st.settings.step_up_minutes),
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
