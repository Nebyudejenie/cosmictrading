"""Eligibility of a provider/model route under FREE_CLOUD_AI_ONLY (fail closed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.ai.registry import ProviderConfig
from cosmic.core.config import Settings
from cosmic.db.schema_ai import ai_model_approvals, ai_provider_attestations, ai_provider_state

PRICE_CHECK_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class Route:
    provider: ProviderConfig
    model_id: str
    tier: str
    approval_id: Any


@dataclass
class Eligibility:
    ok: bool
    reasons: list[str] = field(default_factory=list)


async def active_attestation(conn: AsyncConnection, provider_key: str, now: datetime) -> Any:
    return (
        await conn.execute(
            select(ai_provider_attestations)
            .where(
                ai_provider_attestations.c.provider_key == provider_key,
                ai_provider_attestations.c.revoked_at.is_(None),
                ai_provider_attestations.c.expires_at > now,
            )
            .order_by(desc(ai_provider_attestations.c.attested_at))
            .limit(1)
        )
    ).first()


async def provider_state(conn: AsyncConnection, provider_key: str) -> Any:
    return (
        await conn.execute(
            select(ai_provider_state).where(ai_provider_state.c.provider_key == provider_key)
        )
    ).first()


async def provider_eligibility(
    conn: AsyncConnection, p: ProviderConfig, settings: Settings, now: datetime
) -> Eligibility:
    reasons: list[str] = []
    if settings.ai_policy != "FREE_CLOUD_AI_ONLY":
        reasons.append("AI policy is not FREE_CLOUD_AI_ONLY")
    if not p.enabled:
        reasons.append(p.disabled_reason or "provider disabled in the registry")
    if p.api_key(settings) is None:
        reasons.append("no API key configured (NOT_CONFIGURED)")
    if p.requires_attestation and await active_attestation(conn, p.key, now) is None:
        reasons.append("no valid owner attestation that the account is on a free plan")
    st = await provider_state(conn, p.key)
    if st is not None:
        if st.disabled_reason:
            reasons.append(f"disabled: {st.disabled_reason}")
        if st.rate_limited_until and st.rate_limited_until > now:
            reasons.append("rate limited by the provider")
        if st.circuit == "OPEN" and st.opened_until and st.opened_until > now:
            reasons.append("circuit breaker open after repeated failures")
    return Eligibility(not reasons, reasons)


def model_eligibility(p: ProviderConfig, approval: Any, now: datetime) -> Eligibility:
    reasons: list[str] = []
    if approval is None:
        return Eligibility(False, ["model is not approved"])
    if approval.revoked_at is not None:
        reasons.append("model approval revoked")
    if approval.review_expires_at <= now:
        reasons.append("model approval review expired")
    if not p.model_allowed_by_pattern(approval.model_id):
        reasons.append("model ID is not allowed for this provider (e.g. not a ':free' model)")
    if p.runtime_price_check:
        if (
            approval.price_checked_at is None
            or now - approval.price_checked_at > PRICE_CHECK_MAX_AGE
        ):
            reasons.append("live price check missing or older than 24 h")
        elif approval.price_check_result != "ZERO":
            reasons.append(f"live price check result: {approval.price_check_result}")
    return Eligibility(not reasons, reasons)


async def eligible_routes(
    conn: AsyncConnection,
    providers: dict[str, ProviderConfig],
    settings: Settings,
    now: datetime,
    tiers: tuple[str, ...],
) -> tuple[list[Route], list[str]]:
    routes: list[Route] = []
    why: list[str] = []
    for tier in tiers:
        approvals = list(
            await conn.execute(
                select(ai_model_approvals)
                .where(ai_model_approvals.c.tier == tier, ai_model_approvals.c.revoked_at.is_(None))
                .order_by(ai_model_approvals.c.approved_at)
            )
        )
        for a in approvals:
            p = providers.get(a.provider_key)
            if p is None:
                why.append(f"{a.provider_key}/{a.model_id}: provider not in registry")
                continue
            pe = await provider_eligibility(conn, p, settings, now)
            me = model_eligibility(p, a, now)
            if pe.ok and me.ok:
                routes.append(Route(p, a.model_id, tier, a.id))
            else:
                why.append(f"{p.key}/{a.model_id}: " + "; ".join(pe.reasons + me.reasons))
    if not routes and not why:
        why.append("no model has been approved for this task")
    return routes, why
