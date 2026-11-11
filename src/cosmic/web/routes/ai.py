"""AI provider health and administration (keys are never displayed)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.ai import admin, keystore
from cosmic.ai.registry import load_registry
from cosmic.core.errors import NotFoundError
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import require
from cosmic.ops.integrations import integration_view
from cosmic.web.deps import authorize, authorize_form, state, verify_csrf
from cosmic.web.views import render
from cosmic.worker import queue

router = APIRouter()


@router.get("/ai", include_in_schema=False)
async def ai_page(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    async with st.db.connect() as conn:
        keys = await integration_view(conn, st.settings, st.clock.now())
        overview = await admin.provider_overview(conn, st.settings, st.clock.now(), keys)
    return await render(
        request, "ai.html", principal, providers=overview, policy=st.settings.ai_policy
    )


@router.post("/ai/{provider}/attest", include_in_schema=False)
async def ai_attest(request: Request, provider: str) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.ADMINISTER_SYSTEM)

    def num(name: str) -> int | None:
        raw = str(form.get(name, "")).strip()
        return int(raw) if raw.isdigit() else None

    async with st.db.transaction() as conn:
        await admin.attest(
            conn,
            principal=principal,
            settings=st.settings,
            provider_key=provider,
            statement=str(form.get("statement", "")),
            evidence_url=str(form.get("evidence_url", "")),
            days=num("days") or 30,
            rpm_limit=num("rpm_limit"),
            rpd_limit=num("rpd_limit"),
            now=st.clock.now(),
        )
    return RedirectResponse("/ai", status_code=303)


@router.post("/ai/{provider}/models", include_in_schema=False)
async def ai_approve_model(request: Request, provider: str) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    ctx = str(form.get("context_limit", "")).strip()
    async with st.db.transaction() as conn:
        await admin.approve_model(
            conn,
            principal=principal,
            settings=st.settings,
            provider_key=provider,
            model_id=str(form.get("model_id", "")),
            tier=str(form.get("tier", "")),
            evidence_url=str(form.get("evidence_url", "")),
            context_limit=int(ctx) if ctx.isdigit() else None,
            review_days=int(str(form.get("review_days", "30")) or 30),
            now=st.clock.now(),
        )
    return RedirectResponse("/ai", status_code=303)


@router.post("/ai/models/{approval_id}/revoke", include_in_schema=False)
async def ai_revoke(request: Request, approval_id: str) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.ADMINISTER_SYSTEM)
    await verify_csrf(request, principal, await request.form())
    try:
        aid = uuid.UUID(approval_id)
    except ValueError:
        raise NotFoundError("not found") from None
    async with st.db.transaction() as conn:
        await admin.revoke_model(
            conn, principal=principal, settings=st.settings, approval_id=aid, now=st.clock.now()
        )
    return RedirectResponse("/ai", status_code=303)


# ---- Admin → AI Configuration → API Connections (S81) ------------------------------------------


@router.get("/ai/connections", include_in_schema=False)
async def ai_connections(request: Request) -> Response:
    """Each free AI provider's connection: where its key comes from (masked), the selected
    model, the last tests, today's use and errors. Keys are never displayed."""
    st = state(request)
    principal = await authorize(request, Capability.ADMINISTER_SYSTEM)
    async with st.db.connect() as conn:
        keys = await integration_view(conn, st.settings, st.clock.now())
        overview = await admin.provider_overview(conn, st.settings, st.clock.now(), keys)
    return await render(
        request, "ai_connections.html", principal, providers=overview, policy=st.settings.ai_policy
    )


def _provider(request: Request, provider: str) -> str:
    if provider not in load_registry(state(request).settings):
        raise NotFoundError("unknown AI provider")
    return provider


@router.post("/ai/connections/{provider}/key", include_in_schema=False)
async def ai_key_save(request: Request, provider: str) -> Response:
    """Save (or replace) the provider's API key, with the owner's code: encrypted at once, only
    its last four characters kept for the mask; never shown again, logged or sent elsewhere."""
    st = state(request)
    principal, form = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    require(principal, Capability.ADMINISTER_SYSTEM, now=st.clock.now(), settings=st.settings)
    name = _provider(request, provider)
    async with st.db.transaction() as conn:
        await keystore.save(
            conn,
            st.settings,
            name,
            str(form.get("api_key") or ""),
            actor=principal.actor,
            now=st.clock.now(),
        )
    return RedirectResponse(f"/ai/connections?saved={name}", status_code=303)


@router.post("/ai/connections/{provider}/revoke", include_in_schema=False)
async def ai_key_revoke(request: Request, provider: str) -> Response:
    st = state(request)
    principal, _ = await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    require(principal, Capability.ADMINISTER_SYSTEM, now=st.clock.now(), settings=st.settings)
    name = _provider(request, provider)
    async with st.db.transaction() as conn:
        await keystore.revoke(conn, name, actor=principal.actor, now=st.clock.now())
    return RedirectResponse("/ai/connections", status_code=303)


@router.post("/ai/connections/{provider}/test", include_in_schema=False)
async def ai_key_test(request: Request, provider: str) -> Response:
    """Queue the worker's test: authentication, model, real inference, structured answer."""
    st = state(request)
    await authorize_form(request, Capability.ADMINISTER_SYSTEM)
    name = _provider(request, provider)
    async with st.db.transaction() as conn:
        await queue.enqueue(
            conn,
            kind="ai.test_provider",
            now=st.clock.now(),
            payload={"provider_key": name},
            priority=queue.PRIORITY_DEFAULT,
        )
    return RedirectResponse(f"/ai/connections?testing={name}", status_code=303)
