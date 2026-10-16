"""Command center, readiness, risk policy and internal metrics."""

from __future__ import annotations

import hmac
import re
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from cosmic.ai.admin import provider_overview
from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.db.migrate import head_revision
from cosmic.identity.capabilities import Capability
from cosmic.notify import inbox
from cosmic.ops import metrics
from cosmic.ops.integrations import integration_view
from cosmic.reporting import command_center
from cosmic.trading import golive
from cosmic.trading.policy import ALLOWLISTS, LIMIT_SEMANTICS, active_policy, change_policy
from cosmic.web.deps import authorize, authorize_form, state
from cosmic.web.views import render

router = APIRouter()
EDITABLE = (
    "max_order_notional",
    "max_position_notional",
    "max_gross_exposure",
    "max_concentration_pct",
    "max_daily_loss",
    "max_drawdown_pct",
    "max_loss_streak",
    "max_price_age_seconds",
    "max_spread_bps",
    "max_cross_source_deviation_bps",
    "max_slippage_bps",
    "max_cost_bps",
    "fee_bps_assumption",
    "approval_ttl_seconds",
    "intent_ttl_seconds",
    "risk_per_trade_pct",
    "uncertainty_haircut_pct",
)


@router.get("/", include_in_schema=False)
async def home(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    now = st.clock.now()
    async with st.db.connect() as conn:
        integrations = await integration_view(conn, st.settings, now)
        providers = await provider_overview(conn, st.settings, now, integrations)
        ai_status = (
            "AVAILABLE"
            if any(p["status"] == "AVAILABLE" for p in providers)
            else (
                "NOT_CONFIGURED"
                if all(p["status"] in ("NOT_CONFIGURED", "DISABLED") for p in providers)
                else "DEGRADED"
            )
        )
        cc = await command_center.build(conn, now, ai_status=ai_status)
        unread = await inbox.unread_count(conn, principal.user_id)
    return await render(
        request,
        "home.html",
        principal,
        cc=cc,
        unread_alerts=unread,
        schema_head=head_revision(),
        env=st.settings.env.value,
        integrations=integrations,
    )


@router.get("/alerts", include_in_schema=False)
async def alerts_page(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    async with st.db.connect() as conn:
        rows = await inbox.portal_alerts(conn, principal.user_id)
    unread = sum(1 for r in rows if r["unread"])
    return await render(request, "alerts.html", principal, alerts=rows, unread=unread)


@router.post("/alerts/read", include_in_schema=False)
async def alerts_read(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.VIEW)
    raw = str(form.get("alert_id", "")).strip()
    try:
        alert_id = uuid.UUID(raw) if raw else None
    except ValueError:
        raise NotFoundError("unknown alert") from None
    async with st.db.transaction() as conn:
        await inbox.mark_read(conn, principal.user_id, st.clock.now(), alert_id)
    return RedirectResponse("/alerts", status_code=303)


@router.get("/golive", include_in_schema=False)
async def golive_page(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.VIEW)
    async with st.db.connect() as conn:
        gates = await golive.readiness(conn, st.clock.now())
    return await render(request, "golive.html", principal, gates=gates)


def parse_venue_fees(raw: str) -> list[dict[str, str]]:
    """``BINANCE=10, HFM=0`` → venue fee entries; ``none`` removes them all. The policy model
    validates venues and amounts; change_policy rejects venues that do not exist."""
    if raw.strip().lower() == "none":
        return []
    out: list[dict[str, str]] = []
    for part in re.split(r"[\s,]+", raw.strip()):
        if not part:
            continue
        venue, sep, bps = part.partition("=")
        if not sep or not venue or not bps:
            raise ValidationFailedError(f"venue_fees: write each as VENUE=BPS, not {part!r}")
        out.append({"venue": venue.upper(), "bps": bps})
    return out


@router.get("/settings/risk", include_in_schema=False)
async def risk_page(request: Request) -> Response:
    st = state(request)
    principal = await authorize(request, Capability.READ_ACCOUNT)
    async with st.db.connect() as conn:
        try:
            version, policy = await active_policy(conn)
        except ConflictError as exc:  # missing/invalid/tampered: show the fail-closed state
            return await render(request, "risk.html", principal, policy_error=exc.message)
    return await render(
        request,
        "risk.html",
        principal,
        version=version,
        policy=policy.model_dump(),
        editable=EDITABLE,
        allowlists=ALLOWLISTS,
        semantics=LIMIT_SEMANTICS,
    )


@router.post("/settings/risk", include_in_schema=False)
async def risk_change(request: Request) -> Response:
    st = state(request)
    principal, form = await authorize_form(request, Capability.CHANGE_RISK_LIMITS)
    async with st.db.transaction() as conn:
        _, policy = await active_policy(conn)
        params = policy.model_dump(mode="python")
        for key in EDITABLE:
            raw = str(form.get(key, "")).strip()
            if not raw:
                continue
            try:
                params[key] = int(raw) if isinstance(params[key], int) else Decimal(raw)
            except (ValueError, InvalidOperation):
                raise ValidationFailedError(f"{key} must be a number") from None
        for key in ALLOWLISTS:  # comma-separated; the form shows the current list to edit
            raw = str(form.get(key, "")).strip()
            if raw:
                params[key] = tuple(dict.fromkeys(x for x in re.split(r"[\s,]+", raw) if x))
        raw_fees = str(form.get("venue_fees", "")).strip()
        if raw_fees:  # like the lists: shown filled in; empty leaves it unchanged
            params["venue_fees"] = parse_venue_fees(raw_fees)
        await change_policy(
            conn,
            principal=principal,
            new_params=params,
            reason=str(form.get("reason", "")),
            now=st.clock.now(),
            settings=st.settings,
        )
    return RedirectResponse("/settings/risk", status_code=303)


@router.get("/internal/metrics", include_in_schema=False)
async def internal_metrics(request: Request) -> Response:
    st = state(request)
    token = st.settings.metrics_token
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {token.get_secret_value()}" if token is not None else None
    if expected is None or not hmac.compare_digest(supplied, expected):
        raise NotFoundError("not found")  # indistinguishable from a missing route
    async with st.db.connect() as conn:
        body = await metrics.collect(conn, st.clock.now())
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
