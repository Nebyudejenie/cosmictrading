"""AI provider health and administration (keys are never displayed)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from cosmic.ai import admin
from cosmic.core.errors import NotFoundError
from cosmic.identity.capabilities import Capability
from cosmic.ops.integrations import integration_view
from cosmic.web.deps import authorize, state, verify_csrf
from cosmic.web.views import render

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
    principal = await authorize(request, Capability.ADMINISTER_SYSTEM)
    form = await request.form()
    await verify_csrf(request, principal, form)

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
    principal = await authorize(request, Capability.ADMINISTER_SYSTEM)
    form = await request.form()
    await verify_csrf(request, principal, form)
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
