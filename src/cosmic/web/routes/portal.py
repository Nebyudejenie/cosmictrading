"""Portal pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from cosmic.audit.service import head
from cosmic.db.migrate import head_revision
from cosmic.identity.capabilities import Capability
from cosmic.ops.health import current_revision
from cosmic.web.deps import authorize, state

router = APIRouter()


@router.get("/", include_in_schema=False)
async def home(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    async with st.db.connect() as conn:
        audit_head = await head(conn)
    revision = await current_revision(st.db)
    return st.templates.TemplateResponse(
        request,
        "home.html",
        {
            "principal": principal,
            "csrf_token": principal.csrf_token,
            "env": st.settings.env.value,
            "schema_revision": revision,
            "schema_head": head_revision(),
            "audit_head_seq": audit_head[0] if audit_head else 0,
            "integrations": st.settings.integration_status(),
            "now": st.clock.now(),
        },
    )
